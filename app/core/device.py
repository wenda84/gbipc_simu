"""GB28181 设备业务核心。

- 单 worker 线程 + 事件队列：SIP 层回调只投递事件，业务逻辑全部在 worker 内串行处理；
- 注册状态机：未注册 -> 注册中 -> 在线；失败按"注册间隔"重试；心跳超时判离线并重注册；
- 查询应答：DeviceInfo / DeviceStatus / Catalog（其余查询兜底回 Result=ERROR）；
- 呼叫管理：解析 INVITE SDP offer -> 200 OK 自定义 SDP -> CONFIRMED 起推流 -> BYE/异常停流。
- 国标广播（9.12.1）：本工具为**语音流接收者**。收到平台 Notify/Broadcast 后回
  Response/Broadcast，并主动向语音流发送者发起 INVITE（UAC）拉取 PCMA 语音流，
  由 media/broadcast_session 播放并上报电平；BYE / 入流超时 / 停止时收敛链路。
"""
from __future__ import annotations

import os
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from ..sip.events import (
    RegStateEvent, MessageEvent, IncomingCallEvent, CallStateEvent, MessageStatusEvent,
    RemoteSdpEvent,
)
from ..sip.stack import SipEvents
from ..sip.pjsip_stack import PjsipStack
from ..media.ps_streamer import PsStreamer
from ..media.broadcast_session import BroadcastSession
from . import manscdp
from .applog import log_event
from .config import AppConfig, detect_local_ip
from .gbsdp import parse_offer, build_answer, build_audio_offer, build_subject, SdpOffer


class RegState(str, Enum):
    IDLE = "未注册"
    REGISTERING = "注册中"
    ONLINE = "在线"
    RETRYING = "重试中"


#: 广播子状态（UI 展示用）
BC_IDLE = "空闲"
BC_ANSWERED = "呼叫中"
BC_RECEIVING = "广播中"
BC_FAILED = "失败"


@dataclass
class CallContext:
    call_id: int
    offer: SdpOffer
    answer_sdp: str
    subject: str = ""
    streamer: Optional[PsStreamer] = None
    kind: str = "play"                 # "play"（点播推流，UAS）| "broadcast"（广播收流，UAC）
    broadcast_source: str = ""         # 广播场景：语音流发送者 ID（通知中的 SourceID）


class DeviceEvents:
    """业务层对外通知（UI 层实现）。全部在 worker 线程被调用。"""

    def on_log(self, msg: str) -> None: pass

    def on_reg_state(self, state: RegState, detail: str = "") -> None: pass

    def on_call_state(self, text: str) -> None: pass

    def on_stream_stats(self, text: str) -> None: pass

    def on_broadcast_state(self, text: str) -> None: pass

    def on_broadcast_level(self, level: float, peak: float) -> None: pass

    def on_broadcast_stream(self, text: str) -> None: pass


