"""PJSIP（pjsua2）协议栈实现。

本模块是唯一 import pjsua2 的地方。所有 pjsua2 回调只做解析与事件投递，
不直接调用业务逻辑；业务层通过 SipStack 抽象接口反向调用。
"""
from __future__ import annotations

import threading
import time
import weakref
from typing import Dict, Optional

import pjsua2 as pj

from .events import (
    RegStateEvent, MessageEvent, IncomingCallEvent, CallStateEvent, MessageStatusEvent,
    RemoteSdpEvent,
)
from .stack import SipStack, SipEvents

_STATE_NAMES = {
    pj.PJSIP_INV_STATE_NULL: "NULL",
    pj.PJSIP_INV_STATE_CALLING: "CALLING",
    pj.PJSIP_INV_STATE_INCOMING: "INCOMING",
    pj.PJSIP_INV_STATE_EARLY: "EARLY",
    pj.PJSIP_INV_STATE_CONNECTING: "CONNECTING",
    pj.PJSIP_INV_STATE_CONFIRMED: "CONFIRMED",
    pj.PJSIP_INV_STATE_DISCONNECTED: "DISCONNECTED",
}


def _to_bytes(data, encoding: str = "gbk") -> bytes:
    """把 pjsua2 回调里的字符串/字节统一还原为原始字节。

    SWIG 对 std::string 的 Python3 转换通常为 UTF-8（含 surrogateescape），
    用 surrogateescape 回编可还原 GBK 原始字节；失败时退化为直接 encode。
    """
    if isinstance(data, bytes):
        return data
    if data is None:
        return b""
    try:
        return data.encode("utf-8", errors="surrogateescape")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return data.encode(encoding, errors="replace")


def _split_message(raw: str):
    """从原始 SIP 报文中分离头域与消息体。"""
    for sep in ("\r\n\r\n", "\n\n"):
        idx = raw.find(sep)
        if idx != -1:
            return raw[:idx], raw[idx + len(sep):]
    return raw, ""


def _get_header(raw_headers: str, name: str) -> str:
    prefix = name.lower() + ":"
    for line in raw_headers.replace("\r\n", "\n").split("\n"):
        if line.lower().startswith(prefix):
            return line[len(prefix):].strip()
    return ""


class _LogWriter(pj.LogWriter):
    def __init__(self, events: SipEvents):
        super().__init__()
        self._events = events

    def write(self, entry):
        try:
            self._events.on_log(entry.level, entry.msg.rstrip("\r\n"))
        except Exception:
            pass


class _Call(pj.Call):
    def __init__(self, stack: "PjsipStack", acc, call_id):
        super().__init__(acc, call_id)
        # 弱引用断环：避免 PjsipStack <-> _Call 的跨语言引用环延迟 __del__，
        # 导致析构发生在 libDestroy 之后（TLS 已释放）而触发 658 断言。
        self._stack = weakref.ref(stack)

    def onCallState(self, prm):
        st = self._stack()
        if st is None:
            return
        try:
            ci = self.getInfo()
            ev = CallStateEvent(
                call_id=ci.id,
                state=_STATE_NAMES.get(ci.state, str(ci.state)),
                status_code=ci.lastStatusCode,
                reason=ci.lastReason,
            )
            st._events.on_call_state(ev)
        except Exception as e:
            st._events.on_log(1, f"[sip] onCallState error: {e}")

    def onCallTsxState(self, prm):
        """捕获对端响应携带的 SDP。

        出呼（UAC，语音广播）场景下，平台媒体服务器的 SDP 在 200 OK 里，
        而 pjsua 的呼叫状态回调早于本回调，故这里单独把 SDP 原文上报给业务层，
        用于日志与 SSRC 交叉校验（不作为媒体启动前提）。

        pjsua2 的事件层级：``prm.e``(SipEvent) → ``body``(SipEventBody) →
        ``tsxState``(TsxStateEvent) → ``src``(TsxStateEventSrc) → ``rdata``(SipRxData)。
        注意 ``rdata`` 挂在 ``src`` 上，不在 ``SipEvent`` 上。
        """
        st = self._stack()
        if st is None:
            return
        try:
            ev = prm.e
            body_obj = ev.body if ev is not None else None
            raw = ""
            if body_obj is not None:
                tsx_state = body_obj.tsxState
                if tsx_state is not None and tsx_state.src is not None:
                    rdata = tsx_state.src.rdata
                    if rdata is not None:
                        raw = rdata.wholeMsg or ""
            if not raw:
                return
            _headers, body = _split_message(raw)
            if not body.strip():
                return
            ci = self.getInfo()
            st._events.on_remote_sdp(RemoteSdpEvent(call_id=ci.id, sdp=body))
        except Exception as e:
            st._events.on_log(1, f"[sip] onCallTsxState error: {e}")


