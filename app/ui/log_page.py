"""信令日志页：滚动显示业务层日志，支持级别着色、暂停、清空、导出。"""
from __future__ import annotations

import time

from ..core.applog import log_event
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

_MAX_BLOCKS = 5000          # 超出后自动裁掉最旧的，防止长时间运行吃内存


class LogPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # ---- 工具条 ----
        bar = QHBoxLayout()
        bar.setSpacing(8)

        bar.addWidget(QLabel("过滤"))
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("输入关键字，仅显示包含该关键字的日志（留空显示全部）")
        self.ed_filter.textChanged.connect(self._reapply_filter)
        bar.addWidget(self.ed_filter, 1)

        self.ck_autoscroll = QCheckBox("自动滚动")
        self.ck_autoscroll.setChecked(True)
        bar.addWidget(self.ck_autoscroll)

        self.ck_pause = QCheckBox("暂停")
        bar.addWidget(self.ck_pause)

        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self.clear)
        bar.addWidget(btn_clear)

        btn_export = QPushButton("导出…")
        btn_export.clicked.connect(self._export)
        bar.addWidget(btn_export)

        lay.addLayout(bar)

        # ---- 正文 ----
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(_MAX_BLOCKS)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setStyleSheet(
            "font-family: Consolas, 'Cascadia Mono', monospace; font-size: 12px;")
        lay.addWidget(self.view, 1)

        self._lines: list[str] = []      # 原始文本（用于过滤重放）
        self._pending = 0

    # ---------------- 写入 ----------------

    def append(self, text: str) -> None:
        """追加一行日志（带时间戳）。暂停时不显示，但内容仍保留在缓冲里。

        默认同时落盘到 app.log：传原文给 log_event，由 applog 统一加
        ``[YYYY-MM-DD HH:MM:SS]`` 时间戳，避免与下方 UI 时间戳重复。
        """
        t = text.rstrip()
        log_event(t)
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f"{ts}  {t}"
        self._lines.append(line)
        if len(self._lines) > _MAX_BLOCKS:
            del self._lines[: len(self._lines) - _MAX_BLOCKS]
        if self.ck_pause.isChecked():
            self._pending += 1
            return
        if self._match(line):
            self._append_colored(line)
            self._autoscroll()

    # 按关键字着色：错误 / 告警 / 成功
    _COLOR_RULES = (
        ("!! FAIL", "#c0392b"),
        ("失败", "#c0392b"),
        ("异常", "#c0392b"),
        ("错误", "#c0392b"),
        ("重试中", "#f56c6c"),
        ("心跳连续", "#d68910"),
        ("注册未成功", "#d68910"),
        ("PASS", "#67c23a"),
        ("注册成功", "#67c23a"),
        ("unregistration success", "#67c23a"),
    )

    def _append_colored(self, line: str) -> None:
        """把一行文本按规则着色后写入。

        统一走「insertText(line + 换行, 显式字符格式)」，修掉两个坑：
        1. appendPlainText 写完后文档末块不带换行，裸 insertText 会拼进
           上一行（表现为两条日志挤在一行）；
        2. insertText 会把光标字符格式置为所插入的颜色，后续 appendPlainText
           沿用该格式，未命中关键词的行会继承上一条命中行的颜色（表现为
           日志颜色成片漂移）。
        故每行都显式给出前景色（命中规则用规则色，否则用默认前景色）。
        """
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        for kw, col in self._COLOR_RULES:
            if kw in line:
                fmt.setForeground(QColor(col))
                break
        else:
            fmt.setForeground(
                self.view.palette().color(self.view.foregroundRole()))
        cursor.insertText(line + "\n", fmt)


    def _match(self, line: str) -> bool:
        kw = self.ed_filter.text().strip()
        return (not kw) or (kw.lower() in line.lower())

    def _autoscroll(self) -> None:
        if self.ck_autoscroll.isChecked():
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ---------------- 过滤 / 清空 / 导出 ----------------

    def _reapply_filter(self) -> None:
        kw = self.ed_filter.text().strip()
        self.view.setUpdatesEnabled(False)
        self.view.clear()
        for line in self._lines:
            if (not kw) or (kw.lower() in line.lower()):
                self._append_colored(line)
        self.view.setUpdatesEnabled(True)
        self._autoscroll()

    def clear(self) -> None:
        self._lines.clear()
        self._pending = 0
        self.view.clear()

    def _export(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        default = f"gbipc_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", default,
                                              "文本文件 (*.txt)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self._lines))
        self.append(f"[ui] 日志已导出: {path}")