class GbDevice(SipEvents):
    """国标 IPC 模拟设备。UI 线程调用 start/stop；SIP 事件经队列在 worker 串行处理。"""

    def __init__(self, cfg: AppConfig, events: Optional[DeviceEvents] = None,
                 sip_stack=None):
        self.cfg = cfg
        self.out = events or DeviceEvents()
        self._sip: SipStack = sip_stack or PjsipStack(self)
        self._queue: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # UI 看门狗依据：worker 最近一次主循环迭代的时间戳（0=尚未启动）
        self._last_tick = 0.0
        # 周期自检锚点：保证心跳/注册重发/注册兜底每 ~1s 执行，不被高频事件饿死
        self._last_housekeep = 0.0
        # 进入「注册中」的时刻：用于注册长时间无结果时的兜底重发
        self._reg_pending_since = 0.0

        self.state = RegState.IDLE
        # 「平台侧曾认定本设备在线」的历史标志。与 state 的区别：state 是
        # 最后一次注册事件的快照，可能因心跳超时/刷新失败而回退到 RETRYING；
        # 但平台侧的注册记录在 expires（默认 3600s）到期前**依然存在**。
        # 停止时是否需要发注销 REGISTER，必须以此为准——否则会漏注销、
        # 让平台在设备已退出后仍保留在线状态。
        self._ever_registered = False
        self.local_ip = cfg.local_ip
        self._ka_sn = 0                    # Keepalive SN 递增
        self._ka_pending = 0               # 已发未回应的心跳数
        self._last_ka_sent = 0.0
        self._next_retry_at = 0.0
        self._calls: dict[int, CallContext] = {}

        # ---- 国标广播（语音流接收者）----
        self._broadcast: Optional[BroadcastSession] = None
        self._broadcast_call_id: Optional[int] = None
        self._broadcast_source = ""
        self._bc_state = BC_IDLE
        # 广播 INVITE 接通超时：发出 INVITE 后超过此秒数仍未 CONFIRMED 则主动释放，
        # 避免对端不响应时呼叫永久卡在 CALLING（音频放静音、UI 卡「呼叫中」）。
        self._bc_invite_at = 0.0
        self._bc_confirmed = False
        self._bc_seq = 0                   # 接收方媒体流序列号递增（附录 K）
        self._bc_remote_ssrc = ""          # 对端 200 OK 中声明的 SSRC（用于交叉校验）
        self._bc_level_lock = threading.Lock()
        self._bc_level_pending: Optional[tuple[float, float]] = None
        self._bc_level_queued = False

    # ---------------- 对外控制（UI 线程） ----------------

    def start(self) -> None:
        if self._worker is not None:
            return
        if not self.local_ip:
            self.local_ip = detect_local_ip(self.cfg.server_ip, self.cfg.server_port)
            self._log(f"自动探测本机 IP: {self.local_ip}")
        # pjsua 生命周期（libInit / 唯一一次 libDestroy）与全部 pjsua 同步 API
        # 必须在「已向 pjsua 注册」的线程执行。本方法在 UI 主线程被调用，
        # libInit 内部的 pj_init 即把主线程注册给 pjsua。worker 线程在 _run()
        # 开头经 self._sip.register_thread() 自行注册，其上的 set_registration
        # / shutdown 等 pjsua 调用因此安全（否则触发 os_core_win32.c:658 断言）。
        self._sip.start(self.local_ip, self.cfg.local_sip_port)
        self._sip.create_account(
            device_uri=self.cfg.device_uri,
            registrar_uri=self.cfg.server_uri,
            auth_username=self.cfg.auth_username,
            password=self.cfg.password,
            expires=self.cfg.register_expires,
            retry_interval=self.cfg.register_retry_interval,
        )
        self._set_state(RegState.REGISTERING)
        self._reg_pending_since = time.monotonic()
        self._sip.set_registration(True)
        self._log(f"注册 -> {self.cfg.server_uri} (本机 {self.local_ip}:{self.cfg.local_sip_port})")
        # 启动 worker 处理业务事件（不直接调用 pjsua API）
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="gb-device", daemon=True)
        self._worker.start()

    def stop(self) -> bool:
        """停止设备并释放 pjsua 资源。返回协议栈是否已**彻底销毁**。

        本方法**不**直接调用任何 pjsua API。它只向 worker 线程投递停止事件并
        join 等待其结束；真正的 teardown（set_registration / libDestroy 等）
        由 worker 线程在 _run() 主循环退出后执行。这样可保证 libInit 与
        libDestroy 都在同一个「已向 pjsua 注册」的线程（gb-device worker）上，
        避免 os_core_win32.c 的「unknown/external thread」断言。

        调用方（MainWindow._on_stop）通常在一个独立的 ui-stop 线程里调用本
        方法；join 期间该线程被阻塞，但本身不调用任何 pjsua API，因此安全。
        UI 线程的 closeEvent 直接调用本方法同理（只投递 + join）。

        返回值是 UI 侧的安全前提：pjsua2 的 Lib 是**进程级单实例**，旧协议栈
        未销毁前新建会触发竞态崩溃。join 超时即代表 teardown 仍卡在 pjsua
        内部（libDestroy 阻塞），此时 worker 仍在后台运行，调用方必须继续
        保持「禁止启动」状态，不能把界面翻回可启动。
        """
        self._queue.put(("stop", None))
        ok = True
        if self._worker is not None:
            # teardown 含挂断呼叫排空(3s)+沉降(0.5s)+注销(最长 unregister_grace)
            # +媒体流停止(3s)，给足超时避免 worker 被提前置空而仍在后台析构。
            self._worker.join(timeout=20)
            if self._worker.is_alive():
                # join 超时说明 teardown 卡住（疑似 pjsua 内部阻塞）。注意：
                # 此时**不能**把 worker 置 None 后当作已停止——旧协议栈仍在
                # 后台销毁，允许重新启动会撞进程级单实例竞态而崩溃。返回
                # False 让 UI 保持禁用并提示重启本程序。
                ok = False
                self._log("[core] 停止超时：worker 未在 20s 内退出"
                          "（旧协议栈仍在销毁，本程序需重启后才能再次启动）")
            else:
                self._worker = None
        return ok

    def worker_alive(self) -> bool:
        """worker 线程是否存活（UI 看门狗用）。"""
        return self._worker is not None and self._worker.is_alive()

    def tick_age(self) -> float:
        """距 worker 最近一次主循环迭代经过的秒数（UI 看门狗用）。

        正常情况下主循环每至多 1s 迭代一次；个别重事件（PS 文件分析、
        set_registration 的 EBUSY 退避等）可能让单次迭代耗时数秒，
        UI 侧以 30s 为停滞判定阈值。
        """
        if self._last_tick <= 0.0:
            return 0.0
        return time.monotonic() - self._last_tick

    # ---------------- worker 主循环 ----------------

    def _run(self) -> None:
        # 关键：worker 线程必须在使用任何 pjsua 同步 API 之前向 pjlib 注册，
        # 否则 set_registration / shutdown 等调用会触发 os_core_win32.c:658
        # 「unknown/external thread」断言。主线程由 libInit 自动注册，本 worker
        # 需显式注册。注册后整个生命周期（含唯一的 libDestroy）都发生在本线程。
        try:
            self._sip.register_thread("gb-device")
        except Exception as e:
            self._log(f"[core] 注册 worker 线程失败: {e}")
        self._last_housekeep = time.monotonic()
        while not self._stop.is_set():
            self._last_tick = time.monotonic()
            try:
                kind, payload = self._queue.get(timeout=0.25)
            except queue.Empty:
                kind, payload = None, None
            if kind is not None:
                try:
                    self._dispatch(kind, payload)
                except Exception as e:
                    self._log(f"[core] 事件处理异常({kind}): {e}")
            # 周期自检：不依赖队列空闲。广播电平事件以 25Hz 持续灌入队列时会
            # 让队列几乎永不满 1s 空闲，若只在空闲时自检则心跳/注册重发/注册
            # 兜底会被饿死（表现为「心跳停发、呼叫卡死而 UI 仍显示运行」）。
            # 改为按时间锚点每 ~1s 强制跑一次 _on_tick，与队列负载解耦。
            now = time.monotonic()
            if now - self._last_housekeep >= 1.0:
                self._last_housekeep = now
                try:
                    self._on_tick()
                except Exception as e:
                    self._log(f"[core] 自检异常: {e}")
        # 主循环退出：此刻仍在本 worker 线程（已向 pjsua 注册）。在此执行
        # teardown（set_registration / shutdown → 唯一一次 libDestroy），可避免
        # os_core_win32.c:658 的「unknown/external thread」断言。
        try:
            self._teardown()
        except Exception as e:
            self._log(f"[core] teardown 异常: {e}")

    def _dispatch(self, kind: str, payload) -> None:
        if kind == "stop":
            self._stop.set()
        elif kind == "reg_state":
            self._on_reg_state(payload)
        elif kind == "message":
            self._on_message(payload)
        elif kind == "incoming_call":
            self._on_incoming_call(payload)
        elif kind == "call_state":
            self._on_call_state(payload)
        elif kind == "message_status":
            self._on_message_status(payload)
        elif kind == "remote_sdp":
            self._on_remote_sdp(payload)
        elif kind == "broadcast_level":
            self._flush_broadcast_level()
        elif kind == "broadcast_timeout":
            self._on_broadcast_timeout()
        elif kind == "tick":
            self._on_tick()

    # ---------------- 启动 / 停止 ----------------

    def _teardown(self) -> None:
        # 关闭流程中必须抑制心跳超时触发的自动重注册：否则 set_registration
        # 可能在 ua 忙碌时返回 EBUSY，随后账号被删除，而心跳定时器/回调仍
        # 访问该账号，触发 pjsua_acc_get_user_data 断言崩溃。
        # 关闭所有活跃呼叫：先 hangup（发 BYE），再排空队列让 pjsua 回送的
        # DISCONNECTED 被本线程处理（drop_call 清理 pjsip 调用字典、停流），
        # 确保 pjsua 真正释放呼叫后再销毁账号/协议栈——否则 libDestroy 会触碰
        # 仍在析构的呼叫而段错误（「呼叫中停止」闪退根因：空闲停止正常、仅呼叫
        # 活跃时点停止才崩）。排空复刻了「正常运行中收到 BYE」的安全时序。
        # 广播收流会话必须先在协议栈销毁前收敛：音频播放线程与收流线程
        # 都持有业务回调，若其存活到窗口/协议栈析构之后会触发无效访问。
        # 注意先记下广播呼叫 ID——_stop_broadcast 会清空它并移除呼叫上下文，
        # 之后仍需对该 UAC 呼叫发 BYE。
        bc_call = self._broadcast_call_id
        try:
            if self._broadcast is not None:
                self._stop_broadcast("设备停止")
        except Exception as e:
            self._log(f"[core] 停止广播会话异常: {e}")
        if bc_call is not None:
            try:
                self._sip.hangup_call(bc_call)
            except Exception:
                pass
        for call_id in list(self._calls):
            try:
                self._sip.hangup_call(call_id)
            except Exception:
                pass
        # 仅在确实拆过呼叫（含广播呼叫）时才需要沉降窗口；空闲停止跳过。
        # 此前排空等待(≤0.1s)+沉降(0.5s)无条件执行且位于注销 REGISTER 之前，
        # 是「点停止 → 注销成功」本地延迟的主要来源（抓包实测 SIP 仅 ~8ms）。
        self._drain_until_idle(
            timeout=3.0,
            settle=0.5 if (self._calls or bc_call is not None) else 0.0)
        # 是否真正需要发注销 REGISTER：
        # 判据是「平台侧是否曾经认定本设备在线」(_ever_registered)，而非
        # 当前 state 是否 ONLINE。二者会分叉：注册成功后若心跳超时或注册
        # 刷新失败，state 会回退到 RETRYING，但平台侧的注册记录在 expires
        # （默认 3600s）到期前依然存在——此时跳过注销，设备会在平台侧残留
        # 在线状态整整一小时。改用历史标志可覆盖这整类场景。
        # 反之，若从未拿到 200 OK，平台本就无在线记录，发 Expires=0 注销
        # 既无意义，又会与在途启动 REGISTER 抢 regc、触发最长 6 秒的
        # PJSIP_EBUSY 空锤。此情形直接跳过「业务层」注销，走
        # shutdown(unregister=False)；本地 regc 由 shutdown 内的
        # acc.shutdown()（pjsua_acc_del）→ destroy_regc(force) **强制**销毁
        # （不等在途事务），平台侧无在线记录，无需、也无法注销。
        unreg_done = False
        if self._ever_registered:
            try:
                self._sip.set_registration(False)
                unreg_done = self._sip.wait_unregister_done(self.cfg.unregister_grace)
            except Exception:
                pass
            # 服务器已确认注销（或已超时放弃）——此刻「逻辑上已离线」，立即把
            # UI 翻成「未注册」，不必等 libDestroy（pjsua 协议栈销毁常耗时 ~1s）。
            # 启动/停止按钮的重新使能仍由主窗口在 dev.stop() 返回后统一处理，
            # 避免「界面已可重开」与「旧协议栈仍在销毁」竞态。
            self._set_state(RegState.IDLE)
            # 显式留痕：此行日志时间即「未注册」上屏时刻，比「已停止」早约
            # 一个 libDestroy 耗时，便于核对 UI 刷新与整体停止耗时的分解。
            self._log("注销已确认，界面置为未注册" if unreg_done
                      else "注销未确认（超时/异常），界面置为未注册")
        else:
            # 从未在线：无需（也无法）注销，直接置 IDLE 并销毁协议栈。
            self._set_state(RegState.IDLE)
            self._log("从未在线（无 200 OK），跳过注销，直接销毁协议栈")
        self._ever_registered = False
        self._sip.shutdown(unregister=False)
        self._log("已停止")

    def _drain_until_idle(self, timeout: float, settle: float = 0.5) -> None:
        """排空事件队列，让 pjsua 回送的 DISCONNECTED 等事件被本线程处理。

        worker 主循环已退出，但 pjsua 内部线程仍会把 DISCONNECTED 投递到队列；
        若不处理，drop_call 不会执行、pjsip 调用字典不清空，且 pjsua 内部呼叫
        拆链未完成，随后 libDestroy 会踩到仍在析构的呼叫。这里主动排空，复刻
        「正常运行中收到 BYE」的安全时序；排空后给 pjsua 内部线程一个沉降窗口。

        settle：沉降秒数，仅在确实拆过呼叫时为正。空闲停止（无任何呼叫）
        传 0 跳过沉降——此时无呼叫/媒体拆链，无物可沉，等待纯属白耗。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                kind, payload = self._queue.get(timeout=0.1)
            except queue.Empty:
                if not self._calls:
                    break
                continue
            try:
                self._dispatch(kind, payload)
            except Exception as e:
                self._log(f"[core] drain 异常({kind}): {e}")
            if not self._calls:
                break
        # 给 pjsua 内部线程完成呼叫/媒体拆链的沉降时间（无拆链时跳过）
        if settle > 0:
            time.sleep(settle)

    # ---------------- 注册状态 ----------------

    def _on_reg_state(self, ev: RegStateEvent) -> None:
        # 必须先把「注册失效」(is_active=False) 与「注册结果」分流：
        # 注销被服务端确认时 pjsua 回调的是 is_active=False + code=200，
        # 旧实现只看 code 是否在 200，把它判成「注册未成功」并置 RETRYING、
        # 把重试时刻推后整整一个注册间隔——表现为「心跳超时后长期不上线」。
        if not ev.is_active:
            if self._stop.is_set():
                # 停机流程中本端主动注销的回执：状态由 _teardown 统一收敛，
                # 此处只留痕，绝不再触发重注册（否则会与销毁流程抢 regc）。
                self._log(f"注销已被服务端确认 ({ev.code} {ev.reason})")
                return
            ok = 200 <= ev.code < 300
            if ok:
                self._log(f"注册被服务端解除 ({ev.code} {ev.reason})，将尽快重新注册")
            else:
                self._log(f"注册未成功: {ev.code} {ev.reason}")
            if self.state == RegState.IDLE:
                return
            # 重试时刻只在「刚转入 RETRYING」时设定一次；已在 RETRYING 中则
            # 只刷新原因、不再推迟。旧实现每次失败回调都 now+注册间隔，
            # 平台持续拒绝时重试会被无限向后推（P1-8）。
            fresh = self.state != RegState.RETRYING
            self._set_state(RegState.RETRYING, f"{ev.code} {ev.reason}")
            if fresh:
                # 被服务端解除（2xx）属「平台仍在线、只是注销了本端」，尽快重注册；
                # 注册失败则按配置的注册间隔退避，避免与 pjsua 的自动重试叠加重锤。
                self._next_retry_at = time.monotonic() + (
                    2 if ok else self.cfg.register_retry_interval)
            return

        if ev.code == 200:
            self._set_state(RegState.ONLINE)
            self._ever_registered = True
            self._ka_pending = 0
            self._last_ka_sent = 0.0
            self._log("注册成功 (200 OK)")
        else:
            self._log(f"注册未成功: {ev.code} {ev.reason}")
            if self.state != RegState.IDLE and not self._stop.is_set():
                fresh = self.state != RegState.RETRYING
                self._set_state(RegState.RETRYING, f"{ev.code} {ev.reason}")
                if fresh:
                    self._next_retry_at = time.monotonic() + self.cfg.register_retry_interval

    def _on_tick(self) -> None:
        # 停机流程中不再做心跳/重注册判定，避免与 _teardown 的注销注册
        # 竞争同一注册事务（会返回 PJ_EBUSY 并导致注销 REGISTER 丢失）。
        if self._stop.is_set():
            return
        now = time.monotonic()
        # 广播 INVITE 接通超时：对端（语音流发送者）长期不应答则主动释放，
        # 避免呼叫永久卡在 CALLING（音频放静音、UI 一直「呼叫中」、心跳被连带饿死）。
        if (self._broadcast_call_id is not None and not self._bc_confirmed
                and self._bc_invite_at > 0
                and now - self._bc_invite_at >= self.cfg.broadcast_invite_timeout):
            self._log(f"广播 INVITE 已 {int(now - self._bc_invite_at)}s 未接通，"
                      f"主动释放（对端 {self._broadcast_source} 无响应）")
            self._bc_invite_at = 0.0
            cid = self._broadcast_call_id
            self._stop_broadcast("广播呼叫超时未接通")
            if cid is not None:
                try:
                    self._sip.hangup_call(cid)
                except Exception:
                    pass
        # 广播收流统计（低速率刷新，供状态页展示丢包/乱序）
        if self._broadcast is not None:
            try:
                self.out.on_broadcast_stream(self._broadcast.stream_summary())
            except Exception:
                pass
        if self.state == RegState.ONLINE:
            # 周期心跳
            if now - self._last_ka_sent >= self.cfg.keepalive_interval:
                self._send_keepalive()
            # 心跳超时判定
            if self._ka_pending >= self.cfg.keepalive_timeout_count:
                self._log(f"心跳连续 {self._ka_pending} 次超时，判定平台离线，重新注册")
                self._ka_pending = 0
                self._set_state(RegState.RETRYING, "心跳超时")
                # 直接重注册，**不再先发注销 REGISTER**（旧实现）：
                # 1) GB28181 语义下心跳丢失只需重新注册；发 Expires=0 注销会让
                #    平台先标记离线再上线，产生无谓的上下线抖动与告警；
                # 2) 那次注销的回执（is_active=False + 200）会把 stack 层的
                #    _unregistering 永久置位，污染此后所有「注销完成」判定，
                #    使停止时的 wait_unregister_done() 提前返回、注销被强杀；
                # 3) 注销与随后的重注册会争抢同一个 regc，触发 EBUSY 空锤。
                # pjsua 侧若仍有在途注册事务，本次 set_registration(True) 会
                # 返回 EBUSY，由 RETRYING 分支在下一个间隔重试。
                self._next_retry_at = now + 2
        elif self.state == RegState.RETRYING and now >= self._next_retry_at:
            self._set_state(RegState.REGISTERING)
            self._reg_pending_since = now
            try:
                self._sip.set_registration(True)
            except Exception as e:
                self._log(f"重注册失败: {e}")
                self._next_retry_at = now + self.cfg.register_retry_interval
        elif self.state == RegState.REGISTERING:
            # 兜底：发出 REGISTER 后长时间没有任何 onRegState 结果（平台
            # 彻底无响应且 pjsua 自动重试也未产生回调）时，周期性重新发起
            # 注册，避免永久停留在「注册中」——那会导致心跳/日志整体停摆
            # 而 UI 仍显示运行（无法区分「在线安静」与「卡死」）。
            # 上限 5 分钟：旧实现取 max(60, expires+15)（默认 3615s≈1 小时），
            # 兜底形同虚设；pjsua 自带 retryIntervalSec 重试与本兜底并列，
            # 取 5 分钟既能兜住静默丢包，又不会与 pjsua 的重试节奏脱节。
            if now - self._reg_pending_since >= max(
                    60, min(self.cfg.register_expires // 3, 300)):
                self._reg_pending_since = now
                self._log("长时间无注册结果，重新发起注册")
                try:
                    self._sip.set_registration(True)
                except Exception as e:
                    self._log(f"重新发起注册失败: {e}")

    def _send_keepalive(self) -> None:
        self._ka_sn += 1
        body = manscdp.build_keepalive(str(self._ka_sn), self.cfg.device_id)
        self._sip.send_message(self.cfg.server_uri, manscdp.CONTENT_TYPE, body)
        self._ka_pending += 1
        self._last_ka_sent = time.monotonic()
        # 心跳发送仅写入日志文件，不在 UI 实时日志显示（避免高频刷屏）
        log_event(f"心跳已发送 (SN={self._ka_sn}, 待回应 {self._ka_pending})")

    # ---------------- MESSAGE（查询应答） ----------------

    def _on_message(self, ev: MessageEvent) -> None:
        msg = manscdp.parse(ev.body)
        if msg is None:
            self._log(f"收到无法解析的 MESSAGE（{ev.content_type}, {len(ev.body)}B）")
            return
        self._log(f"收到 {msg.root}/{msg.cmd_type} (SN={msg.sn})")
        if msg.root == "Query":
            self._handle_query(msg)
        elif msg.root == "Notify":
            if msg.cmd_type == "Broadcast":
                self._on_broadcast_notify(msg)
            # 其他 Notify（如平台侧设备状态）本工具不处理
        elif msg.root == "Response":
            pass  # 本工具不主动查询；平台应答无需处理

    def _handle_query(self, msg: manscdp.ManscdpMessage) -> None:
        cfg = self.cfg
        if msg.cmd_type == "DeviceInfo":
            body = manscdp.build_device_info_response(
                msg.sn, cfg.device_id, cfg.device_name, cfg.manufacturer,
                cfg.model, cfg.firmware, cfg.channel)
        elif msg.cmd_type == "DeviceStatus":
            body = manscdp.build_device_status_response(
                msg.sn, cfg.device_id, time.strftime("%Y-%m-%dT%H:%M:%S"))
        elif msg.cmd_type == "Catalog":
            items = [{
                "DeviceID": cfg.device_id, "Name": cfg.device_name,
                "Manufacturer": cfg.manufacturer, "Model": cfg.model,
                "Owner": "", "CivilCode": cfg.civil_code, "Address": "",
                "Parental": "0", "ParentID": cfg.server_id,
                "RegisterWay": "1", "Secrecy": "0", "Status": "ON",
            }]
            # 9.12.1.1：具备语音输出能力时必须在目录中上报语音输出设备
            # （类型编码 137，ParentID = 所属 IPC）。未勾选广播即视为不具备该能力。
            if cfg.broadcast_enabled:
                items.append({
                    "DeviceID": cfg.broadcast_channel_id,
                    "Name": f"{cfg.device_name}-语音输出",
                    "Manufacturer": cfg.manufacturer, "Model": cfg.model,
                    "Owner": "", "CivilCode": cfg.civil_code, "Address": "",
                    "Parental": "0", "ParentID": cfg.device_id,
                    "RegisterWay": "1", "Secrecy": "0", "Status": "ON",
                })
            body = manscdp.build_catalog_response(msg.sn, cfg.device_id, items)
        else:
            self._log(f"未支持的查询 {msg.cmd_type}，回 Result=ERROR")
            body = manscdp.build_result_response(msg.cmd_type, msg.sn,
                                                 cfg.device_id, "ERROR")
        self._sip.send_message(self.cfg.server_uri, manscdp.CONTENT_TYPE, body)
        self._log(f"已应答 {msg.cmd_type} (SN={msg.sn})")

    # ---------------- 呼叫 ----------------

    def _on_incoming_call(self, ev: IncomingCallEvent) -> None:
        offer = parse_offer(ev.offer_sdp)
        self._log(f"收到 INVITE: s={offer.session_name} "
                  f"{offer.media.proto} {offer.dest_ip}:{offer.dest_port} y={offer.ssrc} "
                  f"setup={offer.media.setup} Subject={ev.subject}")

        # 附录 M：同一媒体源重复呼叫，释放旧链路
        for cid, ctx in list(self._calls.items()):
            if ctx.subject and ctx.subject == ev.subject:
                self._log(f"同媒体源重复呼叫，释放旧链路 call={cid}")
                self._stop_stream(cid)
                self._sip.hangup_call(cid)
                self._sip.drop_call(cid)

        if offer.session_name not in ("Play", ""):
            self._log(f"暂不支持 s={offer.session_name}，回 488")
            self._sip.answer_call(ev.call_id, 488)
            self._sip.drop_call(ev.call_id)
            return
        if offer.media.media != "video":
            # 本期接收方向仅支持视频点播（m=video PS/90000）。
            # 语音方向（m=audio）由「国标广播」以 UAC 主动拉流实现，不走本应答路径。
            self._log(f"暂不支持 m={offer.media.media} 的入呼，回 488")
            self._sip.answer_call(ev.call_id, 488)
            self._sip.drop_call(ev.call_id)
            return
        if not offer.dest_ip or not offer.dest_port:
            self._log("Offer 缺少媒体地址，回 488")
            self._sip.answer_call(ev.call_id, 488)
            self._sip.drop_call(ev.call_id)
            return

        proto = "tcp" if offer.is_tcp else "udp"
        answer = build_answer(self.cfg.device_id, self.local_ip,
                              self.cfg.local_media_port, offer)
        self._calls[ev.call_id] = CallContext(call_id=ev.call_id, offer=offer,
                                              answer_sdp=answer, subject=ev.subject)
        self._sip.answer_call(ev.call_id, 200, answer)
        self._log(f"已回 200 OK（{proto.upper()} 模式，本机媒体端口 {self.cfg.local_media_port}）")
        self.out.on_call_state(f"呼叫建立中 ({proto.upper()})")

    def _on_call_state(self, ev: CallStateEvent) -> None:
        ctx = self._calls.get(ev.call_id)
        self._log(f"呼叫 {ev.call_id} 状态 {ev.state} {ev.status_code} {ev.reason}")
        if ctx is not None and ctx.kind == "broadcast":
            self._on_broadcast_call_state(ev, ctx)
            return
        if ev.state == "CONFIRMED" and ctx is not None:
            self._start_stream(ctx)
        elif ev.state == "DISCONNECTED":
            self._stop_stream(ev.call_id)
            self._sip.drop_call(ev.call_id)
            # 该 DISCONNECTED 可能是广播呼叫已被 _stop_broadcast 摘除上下文后回送的，
            # 不能一概清空「推流」状态；仅当已无其它点播呼叫时才置空闲。
            if not any(c.kind == "play" for c in self._calls.values()):
                self.out.on_call_state("空闲")

    def _start_stream(self, ctx: CallContext) -> None:
        if ctx.streamer is not None and ctx.streamer.is_running():
            return
        cfg = self.cfg
        proto = "tcp" if ctx.offer.is_tcp else "udp"
        try:
            streamer = PsStreamer(
                protocol=proto,
                dest_ip=ctx.offer.dest_ip,
                dest_port=ctx.offer.dest_port,
                ssrc=ctx.offer.ssrc or "0",
                local_port=0 if proto == "tcp" else cfg.local_media_port,
                payload_type=(ctx.offer.media.payloads[0]
                              if ctx.offer.media.payloads else 96),
                on_log=self._log,
                on_stats=lambda s: self.out.on_stream_stats(
                    f"RTP {s['rtp']} 包 / {s['bytes'] / 1024:.0f} KB / {s['loops']} 轮"),
            )
            play_times = 0 if cfg.loop_play else 1
            streamer.start_file(cfg.ps_file_abs(), play_times)
            ctx.streamer = streamer
            self.out.on_call_state(f"推流中 -> {ctx.offer.dest_ip}:{ctx.offer.dest_port} ({proto.upper()})")
        except Exception as e:
            self._log(f"启动推流失败: {e}")
            self._sip.hangup_call(ctx.call_id)

    def _stop_stream(self, call_id: int) -> None:
        ctx = self._calls.pop(call_id, None)
        if ctx is not None and ctx.streamer is not None:
            ctx.streamer.stop()
            self._log("推流已停止")

    # ---------------- 国标广播（语音流接收者，9.12.1） ----------------

    def _on_broadcast_notify(self, msg: manscdp.ManscdpMessage) -> None:
        """处理平台的语音广播通知（A.2.5 d）。"""
        source_id = (msg.extra.get("SourceID") or "").strip()
        target_id = (msg.extra.get("TargetID") or "").strip()
        sn = msg.sn or "0"
        cfg = self.cfg

        if not source_id:
            self._log("广播通知缺少 SourceID，忽略")
            return
        # 应答的 DeviceID 取通知中的 TargetID（语音输出设备编码）；
        # 通知寻址到主设备 ID 时即回主设备 ID，二者均符合 A.2.6 l) 语义。
        reply_id = target_id or cfg.device_id

        if not cfg.broadcast_enabled:
            self._reject_broadcast(sn, reply_id, "本机未启用国标广播")
            return
        if self.state != RegState.ONLINE:
            self._reject_broadcast(sn, reply_id, f"设备未在线（{self.state.value}）")
            return
        # 寻址校验：允许目标为主设备 ID（表示该 IPC 下所有语音输出设备）
        # 或本机的 137 语音输出通道 ID（9.12.1.1）。
        if target_id and target_id not in (cfg.device_id, cfg.broadcast_channel_id):
            self._reject_broadcast(sn, reply_id, f"非本机目标编码 {target_id}")
            return

        # 同一媒体源重复呼叫：先释放旧链路（附录 M c）
        if self._broadcast is not None:
            self._log("收到新的广播通知，释放既有广播链路")
            self._stop_broadcast("被新广播通知替换")

        self._sip.send_message(cfg.server_uri, manscdp.CONTENT_TYPE,
                               manscdp.build_broadcast_response(sn, reply_id, "OK"))
        self._log(f"已应答广播通知 (SN={sn}, DeviceID={reply_id}, Result=OK)")
        self._start_broadcast_call(source_id)

    def _reject_broadcast(self, sn: str, reply_id: str, why: str) -> None:
        """明确拒绝广播（优于静默丢弃：平台与抓包侧均可观测）。"""
        self._sip.send_message(self.cfg.server_uri, manscdp.CONTENT_TYPE,
                               manscdp.build_broadcast_response(sn, reply_id, "ERROR"))
        self._log(f"已拒绝广播通知 (SN={sn}): {why} -> Result=ERROR")
        self._set_broadcast_state(BC_FAILED, why, notify=False)

    def _start_broadcast_call(self, source_id: str) -> None:
        """向语音流发送者发起 INVITE（9.12.1.2 信令 5，本端为 UAC）。"""
        cfg = self.cfg
        # 先起收流会话再发 INVITE：收流端口必须在媒体到达前就绪，
        # 否则 200 OK/ACK 后平台立刻发流会丢掉头部若干包。
        session = BroadcastSession(
            source_id=source_id,
            protocol="udp",                      # 标准示例（9.12.1.3.2）为 RTP/AVP
            local_ip=self.local_ip,
            media_port=cfg.broadcast_media_port,
            expect_ssrc=None,                    # 未见 SDP 前先锁定首包，收到 200 OK 后再交叉校验
            jitter_ms=cfg.broadcast_jitter_ms,
            volume_percent=cfg.broadcast_volume_percent,
            rtp_timeout=cfg.broadcast_rtp_timeout,
            on_level=self._emit_broadcast_level,
            on_timeout=lambda: self._queue.put(("broadcast_timeout", None)),
            on_log=self._log,
            # 测试/无头环境可用 GBIPC_FORCE_NULL_SINK=1 关掉出声，仅保留收流与电平，
            # 使自动化测试不依赖也不干扰音频硬件。
            force_null_sink=os.environ.get("GBIPC_FORCE_NULL_SINK") == "1",
        )
        try:
            session.start()
        except Exception as e:
            self._log(f"启动广播收流失败: {e}")
            self._set_broadcast_state(BC_FAILED, f"端口 {cfg.broadcast_media_port} 绑定失败")
            return
        self._broadcast = session
        self._broadcast_source = source_id
        self._bc_remote_ssrc = ""

        self._bc_seq += 1
        subject = build_subject(source_id, cfg.device_id, "0", str(self._bc_seq))
        offer = build_audio_offer(cfg.device_id, self.local_ip,
                                 cfg.broadcast_media_port, self._make_ssrc())
        # 广播 INVITE 经平台转发，目标 host:port 与注册/心跳一致，
        # 由「服务器地址+端口」派生（而非服务器域），保证端口字段全局生效。
        to_uri = f"sip:{source_id}@{cfg.server_hostport}"
        try:
            call_id = self._sip.make_call(to_uri, subject, offer)
        except Exception as e:
            self._log(f"发起广播 INVITE 失败: {e}")
            self._stop_broadcast("呼叫发起失败")
            return

        self._broadcast_call_id = call_id
        self._bc_invite_at = time.monotonic()
        self._bc_confirmed = False
        self._calls[call_id] = CallContext(
            call_id=call_id, offer=parse_offer(offer, prefer="audio"),
            answer_sdp="", subject=subject, kind="broadcast",
            broadcast_source=source_id)
        self._log(f"已发起广播 INVITE -> {to_uri}")
        self._log(f"  Subject: {subject}")
        self._set_broadcast_state(BC_ANSWERED, f"呼叫语音流发送者 {source_id}")

    def _make_ssrc(self) -> str:
        """按附录 F 构造 10 位 SSRC：首位 0（实时）+ 域标识（监控域 ID 第 4~8 位）+ 4 位域内标识。"""
        sid = self.cfg.server_id
        domain = sid[3:8] if len(sid) >= 8 and sid.isdigit() else "00000"
        return f"0{domain}{random.randint(0, 9999):04d}"

    def _on_broadcast_call_state(self, ev: CallStateEvent, ctx: CallContext) -> None:
        if ev.state == "CONFIRMED":
            self._bc_confirmed = True
            self._bc_invite_at = 0.0
            self._set_broadcast_state(BC_RECEIVING, f"来源 {ctx.broadcast_source}")
            self._log("广播链路已建立，等待语音流")
        elif ev.state == "DISCONNECTED":
            self._log(f"广播链路断开: {ev.status_code} {ev.reason}")
            self._stop_broadcast("链路断开")
            self._sip.drop_call(ev.call_id)

    def _on_remote_sdp(self, ev: RemoteSdpEvent) -> None:
        """对端 SDP（广播场景即 200 OK 的 SDP）：记录并对 SSRC 做交叉校验。"""
        ctx = self._calls.get(ev.call_id)
        if ctx is None or ctx.kind != "broadcast":
            return
        remote = parse_offer(ev.sdp, prefer="audio")
        pt = remote.payload_for("PCMA")
        if remote.media.media != "audio" or pt is None:
            self._log("[broadcast] 平台应答 SDP 未声明 PCMA/8000，"
                      "将按裸 PCMA 解析（可能无声音）")
        else:
            self._log(f"[broadcast] 平台应答 SDP: m={remote.media.media} "
                      f"{remote.media.proto} PCMA/{pt} y={remote.ssrc or '-'}")
        self._bc_remote_ssrc = remote.ssrc
        session = self._broadcast
        if session is None or not remote.ssrc:
            return
        # 收流侧已在首个合法包上锁定 SSRC；若与平台声明不一致，仅告警不中断
        # （部分平台 SDP 与实际媒体 SSRC 不一致，强行校验会导致完全收不到流）。
        rx = getattr(session, "_rx", None)
        locked = getattr(rx, "_locked_ssrc", None) if rx is not None else None
        if locked is not None:
            try:
                if int(remote.ssrc) != int(locked):
                    self._log(f"[broadcast] 注意：平台声明 SSRC={remote.ssrc} 与"
                              f"实际入流 SSRC={locked} 不一致")
            except ValueError:
                pass

    def _on_broadcast_timeout(self) -> None:
        """入流中断超时（附录 M b）：主动释放链路。"""
        if self._broadcast is None:
            return
        self._log("广播入流中断超时，释放链路")
        call_id = self._broadcast_call_id
        self._stop_broadcast("入流中断")
        if call_id is not None:
            self._sip.hangup_call(call_id)
            self._sip.drop_call(call_id)

    def _stop_broadcast(self, reason: str = "") -> None:
        session = self._broadcast
        call_id = self._broadcast_call_id
        self._broadcast = None
        self._broadcast_call_id = None
        self._broadcast_source = ""
        self._bc_remote_ssrc = ""
        self._bc_invite_at = 0.0
        self._bc_confirmed = False
        if session is not None:
            session.stop()
        # 必须把广播呼叫上下文一并摘除：否则 _drain_until_idle 会一直等
        # _calls 清空，停机时被拖到超时才返回。
        if call_id is not None:
            self._calls.pop(call_id, None)
        self._reset_broadcast_level()
        self._set_broadcast_state(BC_IDLE, reason)

    # ---- 电平上报：音频线程 → worker 队列（合并式，避免挤占信令事件）----

    def _emit_broadcast_level(self, level: float, peak: float) -> None:
        """由广播会话的播放线程调用。只做合并与投递，不直接触碰 UI。

        采用「有未消费则覆盖」的合并策略：25 Hz 的电平即使 worker 一时繁忙，
        队列里最多也只积压一条，不会挤占 SIP 事件处理。
        """
        with self._bc_level_lock:
            self._bc_level_pending = (level, peak)
            if self._bc_level_queued:
                return
            self._bc_level_queued = True
        self._queue.put(("broadcast_level", None))

    def _flush_broadcast_level(self) -> None:
        with self._bc_level_lock:
            self._bc_level_queued = False
            payload = self._bc_level_pending
            self._bc_level_pending = None
        if payload is not None:
            self.out.on_broadcast_level(payload[0], payload[1])

    def _reset_broadcast_level(self) -> None:
        with self._bc_level_lock:
            self._bc_level_pending = None
        self.out.on_broadcast_level(0.0, 0.0)

    def _set_broadcast_state(self, state: str, detail: str = "",
                             notify: bool = True) -> None:
        self._bc_state = state
        # 日志保留原因，但 UI 的「空闲」不挂原因（用户要求：空闲时不显示 空闲(原因)）
        log_text = state if not detail else f"{state}（{detail}）"
        self._log(f"[broadcast] {log_text}")
        if notify:
            ui_text = state if (state == BC_IDLE or not detail) else f"{state}（{detail}）"
            self.out.on_broadcast_state(ui_text)

    # ---------------- MESSAGE 投递结果 ----------------

    def _on_message_status(self, ev: MessageStatusEvent) -> None:
        # 与平台的任何成功事务（心跳/查询应答收到 2xx）都证明平台存活
        if ev.code == 0 and self.state == RegState.ONLINE:
            self._ka_pending = 0

    # ---------------- SipEvents 接口（SIP 线程调用，只投递） ----------------

    def on_reg_state(self, ev: RegStateEvent) -> None:
        self._queue.put(("reg_state", ev))

    def on_message(self, ev: MessageEvent) -> None:
        self._queue.put(("message", ev))

    def on_incoming_call(self, ev: IncomingCallEvent) -> None:
        self._queue.put(("incoming_call", ev))

    def on_call_state(self, ev: CallStateEvent) -> None:
        self._queue.put(("call_state", ev))

    def on_message_status(self, ev: MessageStatusEvent) -> None:
        self._queue.put(("message_status", ev))

    def on_remote_sdp(self, ev: RemoteSdpEvent) -> None:
        self._queue.put(("remote_sdp", ev))

    def on_log(self, level: int, msg: str) -> None:
        self._log(f"[pjsip] {msg}")

    # ---------------- 内部工具 ----------------

    def _set_state(self, state: RegState, detail: str = "") -> None:
        self.state = state
        self.out.on_reg_state(state, detail)

    def _log(self, msg: str) -> None:
        self.out.on_log(msg)