class _Account(pj.Account):
    def __init__(self, stack: "PjsipStack"):
        super().__init__()
        # 弱引用断环：PjsipStack <-> _Account（见 _Call 说明）。
        self._stack = weakref.ref(stack)

    def onRegState(self, prm):
        st = self._stack()
        if st is None:
            return
        try:
            info = self.getInfo()
            st._notify_reg_state(
                RegStateEvent(is_active=info.regIsActive, code=prm.code, reason=prm.reason)
            )
        except Exception as e:
            st._events.on_log(1, f"[sip] onRegState error: {e}")

    def onIncomingCall(self, prm):
        st = self._stack()
        if st is None:
            return
        try:
            call = _Call(st, self, prm.callId)
            st._calls[prm.callId] = call
            raw = prm.rdata.wholeMsg if prm.rdata else ""
            headers, body = _split_message(raw)
            ev = IncomingCallEvent(
                call_id=prm.callId,
                from_uri=_get_header(headers, "From"),
                to_uri=_get_header(headers, "To"),
                subject=_get_header(headers, "Subject"),
                offer_sdp=body,
                raw=raw,
            )
            st._events.on_incoming_call(ev)
        except Exception as e:
            st._events.on_log(1, f"[sip] onIncomingCall error: {e}")

    def onInstantMessage(self, prm):
        st = self._stack()
        if st is None:
            return
        try:
            raw = prm.rdata.wholeMsg if prm.rdata else ""
            ev = MessageEvent(
                from_uri=prm.fromUri,
                to_uri=prm.toUri,
                content_type=prm.contentType,
                body=_to_bytes(prm.msgBody),
                raw=raw,
            )
            st._events.on_message(ev)
        except Exception as e:
            st._events.on_log(1, f"[sip] onInstantMessage error: {e}")

    def onSendRequest(self, prm):
        """外发请求（MESSAGE）事务结果。e.body.tsxState.src.status==0 即 PJ_SUCCESS。"""
        st = self._stack()
        if st is None:
            return
        try:
            status = -1
            code = 0
            e = prm.e
            if e is not None and e.body is not None:
                tsx_state = e.body.tsxState
                if tsx_state is not None:
                    if tsx_state.src is not None:
                        status = tsx_state.src.status
                    if tsx_state.tsx is not None:
                        code = tsx_state.tsx.statusCode
            st._events.on_message_status(
                MessageStatusEvent(to_uri="", code=status, reason=str(code), body_head="")
            )
        except Exception as ex:
            st._events.on_log(1, f"[sip] onSendRequest error: {ex}")


