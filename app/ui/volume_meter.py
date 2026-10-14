"""音量电平表（分段 LED 风格），对应「国标广播」勾选框旁的声音强度提示框。

参考常见 SIP 话机面板上的音量指示：竖向排列的若干段指示灯，随收到的语音
音量自下而上点亮，并带峰值保持。本控件只负责绘制，电平值由业务层按
``broadcast_session`` 的限流回调（25 Hz）喂入。

显示模式：
- ``MODE_OFF``：未勾选「国标广播」，音量框呈灰色；
- ``MODE_ARMED``：已勾选但呼叫尚未建立，音量框呈暗绿色；
- ``MODE_ACTIVE``：呼叫已建立，按实际电平自下而上点亮亮绿色段。

电平语义（见 docs/GB28181_国标广播功能设计.md §4.7）：
``level``/``peak`` 均为已归一化到 [0, 1] 的值，0 = 静音（-60 dBFS 及以下），
1 = 满幅（0 dBFS）。映射本身在 ``media/alaw.py`` 中实现，本控件不感知 dB。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

# 模式：未启用 / 已启用但未收流 / 正在收流
MODE_OFF = 0
MODE_ARMED = 1
MODE_ACTIVE = 2

# 未勾选时的灰色（与原先一致）
_COL_OFF = QColor("#dcdcdc")
# 已勾选、呼叫未建立时的暗绿色（待机指示）
_COL_ARMED = QColor("#2e7d32")
# 呼叫建立后实际音量的亮绿色
_COL_ACTIVE = QColor("#76ff03")
_COL_ACTIVE_PEAK = QColor("#64dd17")
# 呼叫已建立（活跃）但当前无音量时的「底色」：明确的绿色（明显亮于待机暗绿），
# 使活跃态即使静音也一眼可辨为「已激活」，而非回到暗绿待机或灰显。
_COL_ACTIVE_BASE = QColor("#4caf50")

_GAP = 2


class VolumeMeter(QWidget):
    """竖向分段电平表。

    支持三种显示模式：
    - ``MODE_OFF``：未启用国标广播，所有段显示灰色；
    - ``MODE_ARMED``：已勾选国标广播但呼叫尚未建立，所有段显示暗绿色；
    - ``MODE_ACTIVE``：广播呼叫已建立，按实际电平自下而上点亮亮绿色段。
    """

    def __init__(self, segments: int = 8, parent=None) -> None:
        super().__init__(parent)
        self._segments = max(3, int(segments))
        self._level = 0.0
        self._peak = 0.0
        self._lit = 0
        self._peak_seg = -1
        self._mode = MODE_OFF
        # 尺寸与工具栏按钮（min-height 44px）对齐，避免工具栏高度跳动；
        # 宽度 8px（约为原 16px 的一半），保持竖向分段 LED 的细条观感。
        self.setFixedSize(QSize(8, 44))
        self.setToolTip("收到的语音音量")
        self.setAccessibleName("语音广播音量")

    # ---------------- 对外接口 ----------------

    def set_level(self, level: float, peak: float | None = None) -> None:
        """设置电平。``level``/``peak`` ∈ [0, 1]。"""
        self._level = 0.0 if level < 0.0 else (1.0 if level > 1.0 else float(level))
        if peak is None:
            self._peak = self._level
        else:
            self._peak = 0.0 if peak < 0.0 else (1.0 if peak > 1.0 else float(peak))
        lit = int(round(self._level * self._segments))
        peak_seg = int(round(self._peak * self._segments)) - 1
        if peak_seg >= self._segments:
            peak_seg = self._segments - 1
        if lit != self._lit or peak_seg != self._peak_seg:
            self._lit = lit
            self._peak_seg = peak_seg
            self.update()

    def reset(self) -> None:
        self.set_level(0.0, 0.0)

    def set_mode(self, mode: int) -> None:
        """设置显示模式（``MODE_OFF`` / ``MODE_ARMED`` / ``MODE_ACTIVE``）。"""
        if mode not in (MODE_OFF, MODE_ARMED, MODE_ACTIVE):
            mode = MODE_OFF
        if mode != self._mode:
            self._mode = mode
            self.update()

    # ---------------- 绘制 ----------------

    def _active_color(self, peak: bool) -> QColor:
        return _COL_ACTIVE_PEAK if peak else _COL_ACTIVE

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        n = self._segments
        h = self.height()
        w = self.width()
        seg_h = max(1.0, (h - _GAP * (n - 1)) / float(n))

        if self._mode == MODE_OFF:
            color = _COL_OFF
            for i in range(n):
                y = h - (i + 1) * seg_h - i * _GAP
                p.setBrush(color)
                p.drawRoundedRect(0, int(round(y)), w, int(round(seg_h)), 1.5, 1.5)
            p.end()
            return

        if self._mode == MODE_ARMED:
            color = _COL_ARMED
            for i in range(n):
                y = h - (i + 1) * seg_h - i * _GAP
                p.setBrush(color)
                p.drawRoundedRect(0, int(round(y)), w, int(round(seg_h)), 1.5, 1.5)
            p.end()
            return

        # MODE_ACTIVE：底色为明确绿色，按实际电平自下而上点亮更亮的亮绿色段
        for i in range(n):                      # i = 0 为最下方一段
            y = h - (i + 1) * seg_h - i * _GAP
            is_peak = (i == self._peak_seg and self._lit > 0)
            if i < self._lit:
                color = self._active_color(is_peak)
            elif is_peak:
                # 峰值段未点亮时也显示，用于指示刚刚过去的峰值
                color = self._active_color(True)
            else:
                color = _COL_ACTIVE_BASE
            p.setBrush(color)
            p.drawRoundedRect(0, int(round(y)), w, int(round(seg_h)), 1.5, 1.5)
        p.end()
