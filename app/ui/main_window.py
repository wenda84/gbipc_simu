"""主窗口：设置页 / 状态页 / 日志页 + 启停控制。

与业务层通过 Qt 信号槽通信：GbDevice 的 DeviceEvents 回调运行在 worker 线程，
一律经 signal 转到 UI 线程再更新控件，避免跨线程直接操作 Qt 对象。
"""
from __future__ import annotations

import os
import threading
import traceback

from PySide6.QtCore import (
    QEvent, QObject, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QFontMetrics, QIcon, QPainter, QPalette, QPen, QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QCheckBox, QApplication, QLabel, QMainWindow, QMenu, QMessageBox,
    QStatusBar, QStyleOptionToolButton, QSystemTrayIcon, QTabWidget, QToolBar,
    QToolButton,
    QHBoxLayout, QWidget, QStyle,
)

from ..core.config import (
    AppConfig, DEFAULT_CONFIG_PATH, load_config, save_config,
)
from ..core.app_icon import resolve_app_icon
from ..core.applog import log_event
# 注意：LEGACY_IMPORTED_FROM 是 load_config() 里用 global 改动的可变状态，
# 必须经模块访问，直接 from-import 只会拿到导入时的空字符串快照。
from ..core import config as _config_mod
from ..core.device import DeviceEvents, GbDevice, RegState
from .settings_page import SettingsPage
from .status_page import StatusPage
from .log_page import LogPage
from .volume_meter import MODE_ACTIVE, MODE_ARMED, MODE_OFF, VolumeMeter

APP_TITLE = "GB28181 IPC 模拟工具"


# 工具栏图标颜色：黑白单色（禁用态由 Qt 自动淡化，不需要另配一套）
ICON_COLOR = "#303133"


def _mono_icon(kind: str, side: int) -> QIcon:
    """给「启动 / 停止 / 保存」绘制黑白单色图标。

    ``side`` = 图标边长（逻辑像素），调用方按「文字墨迹高度 × 1.2」计算，
    保证图标略高于文字、又不会顶破按钮高度。

    kind: ``"play"`` 实心三角 / ``"stop"`` 实心方块 / ``"save"`` 软盘线框。
    统一按 4 倍尺寸绘制再 ``setDevicePixelRatio(4)``，高 DPI 下不发糊；
    三种形状的「视觉重量」做过配平（三角比方块略大才显得一样大）。
    """
    scale = 4
    pm = QPixmap(side * scale, side * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(scale, scale)
    col = QColor(ICON_COLOR)
    s = float(side)

    if kind == "play":                      # ▶ 实心三角
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawPolygon(QPolygonF([QPointF(s * 0.26, s * 0.16),
                                 QPointF(s * 0.26, s * 0.84),
                                 QPointF(s * 0.82, s * 0.50)]))
    elif kind == "stop":                    # ■ 实心圆角方块
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(s * 0.21, s * 0.21, s * 0.58, s * 0.58),
                          s * 0.06, s * 0.06)
    else:                                   # 软盘：线框盘体 + 顶部滑片 + 底部标签
        # 盘体用圆角方形而**不做右上切角**：切角 + 顶部滑片在小尺寸下会被
        # 误读成「卷角的文档」，方形盘体反而最像 3.5 寸软盘。
        w = max(1.2, s * 0.078)
        pen = QPen(col, w)
        pen.setJoinStyle(Qt.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(s * 0.14, s * 0.14, s * 0.72, s * 0.72),
                          s * 0.075, s * 0.075)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        # 滑片与标签都与盘体边框齐平（贴边融合），像真软盘那样连成一体
        p.drawRect(QRectF(s * 0.42, s * 0.14, s * 0.20, s * 0.22))   # 滑片
        p.drawRect(QRectF(s * 0.26, s * 0.64, s * 0.48, s * 0.22))   # 标签
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