class PjsipStack(SipStack):
    """pjsua2 实现的 SipStack。"""

    def __init__(self, events: SipEvents):
        self._events = events
        self._ep: Optional[pj.Endpoint] = None
        self._acc: Optional[_Account] = None
        self._log_writer: Optional[_LogWriter] = None
        self._calls: Dict[int, _Call] = {}
        self._lock = threading.Lock()
        # 注销事务完成信号：pjsua_acc_del() 会强制销毁 regc，导致注销
        # REGISTER 收到 401 挑战后无法再补发带鉴权的请求。因此必须等
        # onRegState 报告注销事务结束（成功/失败）后，才允许销毁账号。
        self._unreg_done = threading.Event()
        self._unregistering = False

    def _notify_reg_state(self, ev: "RegStateEvent") -> None:
        """_Account.onRegState 的统一入口：置位注销完成信号并转发给业务层。"""
        if self._unregistering and not ev.is_active:
            # 注销事务（无论 200 成功还是 4xx/超时失败）已结束
            self._unreg_done.set()
        self._events.on_reg_state(ev)

    def register_thread(self, name: str) -> None:
        """把当前线程注册给 pjlib，避免 os_core_win32.c:658「unknown/external
        thread」断言。

        pjlib 不会自动注册 worker 线程；任何在 worker 上直接调用的 pjsua
        同步 API（set_registration / shutdown 等）都要求当前线程已注册。
        必须在 worker 启动后、首次 pjsua 调用之前调用。主线程由 libInit
        内部的 pj_init 自动注册，无需此调用。
        """
        if self._ep is not None:
            try:
                self._ep.libRegisterThread(name)
            except Exception:
                pass

    # ---------- 生命周期 ----------

    def start(self, local_ip: str, local_port: int) -> None:
        ep = pj.Endpoint()
        ep.libCreate()
        cfg = pj.EpConfig()
        cfg.uaConfig.maxCalls = 8
        cfg.uaConfig.threadCnt = 1
        cfg.logConfig.level = 3
        cfg.logConfig.consoleLevel = 3
        cfg.medConfig.ecTailLen = 0
        cfg.medConfig.noVad = True
        self._log_writer = _LogWriter(self._events)
        cfg.logConfig.writer = self._log_writer
        ep.libInit(cfg)

        tp = pj.TransportConfig()
        tp.port = local_port
        if local_ip:
            tp.boundAddress = local_ip
        ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp)
        ep.libStart()
        self._ep = ep

    def shutdown(self, unregister: bool = True) -> None:
        """销毁协议栈。

        unregister=False 用于「业务层已自行发起注销」的场景：pjsua 的注销
        REGISTER 是异步的，且 acc.shutdown() -> pjsua_acc_del() 会立刻
        destroy_regc(acc, PJ_TRUE) 强制销毁 regc；一旦 regc 被销毁，
        平台返回的 401 挑战就再也无法被应答，注销请求无法完成鉴权。
        因此调用方必须先发起注销，再用 wait_unregister_done() 等在途事务
        真正结束，最后以 unregister=False 调用本方法。

        重要（修复 pjsua_call.c:2600 断言崩溃 + 「呼叫中停止」段错误）：
        pjsua2 要求**所有 Call / Account 包装对象在其析构前库必须仍存活**。
        这些对象的 C++ 析构函数会访问 pjsua_var（典型如 ~Call 调用
        pjsua_call_set_user_data(id, NULL)，其内部
        PJ_ASSERT(call_id>=0 && call_id<ua_cfg.max_calls)）。一旦 libDestroy()
        执行，pjsua_var 已被清零，上述断言会因 max_calls==0 而失败并弹出 MSVC
        断言框（用户报的崩溃）。因此必须在 libDestroy() 之前主动断开引用并
        强制 GC，让 native 析构在库存活时完成。

        此外，hangup 是**异步**的：仅发出 BYE 不表示呼叫已结束，底层 invite
        会话要等收到对端 200 才会进入 DISCONNECTED 并被 pjsua 释放，该过程由
        pjsua 内部线程完成。若不等候就 acc.shutdown()/libDestroy，会与仍在
        进行的呼叫析构竞态，触发段错误（特征：空闲停止正常、仅在呼叫活跃时
        点停止才崩）。故挂断后必须**等待所有呼叫真正断开**再销毁。
        """
        # 清空调用字典引用（释放业务侧持有；真正的原生呼叫回收由 libDestroy 统一
        # 完成）。_Call 包装对象已在 device._teardown 的排空阶段随 DISCONNECTED
        # 事件经 drop_call 失去引用并被回收（~Call 在 DISCONNECTED 态仅清
        # user_data、不再 hangup），此处不应再持有其引用。
        with self._lock:
            self._calls.clear()

        # 若调用方要求（非典型路径），先注销账号再销毁。device._teardown 已自行
        # 完成注销，通常以 unregister=False 调用本方法。
        if unregister and self._acc is not None:
            try:
                self._acc.setRegistration(False)
                self.wait_unregister_done()
            except Exception:
                pass

        # 关键：在账号仍存活时、于已注册线程上**显式**调用 libDestroy，由
        # pjsua_destroy 统一强制拆除账号/呼叫/媒体/传输。这保证了 libDestroy 与
        # 账号的可靠时序——避免提前删除账号导致 libDestroy 触碰已释放内存（「呼叫
        # 中停止」闪退的底层根因：空闲停止正常、仅呼叫活跃时点停止才崩）。
        try:
            if self._ep is not None:
                self._ep.libDestroy()
        except Exception as e:
            try:
                self._events.on_log(1, f"[sip] libDestroy 异常: {e}")
            except Exception:
                pass

        # 丢弃 Python 引用；此后 ~Account/~Endpoint 均为安全空操作（状态已
        # DESTROYED，isValid() 为假；且 pjsua_destroy2 对重入有守卫）。
        self._acc = None
        self._log_writer = None
        self._ep = None
        import gc
        gc.collect()

    def wait_unregister_done(self, timeout: float = 5.0) -> bool:
        """等待注销事务结束（收到响应或超时）。返回是否已结束。

        必须在 acc.shutdown() 之前调用，否则 destroy_regc(force) 会
        摧毁 regc，使 401 挑战无法应答。
        """
        return self._unreg_done.wait(timeout=timeout)

    # ---------- 账号 / 注册 ----------

    def create_account(
        self,
        device_uri: str,
        registrar_uri: str,
        auth_username: str,
        password: str,
        expires: int,
        retry_interval: int,
    ) -> None:
        if self._ep is None:
            raise RuntimeError("SIP stack not started")
        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = device_uri
        acc_cfg.regConfig.registrarUri = registrar_uri
        acc_cfg.regConfig.timeoutSec = expires
        acc_cfg.regConfig.retryIntervalSec = retry_interval
        acc_cfg.regConfig.registerOnAdd = False
        acc_cfg.sipConfig.authCreds.append(
            pj.AuthCredInfo("digest", "*", auth_username, 0, password)
        )
        # 关闭 pjsua 自带媒体（媒体由 media/ 层发送）
        acc_cfg.videoConfig.defaultVideoDevice = -1
        acc = _Account(self)
        acc.create(acc_cfg)
        self._acc = acc

    def set_registration(self, enable: bool) -> None:
        """切换注册状态（REGISTER / 注销）。

        pjsua 的注册事务是异步的：若上一个 REGISTER（例如心跳超时后的自动
        重注册，或停机时的注销）尚未完成，再次调用会返回 PJ_EBUSY（171001）。
        停机流程会在极短时间内连续触发「取消注册 -> 重新注册 -> 注销」，
        因此这里对 EBUSY 做有限次退避重试，避免注销 REGISTER 被静默丢弃。
        """
        if self._acc is None:
            raise RuntimeError("account not created")
        if not enable:
            # 标记「正在注销」：_notify_reg_state 会据此置位注销完成信号，
            # 供 shutdown() 在销毁账号前等待事务真正结束。
            self._unreg_done.clear()
            self._unregistering = True
        last_err: Exception | None = None
        # PJ_EBUSY(171001) 表示 regc 上仍有在途注册事务（可能是 pjsua 的
        # 自动 refresh 或心跳超时后的自动重注册）。该事务通常在一个 SIP
        # 事务超时内结束，因此给予约 6 秒的退避重试窗口。
        for attempt in range(60):
            try:
                self._acc.setRegistration(enable)
                return
            except pj.Error as e:
                last_err = e
                if getattr(e, "status", 0) != 171001:   # 仅对 PJSIP_EBUSY 重试
                    raise
                time.sleep(0.1)
        if last_err is not None:
            raise last_err

    # ---------- MESSAGE ----------

    def send_message(self, to_uri: str, content_type: str, body: str,
                     user_data: str = "") -> None:
        """发送带外 MESSAGE。body 为 ASCII 安全 XML（非 ASCII 已转数字字符引用）。"""
        if self._acc is None:
            raise RuntimeError("account not created")
        prm = pj.SendRequestParam()
        prm.method = "MESSAGE"
        prm.txOption.targetUri = to_uri
        prm.txOption.contentType = content_type
        prm.txOption.msgBody = body
        self._acc.sendRequest(prm)

    # ---------- 呼叫 ----------

    def answer_call(self, call_id: int, code: int, sdp: str | None = None) -> None:
        call = self._calls.get(call_id)
        if call is None:
            raise RuntimeError(f"unknown call {call_id}")
        prm = pj.CallOpParam()
        prm.statusCode = code
        if code == 200 and sdp is not None:
            prm.opt.audioCount = 0
            prm.opt.videoCount = 0
            prm.txOption.contentType = "application/sdp"
            prm.txOption.msgBody = sdp
        call.answer(prm)

    def make_call(self, to_uri: str, subject: str, sdp: str) -> int:
        """发起出呼（UAC）。用于国标语音广播：本工具作为语音流接收者主动拉流。

        三个关键点（均已核对本工程补丁与源码）：

        1. ``prm.opt.audioCount = prm.opt.videoCount = 0`` —— 延续「信令优先」，
           不让 pjsua 创建媒体通道/编解码器（本工程自编译版亦已默认置 0）；
        2. ``prm.txOption.msgBody``（application/sdp）—— 业务层 SDP 覆盖自动生成体。
           补丁位于 ``pjsua_process_msg_data()``，该函数由 ``invite_session()``
           （``pjsua_call.c:564``）在构造外发 INVITE 时调用，故对 UAC 同样生效；
        3. ``prm.txOption.headers`` 追加 Subject 头域（附录 K，媒体链路标识）。

        无媒体导致的 ENOMEDIA 由 ``pjsua_call.c:5408`` 的补丁在任意 INVITE
        状态下容忍，呼叫可正常进入 CONFIRMED。
        """
        if self._acc is None:
            raise RuntimeError("account not created")
        call = _Call(self, self._acc, pj.PJSUA_INVALID_ID)
        prm = pj.CallOpParam(True)
        prm.opt.audioCount = 0
        prm.opt.videoCount = 0
        prm.txOption.targetUri = to_uri
        prm.txOption.contentType = "application/sdp"
        prm.txOption.msgBody = sdp
        if subject:
            hdr = pj.SipHeader()
            hdr.hName = "Subject"
            hdr.hValue = subject
            prm.txOption.headers.append(hdr)
        try:
            call.makeCall(to_uri, prm)
        except Exception:
            # 出呼失败时不能把包装对象留在字典里，否则退出时析构顺序会被打乱
            try:
                del call
            except Exception:
                pass
            raise
        call_id = call.getId()
        with self._lock:
            self._calls[call_id] = call
        return call_id

    def hangup_call(self, call_id: int) -> None:
        call = self._calls.get(call_id)
        if call is None:
            return
        try:
            call.hangup(pj.CallOpParam())
        except Exception:
            pass

    def drop_call(self, call_id: int) -> None:
        """业务层确认会话已结束后调用，清理本地引用。"""
        self._calls.pop(call_id, None)
