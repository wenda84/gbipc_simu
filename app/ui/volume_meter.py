"""音量电平表（分段 LED 风格），对应「国标广播」勾选框旁的声音强度提示框。

参考常见 SIP 话机面板上的音量指示：竖向排列的若干段指示灯，随收到的语音
音量自下而上点亮，并带峰值保持。本控件只负责绘制，电平值由业务层按
``broadcast_session`` 的限流回调（25 Hz）喂入。

电平语义（见 docs/GB28181_国标广播功能设计.md §4.7）：
``level``/``peak`` 均为已归一化到 [0, 1] 的值，0 = 静音（-60 dBFS 及以下），
1 = 满幅（0 dBFS）。映射本身在 ``media/alaw.py`` 中实现，本控件不感知 dB。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

# 段色：正常段（绿）→ 接近满幅（琥珀）→ 削顶风险（红）
_COL_LOW = QColor("#3cba54")
_COL_MID = QColor("#f0ad4e")
_COL_HIGH = QColor("#e74c3c")
# 峰值指示取对应段的深色版本，保证在亮段上仍可辨识
_PEAK_LOW = QColor("#2e8b44")
_PEAK_MID = QColor("#c98a2e")
_PEAK_HIGH = QColor("#c0392b")
_COL_OFF = QColor("#dcdcdc")

_GAP = 2


class VolumeMeter(QWidget):
    """竖向分段电平表。"""

    def __init__(self, segments: int = 8, parent=None) -> None:
        super().__init__(parent)
        self._segments = max(3, int(segments))
        self._level = 0.0
        self._peak = 0.0
        self._lit = 0
        self._peak_seg = -1
        # 尺寸与工具栏按钮（min-height 44px）对齐，避免工具栏高度跳动
        self.setFixedSize(QSize(16, 44))
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

    # ---------------- 绘制 ----------------

    def _segment_color(self, index: int, peak: bool) -> QColor:
        n = self._segments
        if index >= n - 1:
            return _PEAK_HIGH if peak else _COL_HIGH
        if index >= n - 3:
            return _PEAK_MID if peak else _COL_MID
        return _PEAK_LOW if peak else _COL_LOW

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        n = self._segments
        h = self.height()
        w = self.width()
        seg_h = max(1.0, (h - _GAP * (n - 1)) / float(n))
        for i in range(n):                      # i = 0 为最下方一段
            y = h - (i + 1) * seg_h - i * _GAP
            is_peak = (i == self._peak_seg and self._lit > 0)
            if i < self._lit:
                color = self._segment_color(i, is_peak)
            elif is_peak:
                # 峰值段未点亮时也显示，用于指示刚刚过去的峰值
                color = self._segment_color(i, True)
            else:
                color = _COL_OFF
            p.setBrush(color)
            p.drawRoundedRect(0, int(round(y)), w, int(round(seg_h)), 1.5, 1.5)
        p.end()
