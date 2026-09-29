"""平台接入配置页。

字段布局对齐海康「平台接入」设置页截图（resource/ui/ipc接入178.png），
仅保留本工具实际使用的参数（裁剪依据见 docs/GB28181_IPC模拟工具_总体方案.md §6）。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..core.config import AppConfig, USER_BASE_DIR

# PS 流素材目录（相对 USER_BASE_DIR：源码态=工程根，打包态=exe 同级目录）。
# 注意：不能用 ROOT_DIR——打包后 ROOT_DIR 落在 _internal 内，而素材由
# build.bat 拷贝到 exe 同级，二者不一致。
_PS_REL_DIR = os.path.join("resource", "ps_samples")
_PS_DIR = os.path.join(USER_BASE_DIR, _PS_REL_DIR)


class FocusSpinBox(QSpinBox):
    """仅在获得焦点后才响应滚轮的 QSpinBox。

    Qt 默认行为：QAbstractSpinBox 的焦点策略为 Qt.WheelFocus——鼠标悬停
    滚动滚轮时，Qt 会先把焦点给控件、再派发 wheel 事件，导致无需点击进入
    就直接改值（易误改）。

    此处双重保险：
      1) 焦点策略改为 Qt.ClickFocus：仅鼠标点击进入时才获得焦点，悬停
         滚轮不会自动抢焦点；
      2) 重写 wheelEvent：未获焦点时忽略滚轮事件（向上传递给外层
         QScrollArea，页面正常滚动），获焦点后才改值。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 关键：悬停滚轮不再自动抢焦点，否则 hasFocus() 在 wheel 时已为 True
        self.setFocusPolicy(Qt.ClickFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class FocusComboBox(QComboBox):
    """仅在获得焦点后才响应滚轮的 QComboBox（与 FocusSpinBox 行为统一）。

    Qt 默认 QComboBox 在可编辑状态下，鼠标悬停滚动滚轮时，滚轮事件会先
    落到内部 QLineEdit，再冒泡给组合框本身的 wheelEvent 从而直接切换选项
    （无需点击进入即可改值，易误改）。

    此处与 FocusSpinBox 保持一致：
      1) 焦点策略改为 Qt.ClickFocus：仅鼠标点击进入时才获得焦点，悬停
         滚轮不会自动抢焦点；
      2) 重写 wheelEvent：未获焦点时忽略滚轮事件（向上传递给外层
         QScrollArea，页面正常滚动），获焦点后才切换选项；
      3) 可编辑时同步把内部 QLineEdit 的焦点策略设为 ClickFocus，
         保证输入框也需点击进入才聚焦。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 悬停滚轮不再自动抢焦点，否则 hasFocus() 在 wheel 时已为 True
        self.setFocusPolicy(Qt.ClickFocus)

    def setEditable(self, editable: bool) -> None:  # noqa: N802 (Qt 命名)
        super().setEditable(editable)
        if editable and self.lineEdit() is not None:
            self.lineEdit().setFocusPolicy(Qt.ClickFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class SettingsPage(QWidget):
    """配置页：采集 AppConfig 的全部字段。"""

    changed = Signal()          # 任一配置项被修改（用于「应用」按钮高亮等）

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._forms: list[QFormLayout] = []
        self._build()

    # ---------------- 构建 ----------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 内容较长，套一层滚动区，避免小窗口下底部字段被裁掉
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        lay.addWidget(self._build_local_group())
        lay.addWidget(self._build_platform_group())
        lay.addWidget(self._build_device_group())
        lay.addWidget(self._build_reg_group())
        lay.addWidget(self._build_media_group())
        lay.addWidget(self._build_info_group())
        lay.addStretch(1)

        # 各分组各自持有 QFormLayout，标签列宽度按组内最宽标签自适应，
        # 导致输入框起始位置跨分组参差不齐。构建完成后统一取全局最宽
        # 标签，为所有标签设置最小宽度，使输入框跨分组严格左对齐。
        self._align_label_columns()

    def _align_label_columns(self) -> None:
        """统一所有分组表单的标签列宽度。"""
        max_w = 0
        for f in self._forms:
            for r in range(f.rowCount()):
                it = f.itemAt(r, QFormLayout.LabelRole)
                if it is not None and it.widget() is not None:
                    max_w = max(max_w, it.widget().sizeHint().width())
        for f in self._forms:
            for r in range(f.rowCount()):
                it = f.itemAt(r, QFormLayout.LabelRole)
                if it is not None and it.widget() is not None:
                    it.widget().setMinimumWidth(max_w)

    def _form(self, box: QGroupBox) -> QFormLayout:
        f = QFormLayout(box)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setHorizontalSpacing(14)
        f.setVerticalSpacing(8)
        f.setContentsMargins(14, 16, 14, 12)
        self._forms.append(f)
        return f

    def _build_local_group(self) -> QGroupBox:
        box = QGroupBox("本地")
        f = self._form(box)

        self.ed_local_ip = QLineEdit(self._cfg.local_ip)
        self.ed_local_ip.setPlaceholderText("留空则启动时自动选择IP")
        self.ed_local_ip.textChanged.connect(self._on_change)
        f.addRow("本机 IP", self.ed_local_ip)

        self.sp_sip_port = FocusSpinBox()
        self.sp_sip_port.setRange(1, 65535)
        self.sp_sip_port.setValue(self._cfg.local_sip_port)
        self.sp_sip_port.valueChanged.connect(self._on_change)
        f.addRow("本地 SIP 端口", self.sp_sip_port)

        self.ed_transport = QComboBox()
        self.ed_transport.addItems(["UDP"])
        self.ed_transport.setEnabled(False)      # 本期仅 UDP 信令
        f.addRow("传输协议", self.ed_transport)

        self.sp_media_port = FocusSpinBox()
        self.sp_media_port.setRange(1, 65535)
        self.sp_media_port.setValue(self._cfg.local_media_port)
        self.sp_media_port.valueChanged.connect(self._on_change)
        f.addRow("本机媒体端口", self.sp_media_port)

        return box

    def _build_platform_group(self) -> QGroupBox:
        box = QGroupBox("平台")
        f = self._form(box)

        self.ed_server_id = QLineEdit(self._cfg.server_id)
        self.ed_server_id.setMaxLength(20)
        self.ed_server_id.textChanged.connect(self._on_change)
        f.addRow("SIP 服务器 ID", self.ed_server_id)

        self.ed_server_domain = QLineEdit(self._cfg.server_domain)
        self.ed_server_domain.textChanged.connect(self._on_change)
        f.addRow("SIP 服务器域", self.ed_server_domain)

        self.ed_server_ip = QLineEdit(self._cfg.server_ip)
        self.ed_server_ip.textChanged.connect(self._on_change)
        f.addRow("SIP 服务器地址", self.ed_server_ip)

        self.sp_server_port = FocusSpinBox()
        self.sp_server_port.setRange(1, 65535)
        self.sp_server_port.setValue(self._cfg.server_port)
        self.sp_server_port.valueChanged.connect(self._on_change)
        f.addRow("SIP 服务器端口", self.sp_server_port)

        return box

    def _build_device_group(self) -> QGroupBox:
        box = QGroupBox("用户")
        f = self._form(box)

        self.ed_device_id = QLineEdit(self._cfg.device_id)
        self.ed_device_id.setMaxLength(20)
        self.ed_device_id.textChanged.connect(self._on_change)
        f.addRow("SIP 用户名", self.ed_device_id)

        self.ed_auth_user = QLineEdit(self._cfg.auth_username)
        self.ed_auth_user.setMaxLength(20)
        self.ed_auth_user.textChanged.connect(self._on_change)
        f.addRow("SIP 用户认证 ID", self.ed_auth_user)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)   # 否则容器默认边距会让输入框比纯控件行右缩 ~9px
        self.ed_password = QLineEdit(self._cfg.password)
        self.ed_password.setEchoMode(QLineEdit.Password)
        self.ed_password.textChanged.connect(self._on_change)
        row.addWidget(self.ed_password, 1)
        btn_echo = QPushButton("显示")
        btn_echo.setCheckable(True)
        btn_echo.setFixedWidth(56)
        btn_echo.toggled.connect(
            lambda on: self.ed_password.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))
        row.addWidget(btn_echo)
        wrap = QWidget()
        wrap.setLayout(row)
        f.addRow("密码", wrap)

        return box

    def _build_reg_group(self) -> QGroupBox:
        box = QGroupBox("注册 / 心跳")
        f = self._form(box)

        self.sp_expires = FocusSpinBox()
        self.sp_expires.setRange(3600, 86400)     # 国标要求 ≥3600
        self.sp_expires.setValue(self._cfg.register_expires)
        self.sp_expires.setSuffix(" 秒")
        self.sp_expires.valueChanged.connect(self._on_change)
        f.addRow("注册有效期", self.sp_expires)

        self.sp_retry = FocusSpinBox()
        self.sp_retry.setRange(60, 3600)          # 国标要求 ≥60
        self.sp_retry.setValue(self._cfg.register_retry_interval)
        self.sp_retry.setSuffix(" 秒")
        self.sp_retry.valueChanged.connect(self._on_change)
        f.addRow("注册间隔", self.sp_retry)

        self.sp_ka_interval = FocusSpinBox()
        self.sp_ka_interval.setRange(1, 3600)
        self.sp_ka_interval.setValue(self._cfg.keepalive_interval)
        self.sp_ka_interval.setSuffix(" 秒")
        self.sp_ka_interval.valueChanged.connect(self._on_change)
        f.addRow("心跳周期", self.sp_ka_interval)

        self.sp_ka_timeout = FocusSpinBox()
        self.sp_ka_timeout.setRange(1, 20)
        self.sp_ka_timeout.setValue(self._cfg.keepalive_timeout_count)
        self.sp_ka_timeout.valueChanged.connect(self._on_change)
        f.addRow("最大心跳超时次数", self.sp_ka_timeout)

        return box

    def _build_media_group(self) -> QGroupBox:
        box = QGroupBox("媒体")
        f = self._form(box)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)   # 同上：消除容器默认边距，保证左对齐
        self.cb_ps_file = FocusComboBox()
        self.cb_ps_file.setEditable(True)
        for name in self._list_ps_files():
            self.cb_ps_file.addItem(name)
        self.cb_ps_file.setCurrentText(self._cfg.ps_file)
        self.cb_ps_file.currentTextChanged.connect(self._on_change)
        row.addWidget(self.cb_ps_file, 1)

        # 保存为实例属性，使运行中可随 set_editable(False) 一并禁用；
        # 否则运行中仍能通过「浏览…」改 PS 流文件（配置已锁却留了后门）。
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setFixedWidth(72)
        self.btn_browse.clicked.connect(self._browse_ps)
        row.addWidget(self.btn_browse)

        wrap = QWidget()
        wrap.setLayout(row)
        f.addRow("PS 流文件", wrap)

        self.ck_loop = QCheckBox("循环播放")
        self.ck_loop.setChecked(self._cfg.loop_play)
        self.ck_loop.toggled.connect(self._on_change)
        f.addRow("", self.ck_loop)

        hint = QLabel("素材可换为含视频 PES 的 PS 文件；发送器会自动识别音/视频帧。")
        hint.setStyleSheet("color:#888;")
        hint.setWordWrap(True)
        f.addRow("", hint)

        return box

    def _build_info_group(self) -> QGroupBox:
        box = QGroupBox("设备信息")
        f = self._form(box)

        self.ed_name = QLineEdit(self._cfg.device_name)
        self.ed_name.textChanged.connect(self._on_change)
        f.addRow("设备名称", self.ed_name)

        self.ed_vendor = QLineEdit(self._cfg.manufacturer)
        self.ed_vendor.textChanged.connect(self._on_change)
        f.addRow("厂商", self.ed_vendor)

        self.ed_model = QLineEdit(self._cfg.model)
        self.ed_model.textChanged.connect(self._on_change)
        f.addRow("型号", self.ed_model)

        self.ed_firmware = QLineEdit(self._cfg.firmware)
        self.ed_firmware.textChanged.connect(self._on_change)
        f.addRow("固件版本", self.ed_firmware)

        self.sp_channel = FocusSpinBox()
        self.sp_channel.setRange(1, 256)
        self.sp_channel.setValue(self._cfg.channel)
        self.sp_channel.valueChanged.connect(self._on_change)
        f.addRow("通道号", self.sp_channel)

        self.ed_civil = QLineEdit(self._cfg.civil_code)
        self.ed_civil.textChanged.connect(self._on_change)
        f.addRow("行政区划码", self.ed_civil)

        return box

    # ---------------- 行为 ----------------

    def _list_ps_files(self) -> list[str]:
        out = []
        if os.path.isdir(_PS_DIR):
            for name in sorted(os.listdir(_PS_DIR)):
                if name.lower().endswith(".ps"):
                    out.append(os.path.join(_PS_REL_DIR, name).replace("\\", "/"))
        return out or [os.path.join(_PS_REL_DIR, "HK264.ps").replace("\\", "/")]

    def _browse_ps(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        # 文件对话框起始目录按优先级回退：
        #   1) 当前 PS 文件所在目录（相对路径按 USER_BASE_DIR 解析）
        #   2) resource/ps_samples（素材目录被误删/不可用时兜底）
        #   3) exe/程序根目录（素材目录本身也被误删时兜底）
        cur = self.cb_ps_file.currentText().strip()
        if cur:
            cur_abs = cur if os.path.isabs(cur) else os.path.join(USER_BASE_DIR, cur)
            start_dir = os.path.dirname(os.path.abspath(cur_abs))
        else:
            start_dir = ""
        if not start_dir or not os.path.isdir(start_dir):
            start_dir = _PS_DIR if os.path.isdir(_PS_DIR) else USER_BASE_DIR

        path, _ = QFileDialog.getOpenFileName(
            self, "选择 PS 流文件", start_dir, "PS 流 (*.ps);;所有文件 (*)")
        if path:
            self.cb_ps_file.setCurrentText(path.replace("\\", "/"))

    def _on_change(self, *_a) -> None:
        self.changed.emit()

    # ---------------- 读写 ----------------

    def read_into(self, cfg: AppConfig) -> AppConfig:
        """把界面上的值写回 cfg 并返回（就地修改）。"""
        cfg.local_ip = self.ed_local_ip.text().strip()
        cfg.local_sip_port = self.sp_sip_port.value()
        cfg.server_id = self.ed_server_id.text().strip()
        cfg.server_domain = self.ed_server_domain.text().strip()
        cfg.server_ip = self.ed_server_ip.text().strip()
        cfg.server_port = self.sp_server_port.value()
        cfg.device_id = self.ed_device_id.text().strip()
        cfg.auth_username = self.ed_auth_user.text().strip()
        cfg.password = self.ed_password.text()
        cfg.register_expires = self.sp_expires.value()
        cfg.register_retry_interval = self.sp_retry.value()
        cfg.keepalive_interval = self.sp_ka_interval.value()
        cfg.keepalive_timeout_count = self.sp_ka_timeout.value()
        cfg.local_media_port = self.sp_media_port.value()
        cfg.ps_file = self.cb_ps_file.currentText().strip()
        cfg.loop_play = self.ck_loop.isChecked()
        cfg.device_name = self.ed_name.text().strip()
        cfg.manufacturer = self.ed_vendor.text().strip()
        cfg.model = self.ed_model.text().strip()
        cfg.firmware = self.ed_firmware.text().strip()
        cfg.channel = self.sp_channel.value()
        cfg.civil_code = self.ed_civil.text().strip()
        return cfg

    def write_from(self, cfg: AppConfig) -> None:
        """把 cfg 的值刷到界面（加载配置后调用）。"""
        self._cfg = cfg
        self.ed_local_ip.setText(cfg.local_ip)
        self.sp_sip_port.setValue(cfg.local_sip_port)
        self.ed_server_id.setText(cfg.server_id)
        self.ed_server_domain.setText(cfg.server_domain)
        self.ed_server_ip.setText(cfg.server_ip)
        self.sp_server_port.setValue(cfg.server_port)
        self.ed_device_id.setText(cfg.device_id)
        self.ed_auth_user.setText(cfg.auth_username)
        self.ed_password.setText(cfg.password)
        self.sp_expires.setValue(cfg.register_expires)
        self.sp_retry.setValue(cfg.register_retry_interval)
        self.sp_ka_interval.setValue(cfg.keepalive_interval)
        self.sp_ka_timeout.setValue(cfg.keepalive_timeout_count)
        self.sp_media_port.setValue(cfg.local_media_port)
        self.cb_ps_file.setCurrentText(cfg.ps_file)
        self.ck_loop.setChecked(cfg.loop_play)
        self.ed_name.setText(cfg.device_name)
        self.ed_vendor.setText(cfg.manufacturer)
        self.ed_model.setText(cfg.model)
        self.ed_firmware.setText(cfg.firmware)
        self.sp_channel.setValue(cfg.channel)
        self.ed_civil.setText(cfg.civil_code)

    def set_editable(self, ok: bool) -> None:
        """设备运行中禁止改配置，避免改到一半状态不一致。"""
        for w in (self.ed_local_ip, self.sp_sip_port, self.sp_media_port,
                  self.ed_server_id, self.ed_server_domain, self.ed_server_ip,
                  self.sp_server_port, self.ed_device_id, self.ed_auth_user,
                  self.ed_password, self.sp_expires, self.sp_retry,
                  self.sp_ka_interval, self.sp_ka_timeout, self.cb_ps_file,
                  self.btn_browse,
                  self.ck_loop, self.ed_name, self.ed_vendor, self.ed_model,
                  self.ed_firmware, self.sp_channel, self.ed_civil):
            w.setEnabled(ok)