class CenterToolButton(QToolButton):
    """内容严格居中的工具按钮。

    QToolButton 在「按钮宽度 > 内容宽度」时会把「图标 + 文字」整块贴在左边
    （Qt 的 CE_ToolButtonLabel 只在 ToolButtonTextOnly 时居中），三个按钮
    统一等宽后文字就都偏左了。这里自绘：背景仍交给当前样式绘制
    （hover / 按下 / 禁用底板与普通工具按钮完全一致），只有内容自己算位置，
    保证水平居中 + 垂直居中，与按钮多宽无关。
    """

    ICON_TEXT_GAP = 4

    def paintEvent(self, ev) -> None:                      # noqa: N802 (Qt 命名)
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)

        p = QPainter(self)
        # 1) 只画底板：清掉文字/图标/箭头，仍走 CC_ToolButton，
        #    这样 hover、按下、禁用、焦点框的表现与系统一致。
        opt.text = ""
        opt.icon = QIcon()
        opt.arrowType = Qt.NoArrow
        self.style().drawComplexControl(QStyle.CC_ToolButton, opt, p, self)

        # 2) 内容居中
        icon = self.icon()
        side = self.iconSize().width() if not icon.isNull() else 0
        tw = self.fontMetrics().horizontalAdvance(self.text())
        gap = self.ICON_TEXT_GAP if (side and tw) else 0
        total = side + gap + tw

        x = QRect(0, 0, self.width(), self.height()).x() + int(round((self.width() - total) / 2))
        if side:
            pm = icon.pixmap(self.iconSize(), self.devicePixelRatioF())
            h = self.iconSize().height()
            if not self.isEnabled():
                p.setOpacity(0.4)                # 禁用态图标淡化（文字由样式着色）
            p.drawPixmap(QPointF(x, (self.height() - h) / 2.0), pm)
            p.setOpacity(1.0)
            x += side + gap

        if tw:
            self.style().drawItemText(
                p, QRect(x, 0, tw, self.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                self.palette(), self.isEnabled(), self.text(), QPalette.ButtonText)
        p.end()


class DeviceBridge(QObject, DeviceEvents):
    """把 DeviceEvents（worker 线程）转为 Qt 信号（UI 线程）。"""

    sig_log = Signal(str)
    sig_reg_state = Signal(object, str)
    sig_call_state = Signal(str)
    sig_stream_stats = Signal(str)
    sig_broadcast_state = Signal(str)
    sig_broadcast_level = Signal(float, float)
    sig_broadcast_stream = Signal(str)
    # 「停止完成」由后台线程 emit；Qt 会把跨线程信号投递到本对象所属线程
    # （UI 线程）执行槽函数，从而安全更新控件。
    # 参数：(错误文本, 是否彻底停止)。第二参 False 表示 dev.stop() 超时返回、
    # 旧协议栈仍在后台销毁，此时绝不能恢复可启动状态（进程级单实例竞态）。
    sig_stopped = Signal(str, bool)

    def on_log(self, msg: str) -> None:
        self.sig_log.emit(msg)

    def on_reg_state(self, state, detail: str = "") -> None:
        self.sig_reg_state.emit(state, detail)

    def on_call_state(self, text: str) -> None:
        self.sig_call_state.emit(text)

    def on_stream_stats(self, text: str) -> None:
        self.sig_stream_stats.emit(text)

    def on_broadcast_state(self, text: str) -> None:
        self.sig_broadcast_state.emit(text)

    def on_broadcast_level(self, level: float, peak: float) -> None:
        self.sig_broadcast_level.emit(level, peak)

    def on_broadcast_stream(self, text: str) -> None:
        self.sig_broadcast_stream.emit(text)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        # 显式设置窗口图标（双保险）：覆盖标题栏与任务栏运行按钮。
        # QApplication 已设过，这里再设一次以避免个别 Qt/Windows 组合下不继承。
        _ic = resolve_app_icon()
        if _ic:
            self.setWindowIcon(QIcon(_ic))
        self.resize(940, 720)

        # 系统托盘：启动即显示，最小化收起、双击恢复、右键菜单退出
        self._build_tray()

        self.cfg: AppConfig = load_config()
        self.device: GbDevice | None = None
        self.bridge = DeviceBridge()
        self._dirty = False
        # 停止中标记：dev.stop() 尚未返回（旧 pjsua2 Lib 的 libDestroy 仍在进行）。
        # 期间启动按钮保持禁用——pjsua2 Lib 是进程级单实例，旧 Lib 未销毁前新建
        # 会触发竞态崩溃。因此「未注册 + 启动可点」严格等价于「旧 Lib 已彻底销毁、
        # 可安全重启」，状态机无矛盾（见 _set_stopping / _finish_stop）。
        self._stopping = False
        # 恢复窗口时临时抑制「最小化→收起」逻辑，避免 showNormal 触发的
        # WindowStateChange 又被 changeEvent 误判为最小化而重新隐藏窗口。
        self._suppress_min_hide = False
        # 真正退出标记：仅托盘菜单「退出」会置 True；closeEvent 据此区分
        # 「用户点 X（收起托盘）」与「应用正在退出（允许关闭）」。
        self._real_quit = False
        # 运行看门狗：worker 是业务日志/心跳的唯一驱动，若其死亡或阻塞，
        # UI 没有自动途径感知（「停止」只能由用户触发），会一直显示运行中。
        self._wd_timer = QTimer(self)
        self._wd_timer.setInterval(5000)
        self._wd_timer.timeout.connect(self._watchdog_check)
        self._wd_alerting = False

        self._build_ui()
        self._wire()

        self._set_running(False)
        self.log.append("[ui] 就绪。填写「平台接入」参数后点击「启动」。")
        if _config_mod.LEGACY_IMPORTED_FROM:
            self.log.append("[ui] 已从旧位置导入已有配置: "
                            f"{_config_mod.LEGACY_IMPORTED_FROM}")
            self.log.append("[ui] 之后「保存配置」会写入上面的新位置，"
                            "整个目录一起拷贝即为便携部署。")

    # ---------------- 系统托盘 ----------------

    def _build_tray(self) -> None:
        """创建系统托盘图标（与 exe 同款 `ipc.ico`/`ipc.png`）。

        行为约定：
          - 应用启动即显示托盘图标；
          - 标题栏「最小化」→ 收起到托盘，任务栏不留存该应用；
          - 双击托盘图标 → 恢复主窗口到原尺寸/位置；
          - 右键托盘图标 → 弹出菜单（当前仅「退出」）→ 退出应用。
        """
        self.tray = QSystemTrayIcon(QIcon(resolve_app_icon() or ""), self)
        self.tray.setToolTip(APP_TITLE)

        menu = QMenu(self)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._request_quit)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)

        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """托盘图标交互：双击恢复主窗口。"""
        try:
            reason_name = QSystemTrayIcon.ActivationReason(reason).name
        except Exception:
            reason_name = str(int(reason))
        log_event(f"tray activated: {reason_name}")
        if reason == QSystemTrayIcon.DoubleClick:
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        """从托盘恢复主窗口（保持原窗口大小/位置）。"""
        log_event("tray restore begin")
        self._suppress_min_hide = True
        try:
            self.showNormal()
            self.raise_()
        except Exception as e:  # 兜底：任何异常都记录下来而非静默崩退
            log_event(f"tray restore ERROR: {e!r}")
            raise
        finally:
            self._suppress_min_hide = False
        # 延后到下一轮事件循环再请求激活前台：从托盘回调直接调用
        # SetForegroundWindow 在 Windows 前景锁限制下既可能被忽略，也可能
        # 在个别环境下触发原生层崩溃；放到 singleShot(0) 脱离托盘回调调用栈
        # 更稳妥。即使激活被系统忽略（窗口未抢到焦点），也不影响可见性。
        QTimer.singleShot(0, self.activateWindow)
        log_event("tray restore end")

    def _really_quit(self) -> None:
        """真正退出应用：运行中先停设备再退出，空闲直接退出。

        同时被「标题栏 X」与「托盘菜单→退出」调用，保证两条退出入口行为一致。
        """
        log_event("app quit requested")
        self._real_quit = True
        if self.device is not None:
            self.hide()
            self._wd_timer.stop()
            self._wd_alerting = False
            self._quit_after_stop = True
            self._on_stop()
        else:
            QApplication.quit()

    def bring_to_front(self) -> None:
        """被同路径的第二个实例唤醒时，将窗口恢复到前台。

        兼容三种状态：正常可见、最小化、收起托盘（隐藏）均恢复并置顶。
        """
        from app.core.applog import log_event
        log_event("bring to front (second instance)")
        self._suppress_min_hide = True
        try:
            self.showNormal()
            self.raise_()
        finally:
            self._suppress_min_hide = False
        # 延后到下一轮事件循环再激活前台，规避 Windows 前景锁下的潜在冲突
        QTimer.singleShot(0, self.activateWindow)

    def _request_quit(self) -> None:
        """托盘菜单「退出」：关闭应用（运行中先停设备再退）。"""
        self._really_quit()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.settings = SettingsPage(self.cfg)
        self.status = StatusPage()
        self.log = LogPage()
        self.tabs.addTab(self.settings, "参数配置")
        self.tabs.addTab(self.status, "运行状态")
        self.tabs.addTab(self.log, "信令日志")
        self.setCentralWidget(self.tabs)

        # 顶部工具条：启停 + 配置存取
        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        # 三个按钮都是「符号 + 文字」，保持 TextBesideIcon 以防某些主题把
        # 仅有文字的按钮画成纯图标。
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        # 放大「启动 / 停止 / 保存」按钮（约 2 倍可点击面积）：
        # 工具栏内仅此三个动作，统一通过样式表放大高度、字体与内边距。
        # 宽度不用 min-width 定——三个按钮文字长度不同（启动 / 停止 / 保存），
        # 靠 min-width 兜底只能保证「不窄于」，内容更宽者仍会更宽。
        # 宽度交给 _equalize_tool_buttons 按最大 sizeHint 统一 setFixedSize。
        tb.setStyleSheet(
            "QToolButton {"
            "  min-height: 44px;"
            "  font-size: 15px;"
            "  font-weight: 600;"
            "  padding: 4px 14px;"
            "}"
            "QCheckBox {"
            "  font-size: 14px;"
            "  padding: 0 4px 0 10px;"
            "}"
        )
        self.addToolBar(tb)
        self.toolbar = tb

        # 三个按钮统一为「黑白图标 + 文字」：图标由 _apply_tool_icons 按
        # 「文字墨迹高度 × 1.2」绘制（见 _mono_icon）。
        # 按钮用 CenterToolButton（而不是 tb.addAction 生成的 QToolButton），
        # 否则按钮被拉宽后内容会贴左、不居中。
        self.act_start = QAction("启动", self)
        self.act_start.triggered.connect(self._on_start)
        self.btn_start = self._add_tool_button(tb, self.act_start)

        self.act_stop = QAction("停止", self)
        self.act_stop.triggered.connect(self._on_stop)
        self.btn_stop = self._add_tool_button(tb, self.act_stop)

        tb.addSeparator()

        # 「保存」：悬停提示仍为「保存配置」。默认灰化（未改动配置时无可保存），
        # 改动后自动亮起，保存完成再灰化。
        self.act_save = QAction("保存", self)
        self.act_save.setToolTip("保存配置")
        self.act_save.setStatusTip("保存配置到 config/app_config.toml")
        self.act_save.setEnabled(False)
        self.act_save.triggered.connect(self._on_save)
        self.btn_save = self._add_tool_button(tb, self.act_save)

        self.tool_buttons = [self.btn_start, self.btn_stop, self.btn_save]

        # 图标尺寸依赖最终字体（样式表 15px），故先建动作、再按 polish 后的
        # 字体算图标边长并统一按钮尺寸。
        self._apply_tool_icons()

        # ---- 国标广播：勾选框 + 音量提示框（位于「保存配置」右侧）----
        tb.addSeparator()
        self.ck_broadcast = QCheckBox("国标广播")
        self.ck_broadcast.setChecked(bool(self.cfg.broadcast_enabled))
        self.ck_broadcast.setToolTip(
            "勾选后支持国标语音广播（作为语音输出设备接收并播放 PCMA 语音）。")
        self.ck_broadcast.toggled.connect(self._on_broadcast_toggled)
        tb.addWidget(self.ck_broadcast)

        self.vu = VolumeMeter(segments=8)
        self.vu.set_mode(MODE_ARMED if self.cfg.broadcast_enabled else MODE_OFF)
        # 音量框与「国标广播」之间的间隔 = 当前工具栏默认间隔的 2 倍：
        # 用带左侧间距的容器包裹，工具栏默认间隔 S 作用在「勾选框↔容器」之间，
        # 容器内再留 S 的左间距，总间隔 ≈ S × 2。部分风格 PM_ToolBarItemSpacing
        # 返回 0，给一个基础值兜底，保证间隔可见。
        gap = self.style().pixelMetric(QStyle.PM_ToolBarItemSpacing)
        if gap <= 0:
            gap = 4
        vu_wrap = QWidget()
        vu_lay = QHBoxLayout(vu_wrap)
        vu_lay.setContentsMargins(gap, 0, 0, 0)
        vu_lay.setSpacing(0)
        vu_lay.addWidget(self.vu)
        tb.addWidget(vu_wrap)

        # 状态栏：左侧常驻显示当前可执行文件的完整路径，右侧显示注册状态
        self.sb = QStatusBar()
        self.setStatusBar(self.sb)
        self.lb_sb_path = QLabel(self._exe_path_text())
        self.lb_sb_path.setToolTip(self._exe_path_text())
        self.lb_sb_path.setStyleSheet("color:#888;")
        self.sb.addWidget(self.lb_sb_path, 1)
        self.lb_sb_state = QLabel("未注册")
        self.sb.addPermanentWidget(self.lb_sb_state)

    def _wire(self) -> None:
        self.bridge.sig_log.connect(self.log.append)
        self.bridge.sig_reg_state.connect(self._on_reg_state)
        self.bridge.sig_call_state.connect(self._on_call_state)
        self.bridge.sig_stream_stats.connect(self.status.set_stream_stat)
        self.bridge.sig_broadcast_state.connect(self._on_broadcast_state)
        self.bridge.sig_broadcast_level.connect(self._on_broadcast_level)
        self.bridge.sig_broadcast_stream.connect(self.status.set_broadcast_stream)
        self.bridge.sig_stopped.connect(self._finish_stop)
        self.settings.changed.connect(self._mark_dirty)
        # 未运行时，本机地址随「平台接入」页编辑实时刷新
        self.settings.changed.connect(self._refresh_idle_local_addr)

    # ---------------- 启停 ----------------

    @staticmethod
    def _exc_text(e: BaseException) -> str:
        """把异常转成适合弹窗/日志的文本。

        pjsua2 的 pj.Error（SWIG 包装）str(e) 是空串（args=()），详细信息
        在 info() / title / reason / status 里，直接 str(e) 会弹出空白框。
        """
        info = getattr(e, "info", None)
        if callable(info):
            try:
                text = str(info()).strip()
                if text:
                    return text
            except Exception:
                pass
        text = str(e).strip()
        if text:
            return text
        return f"{type(e).__name__}（无异常详情）"

    def _on_start(self) -> None:
        # 防御：停止流程（旧协议栈 libDestroy 仍在进行）期间启动按钮处于禁用态，
        # 此处仅在被异常路径触发时兜底返回。绝不延后、绝不自动重启——「未注册 +
        # 启动可点」只在 _finish_stop 之后出现，届时旧 Lib 已销毁，可安全启动。
        if self._stopping or self.device is not None:
            return
        self.settings.read_into(self.cfg)
        err = self._validate(self.cfg)
        if err:
            QMessageBox.warning(self, "参数有误", err)
            self.tabs.setCurrentWidget(self.settings)
            return

        # 启动即保存：把当前界面上的配置落盘（相当于自动点一次「保存」）
        self._on_save(auto=True)

        self.log.append("=" * 60)
        self.log.append("[ui] 启动设备…")
        self.vu.reset()
        try:
            self.device = GbDevice(self.cfg, self.bridge)
            self.device.start()
        except Exception as e:
            self.device = None
            self.log.append(f"[ui] 启动失败: {self._exc_text(e)}")
            self.log.append(traceback.format_exc())
            QMessageBox.critical(self, "启动失败", self._exc_text(e))
            return

        self._set_running(True)
        self.status.set_device_info(
            self.cfg.device_id,
            self._local_addr_text(running=True),
            f"{self.cfg.server_ip}:{self.cfg.server_port}",
        )

    def _on_stop(self) -> None:
        """停止设备。

        GbDevice.stop() 是阻塞的：worker 内部要完成 teardown（挂断呼叫排空、
        注销 REGISTER 事务、销毁协议栈 libDestroy）再返回，外层 join 最多等 20 秒。
        若直接在 UI 线程调用会导致窗口假死，因此放到后台线程执行；结果经跨线程
        Signal (sig_stopped) 回到 UI 线程的 _finish_stop 收尾。

        注意：结果回调必须用跨线程 Signal，切勿用 ``QTimer.singleShot(0, cb)`` ——
        无 receiver 时它会被投递到**调用线程**（普通 threading.Thread 无事件循环），
        回调将永不触发（表现为界面永久停留「停止中」）。最小复现见
        tests/probe_qt_singleShot_thread.py。
        """
        dev = self.device
        if dev is None:
            return
        self.device = None               # 先摘引用，避免停止期间误触发其它操作
        self._stopping = True            # 进入停止中（旧协议栈 libDestroy 仍在进行）
        # 立即把界面切到「停止中」忙碌态：禁用启动/停止按钮与参数编辑，状态栏
        # 显式「停止中」。这是状态机的关键不变量——「未注册 + 启动可点」只在
        # _finish_stop（dev.stop() 已返回、旧 Lib 已销毁）时才出现，杜绝
        # 「界面显示已停止却点不了启动」的矛盾，也彻底移除延后/自动重启逻辑。
        self._set_stopping()
        # 立即停止看门狗：停止是预期内的主动行为，teardown 期间 worker 不再走
        # 主循环、tick 不更新，看门狗若仍运行会误报「线程阻塞」。
        self._wd_timer.stop()
        self.log.append("[ui] 停止设备…")

        def _worker() -> None:
            err = ""
            clean = True
            try:
                clean = dev.stop()
            except Exception as e:      # pragma: no cover - 仅记录
                err = self._exc_text(e)
                clean = False
            # 跨线程 emit：槽函数 _finish_stop 在 UI 线程执行
            self.bridge.sig_stopped.emit(err, clean)

        threading.Thread(target=_worker, name="ui-stop", daemon=True).start()

    def _set_stopping(self) -> None:
        """进入「停止中」：旧协议栈 libDestroy 仍在进行，界面进入忙碌态。

        关键不变量：启动按钮在此阶段**禁用**。pjsua2 的 Lib 是进程级单实例，
        旧 Lib 未销毁前新建 Lib 会触发竞态崩溃；因此「启动可点」严格等价于
        「旧 Lib 已彻底销毁、可安全重启」。状态栏显示「停止中」而非「未注册」，
        既如实反映进度，也避免用户误以为可以立即启动。
        """
        self.act_start.setEnabled(False)
        self.act_stop.setEnabled(False)
        self.act_save.setEnabled(False)
        self.settings.set_editable(False)
        self.ck_broadcast.setEnabled(False)
        self.vu.reset()
        self.lb_sb_state.setText("停止中")
        self.lb_sb_state.setStyleSheet("color:#909399;font-weight:600;")

    def _finish_stop(self, err: str = "", clean: bool = True) -> None:
        """UI 线程收尾停止流程。

        dev.stop() 正常返回（clean=True）即代表旧协议栈（libDestroy）已彻底
        销毁，单实例竞态风险解除。此刻才把界面从「停止中」翻转为「未注册」
        并重新使能启动按钮——两者永远同步，状态机无矛盾。若停止由退出流程
        触发，则直接退出应用。

        clean=False 表示 dev.stop() 超时返回：worker 仍在后台销毁协议栈。
        pjsua2 的 Lib 是进程级单实例，此时新建协议栈会竞态崩溃，因此**绝不**
        恢复可启动状态，界面保持禁用并提示重启本程序。
        """
        self._stopping = False
        # 先落日志再分支：早期实现把退出分支放在最前，导致「运行中关窗退出」
        # 时的停止异常被整条吞掉，用户永远看不到。
        if err:
            self.log.append(f"[ui] 停止时异常: {err}")
        if not clean:
            self.log.append("[ui] 停止超时：旧协议栈未完成销毁，为保证安全"
                            "不再允许启动，请重启本程序")
            self.lb_sb_state.setText("停止超时")
            self.lb_sb_state.setStyleSheet("color:#f56c6c;font-weight:600;")
            self.act_start.setEnabled(False)
            self.act_stop.setEnabled(False)
            # 退出流程中遇此情况仍要退出：进程结束会随 daemon 线程一并终止。
            if getattr(self, "_quit_after_stop", False):
                self._quit_after_stop = False
                QApplication.quit()
                return
            QMessageBox.warning(self, "停止未完成",
                                "设备停止超时，协议栈未能完成销毁。\n"
                                "为避免崩溃，本程序需重启后才能再次启动设备。")
            return
        self.log.append("[ui] 已停止")
        # 运行中关窗触发的停止：完成后真正退出应用（见 closeEvent）
        if getattr(self, "_quit_after_stop", False):
            self._quit_after_stop = False
            QApplication.quit()
            return
        self.status.reset()
        self._set_running(False)

    def _local_addr_text(self, running: bool) -> str:
        """「本机地址」显示文本。

        运行中：显示实际生效的本机 IP（配置留空时即为自动探测结果，见
        GbDevice.start()）。
        未启动：显示「平台接入」页当前填写的本机 IP 与端口（含未保存的编辑）；
        本机 IP 留空时显示「(自动探测)」。
        """
        if running and self.device is not None and self.device.local_ip:
            return f"{self.device.local_ip}:{self.device.local_sip_port}"
        ip = self.settings.ed_local_ip.text().strip()
        port = self.settings._port_value(self.settings.sp_sip_port)
        return f"{ip or '(自动探测)'}:{port or '(自动选择)'}"

    def _refresh_idle_local_addr(self) -> None:
        """未运行时，「本机地址」随「平台接入」页编辑实时刷新。"""
        if self.device is None:
            self.status.set_local_addr(self._local_addr_text(running=False))

    def _set_running(self, running: bool) -> None:
        self.act_start.setEnabled(not running)
        self.act_stop.setEnabled(running)
        # 运行中配置已锁定，保存按钮同步灰化；未运行时按「是否有改动」决定
        self.act_save.setEnabled((not running) and self._dirty)
        self.settings.set_editable(not running)
        # 「国标广播」是配置项，同样只在停止状态可改；运行中灰化。
        self.ck_broadcast.setEnabled(not running)
        self.vu.reset()
        self._update_vu_mode()
        if running:
            # 启动成功仅代表设备进程已拉起并进入「注册中」；真正的「在线」
            # 必须等服务端回 200 OK（见 _on_reg_state 的 ONLINE 分支）。此处
            # 绝不能写成「在线」，否则服务端不可达时也会误报在线（需求 1）。
            self.lb_sb_state.setText("注册中")
            self.lb_sb_state.setStyleSheet("color:#e6a23c;font-weight:600;")
            self._wd_alerting = False
            self._wd_timer.start()
        else:
            self.lb_sb_state.setText("未注册")
            self.lb_sb_state.setStyleSheet("color:#888;font-weight:600;")
            self._wd_timer.stop()
            # 停止/未启动时，广播状态由配置开关决定：勾选=空闲，未勾选=未启用
            self.status.set_broadcast_state(
                "空闲" if self.cfg.broadcast_enabled else "未启用")
            # 未启动时「本机地址」反映配置值；配置留空则显示「(自动探测)」。
            self.status.set_local_addr(self._local_addr_text(running=False))

    # ---------------- 运行看门狗 ----------------

    # worker 单次迭代正常 <1s；重事件（PS 文件分析、注册 EBUSY 退避）
    # 可达数秒，取宽裕阈值防误报。
    _WD_STALL_SECONDS = 30.0

    def _watchdog_check(self) -> None:
        """周期巡检设备 worker：死亡或长时间无迭代即显式告警。"""
        dev = self.device
        if dev is None:
            return
        if not dev.worker_alive():
            self._wd_alert(
                "设备工作线程已异常退出，信令与心跳停滞。"
                "请点「停止」复位界面，然后重启本程序。")
            return
        age = dev.tick_age()
        if age > self._WD_STALL_SECONDS:
            self._wd_alert(
                f"设备工作线程已 {age:.0f}s 未处理任何事件（疑似阻塞），"
                "信令与心跳停滞。可尝试点「停止」强制复位（最长约 20s），"
                "无效请重启本程序。")
            return
        if self._wd_alerting:
            # 曾告警、现已恢复：解除状态栏提示
            self._wd_alerting = False
            self.sb.clearMessage()

    def _wd_alert(self, msg: str) -> None:
        self.log.append(f"[ui] 检测异常: {msg}")
        self.sb.showMessage(msg)
        if not self._wd_alerting:
            self._wd_alerting = True
            QMessageBox.warning(self, "设备运行异常", msg)

    # ---------------- 国标广播 ----------------

    def _update_vu_mode(self, bc_text: str = "") -> None:
        """根据勾选状态与广播呼叫状态刷新音量框模式。"""
        if not self.cfg.broadcast_enabled:
            self.vu.set_mode(MODE_OFF)
            return
        # 呼叫已发起（呼叫中）或已建立（广播中）即进入活跃态：底色转绿、按实际音量
        # 点亮亮绿；仅「勾选但未发起呼叫」（空闲）时保持暗绿待机。
        if "呼叫中" in bc_text or "广播中" in bc_text:
            self.vu.set_mode(MODE_ACTIVE)
        else:
            self.vu.set_mode(MODE_ARMED)

    def _on_broadcast_state(self, text: str) -> None:
        """广播状态变化：同步刷新状态页与音量框模式。"""
        self.status.set_broadcast_state(text)
        self._update_vu_mode(text or "")

    def _on_broadcast_toggled(self, checked: bool) -> None:
        """勾选后启用广播能力，音量框切换为暗绿色待机；取消则回到灰色。

        与其它配置项一致：仅写入内存配置并标记「未保存」，由「保存配置」落盘。
        """
        self.cfg.broadcast_enabled = bool(checked)
        self._update_vu_mode()
        self.vu.reset()
        self._mark_dirty()
        # 未运行时，广播状态随开关实时变化：勾选=空闲，未勾选=未启用
        if self.device is None:
            self.status.set_broadcast_state("空闲" if checked else "未启用")
        self.log.append(f"[ui] 国标广播 {'已启用' if checked else '已关闭'}"
                        "（启动后生效；勾选状态需「保存配置」才会持久化）")

    def _on_broadcast_level(self, level: float, peak: float) -> None:
        self.vu.set_level(level, peak)

    # ---------------- 事件 ----------------

    def changeEvent(self, event) -> None:
        """标题栏「最小化」→ 收起到托盘（任务栏不显示该应用）。"""
        if (event.type() == QEvent.WindowStateChange
                and self.isMinimized()
                and not self._suppress_min_hide):
            log_event("window minimized -> hide to tray")
            self.hide()
        super().changeEvent(event)

    def showEvent(self, event) -> None:
        """首次显示后再校准一次工具栏按钮尺寸。

        样式表/字体在 show 之后由 Qt 完成 polish，个别主题（或换用不同 DPI 的
        屏幕时）sizeHint 会与构造期不同；再取一次最大 sizeHint 重设固定宽高，
        保证三个按钮在任何环境下都严格等宽等高。
        """
        super().showEvent(event)
        if getattr(self, "_tb_sized_once", False):
            return
        self._tb_sized_once = True
        # show 之后 Qt 才完成样式表/字体 polish，此时再按真实字体重画图标、
        # 重设固定尺寸，保证不同 DPI/主题下图标高度与按钮尺寸一致。
        self._apply_tool_icons()

    def _on_reg_state(self, state: RegState, detail: str) -> None:
        # 状态页仍反映设备侧注册事件（无害）；但停止流程中状态栏保持「停止中」，
        # 不随即将销毁的设备的注册态跳动，避免与「未注册=可启动」的不变量冲突。
        self.status.set_reg_state(state, detail)
        if self._stopping:
            return
        self.lb_sb_state.setText(state.value)
        color = {"在线": "#67c23a", "注册中": "#e6a23c",
                 "重试中": "#f56c6c"}.get(state.value, "#888")
        self.lb_sb_state.setStyleSheet(f"color:{color};font-weight:600;")
        if state == RegState.ONLINE:
            self.status.set_keepalive("正常")
        elif "心跳超时" in (detail or ""):
            # 心跳超时判离线时，状态页不能仍显示「正常」
            self.status.set_keepalive("超时")

    def _on_call_state(self, text: str) -> None:
        self.status.set_call_state(text)

    # ---------------- 配置 ----------------

    def _mark_dirty(self) -> None:
        self._dirty = True
        # 有改动才允许保存；运行中配置锁定（set_editable(False) 已拦住编辑），
        # 此时若仍是脏状态则保存按钮保持可用，允许把改动落盘。
        if self.device is None:
            self.act_save.setEnabled(True)

    def _on_save(self, *_a, auto: bool = False) -> None:
        self.settings.read_into(self.cfg)
        try:
            save_config(self.cfg, DEFAULT_CONFIG_PATH)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", self._exc_text(e))
            return
        self._dirty = False
        self.act_save.setEnabled(False)      # 保存完成 → 回到灰化态
        self.log.append("[ui] 配置已自动保存" if auto else "[ui] 配置已保存")
        self.sb.showMessage("配置已保存", 3000)

    @staticmethod
    def _exe_path_text() -> str:
        """状态栏展示的可执行文件完整路径。

        打包态 = exe 自身路径；源码态 = 启动脚本（app/__main__.py 或 main.py）
        的绝对路径，保证两种运行方式都有意义、且都是「完整路径」。
        """
        import sys
        if getattr(sys, "frozen", False):
            return os.path.abspath(sys.executable)
        # 源码态：优先显示实际启动脚本；用 `-c` 等方式启动时 argv[0] 不是
        # 真实文件，退回解释器路径，避免状态栏出现 "-c" 这类无意义内容。
        entry = sys.argv[0] if sys.argv else ""
        if entry and os.path.isfile(entry):
            return os.path.abspath(entry)
        return os.path.abspath(sys.executable)

    @staticmethod
    def _add_tool_button(tb: QToolBar, act: QAction) -> "CenterToolButton":
        """把动作包装成自绘居中按钮加进工具栏。

        用 ``QToolBar.addWidget``（而非 ``addAction``），才能指定按钮类；
        按钮通过 ``setDefaultAction`` 跟随动作的文字/图标/使能/tooltip。
        """
        btn = CenterToolButton()
        btn.setDefaultAction(act)
        tb.addWidget(btn)
        return btn

    def _apply_tool_icons(self) -> None:
        """给「启动 / 停止 / 保存」装黑白图标，并统一三个按钮尺寸。

        图标边长 = **文字墨迹高度 × 1.2**（``QFontMetrics.tightBoundingRect``
        取汉字实际墨迹高度，而不是含行距的 ``height()``，否则图标会明显偏大）。
        字号由样式表给出，所以必须在按钮 polish 之后取 ``btn.font()``。
        """
        tb = getattr(self, "toolbar", None)
        if tb is None:
            return
        acts = (self.act_start, self.act_stop, self.act_save)
        btns = [b for b in getattr(self, "tool_buttons", [])
                if isinstance(b, QToolButton)]
        if len(btns) != 3:
            return
        ref = btns[0]
        ref.ensurePolished()
        fm = QFontMetrics(ref.font())
        ink = fm.tightBoundingRect("启").height()
        if ink <= 0:                        # 字体度量异常时退回 ascent
            ink = fm.ascent()
        side = max(12, int(round(ink * 1.2)))
        for act, kind in zip(acts, ("play", "stop", "save")):
            act.setIcon(_mono_icon(kind, side))     # 按钮经 defaultAction 自动同步
        tb.setIconSize(QSize(side, side))
        self._equalize_tool_buttons(btns)

    @staticmethod
    def _equalize_tool_buttons(buttons, min_w: int = 120) -> None:
        """把工具栏里的动作按钮统一成严格等宽等高的方块。

        三个按钮都是「图标 + 文字」，但文字长度不同（启动 / 停止 / 保存），
        sizeHint 因此天然不等；只靠样式表 ``min-width`` 兜底时，谁的内容更宽
        谁就更宽，视觉上三个按钮宽窄不一。这里取三者 sizeHint 的最大值
        （并有 min_w 兜底），逐个 ``setFixedSize``，与字体、DPI、系统主题
        无关地保持等宽等高。
        """
        btns = [b for b in buttons if isinstance(b, QToolButton)]
        if not btns:
            return
        for b in btns:
            b.ensurePolished()          # 先应用样式表，sizeHint 才是最终值
        w = max([min_w] + [b.sizeHint().width() for b in btns])
        h = max([44] + [b.sizeHint().height() for b in btns])
        for b in btns:
            b.setFixedSize(w, h)

    def _validate(self, cfg: AppConfig) -> str:
        if not cfg.server_ip:
            return "请填写 SIP 服务器地址。"
        if not cfg.server_domain:
            return "请填写 SIP 服务器域（形如 1.2.3.4:5080）。"
        if len(cfg.server_id) != 20 or not cfg.server_id.isdigit():
            return "SIP 服务器 ID 必须是 20 位数字。"
        if len(cfg.device_id) != 20 or not cfg.device_id.isdigit():
            return "SIP 用户名（设备 ID）必须是 20 位数字。"
        if not cfg.auth_username:
            return "请填写 SIP 用户认证 ID。"
        if not cfg.password:
            return "请填写密码。"
        if cfg.broadcast_enabled:
            if not (1 <= cfg.broadcast_media_port <= 65535):
                return "国标广播的音频接收端口必须在 1~65535 之间。"
            if cfg.local_media_port and cfg.broadcast_media_port == cfg.local_media_port:
                return ("国标广播的音频接收端口与「本机媒体端口」冲突"
                        f"（均为 {cfg.local_media_port}），请改为不同端口。")
        if not os.path.exists(cfg.ps_file_abs()):
            return f"PS 流文件不存在：{cfg.ps_file_abs()}"
        return ""

    def closeEvent(self, ev) -> None:
        dev = self.device
        real_quit = getattr(self, "_real_quit", False)
        log_event(f"closeEvent: device={'running' if dev is not None else 'idle'}; "
                  f"real_quit={real_quit}")
        if real_quit:
            # 退出流程进行中（_really_quit 已置位）：放行窗口关闭，结束事件循环。
            log_event("closeEvent: accepting (real quit)")
            ev.accept()
            return
        # 标题栏「关闭(X)」= 关闭应用（与最小化不同：最小化收起托盘、X 直接退出）。
        # 运行中先停设备再退，空闲直接退；行为与托盘菜单「退出」完全一致。
        self._really_quit()
        ev.ignore()
