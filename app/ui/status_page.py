"""运行状态页：注册状态 / 呼叫状态 / 推流统计。

业务层通过 DeviceEvents 回调上报，主窗口把它转成 Qt 信号送到这里。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QLabel, QVBoxLayout, QWidget,
)

from ..core.device import RegState

# 注册状态 -> (显示文本, 颜色)
_STATE_STYLE = {
    RegState.IDLE: ("未注册", "#888888"),
    RegState.REGISTERING: ("注册中", "#e6a23c"),
    RegState.ONLINE: ("在线", "#67c23a"),
    RegState.RETRYING: ("重试中", "#f56c6c"),
}


class _ValueLabel(QLabel):
    """定宽只读取值标签，避免状态变化时窗口跳动。"""

    def __init__(self, text: str = "—", color: str = "#333"):
        super().__init__(text)
        self._color = color
        self._apply(color)

    def _apply(self, color: str) -> None:
        self._color = color
        self.setStyleSheet(f"color:{color}; font-weight:600;")

    def set_value(self, text: str, color: str | None = None) -> None:
        self.setText(text)
        self._apply(color or self._color)


class StatusPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        lay.addWidget(self._build_conn_group())
        lay.addWidget(self._build_stream_group())
        lay.addWidget(self._build_broadcast_group())
        lay.addStretch(1)

    def _form(self, box: QGroupBox) -> QFormLayout:
        f = QFormLayout(box)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setHorizontalSpacing(14)
        f.setVerticalSpacing(9)
        f.setContentsMargins(14, 16, 14, 12)
        return f

    def _build_conn_group(self) -> QGroupBox:
        box = QGroupBox("连接状态")
        f = self._form(box)

        self.lb_reg_state = _ValueLabel("未注册", "#888888")
        f.addRow("注册状态", self.lb_reg_state)

        # 「详情」仅在注册异常时出现，正常/停止时隐藏整行
        self.lb_reg_detail_label = QLabel("详情")
        self.lb_reg_detail_label.setWordWrap(True)
        self.lb_reg_detail = _ValueLabel("—", "#666")
        self.lb_reg_detail.setWordWrap(True)
        f.addRow(self.lb_reg_detail_label, self.lb_reg_detail)
        self._hide_reg_detail()

        self.lb_device = _ValueLabel("—")
        f.addRow("设备 ID", self.lb_device)

        self.lb_local = _ValueLabel("—")
        f.addRow("本机地址", self.lb_local)

        self.lb_platform = _ValueLabel("—")
        f.addRow("平台地址", self.lb_platform)

        self.lb_keepalive = _ValueLabel("—")
        f.addRow("心跳", self.lb_keepalive)

        return box

    def _build_stream_group(self) -> QGroupBox:
        box = QGroupBox("国标点播")
        f = self._form(box)

        self.lb_call_state = _ValueLabel("空闲", "#888888")
        f.addRow("呼叫状态", self.lb_call_state)

        self.lb_stream_stat = _ValueLabel("—")
        f.addRow("推流统计", self.lb_stream_stat)

        return box

    def _build_broadcast_group(self) -> QGroupBox:
        box = QGroupBox("国标广播")
        f = self._form(box)

        self.lb_bc_state = _ValueLabel("未启用", "#888888")
        self.lb_bc_state.setWordWrap(True)
        f.addRow("广播状态", self.lb_bc_state)

        self.lb_bc_stream = _ValueLabel("—")
        f.addRow("收流统计", self.lb_bc_stream)

        return box

    # ---------------- 对外更新接口 ----------------

    def set_reg_state(self, state: RegState, detail: str = "") -> None:
        text, color = _STATE_STYLE.get(state, (state.value, "#333"))
        self.lb_reg_state.set_value(text, color)
        if detail:
            self._show_reg_detail(detail)
        else:
            self._hide_reg_detail()

    def set_reg_detail(self, detail: str) -> None:
        if detail:
            self._show_reg_detail(detail)
        else:
            self._hide_reg_detail()

    def _show_reg_detail(self, detail: str) -> None:
        """注册异常时显示「详情」整行。"""
        self.lb_reg_detail.set_value(detail, "#666")
        self.lb_reg_detail_label.show()
        self.lb_reg_detail.show()

    def _hide_reg_detail(self) -> None:
        """正常/停止时隐藏「详情」整行（QFormLayout 自动跳过隐藏行，不留空隙）。"""
        self.lb_reg_detail_label.hide()
        self.lb_reg_detail.hide()

    def set_call_state(self, text: str) -> None:
        if not text or text == "空闲":
            # 呼叫释放（回到空闲）后，上一轮推流统计随之失效，一并清空。
            # 若仍有其它点播呼叫在推流，业务层不会上报「空闲」，不受影响。
            self.lb_stream_stat.set_value("—", "#333")
        color = "#67c23a" if ("推流" in text) else (
            "#e6a23c" if "建立" in text else "#888888")
        self.lb_call_state.set_value(text or "空闲", color)

    def set_stream_stat(self, text: str) -> None:
        self.lb_stream_stat.set_value(text or "—", "#333")

    def set_device_info(self, device_id: str, local: str, platform: str) -> None:
        self.lb_device.set_value(device_id or "—")
        self.lb_local.set_value(local or "—")
        self.lb_platform.set_value(platform or "—")

    def set_local_addr(self, text: str) -> None:
        """单独刷新「本机地址」（未启动时显示配置值或「(自动探测)」）。"""
        self.lb_local.set_value(text or "—")

    def set_keepalive(self, text: str) -> None:
        self.lb_keepalive.set_value(text or "—")

    def set_broadcast_state(self, text: str) -> None:
        """广播子状态（空闲 / 呼叫中 / 广播中 / 失败）。"""
        color = "#67c23a" if "广播中" in text else (
            "#e6a23c" if "呼叫中" in text else (
                "#f56c6c" if "失败" in text else "#888888"))
        self.lb_bc_state.set_value(text or "未启用", color)
        if "广播中" not in text:
            self.lb_bc_stream.set_value("—")

    def set_broadcast_stream(self, text: str) -> None:
        self.lb_bc_stream.set_value(text or "—")

    def reset(self) -> None:
        """停止后清空动态项。"""
        self.set_reg_state(RegState.IDLE)
        self.set_call_state("空闲")
        self.set_stream_stat("—")
        self.set_keepalive("—")
        self.set_broadcast_state("未启用")
        self.set_broadcast_stream("—")
