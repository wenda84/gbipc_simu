"""SIP 协议栈抽象接口。

业务层仅依赖此接口；当前唯一实现为 PjsipStack（pjsua2）。
"""
from __future__ import annotations

import abc
from typing import Callable

from .events import (
    RegStateEvent, MessageEvent, IncomingCallEvent, CallStateEvent, MessageStatusEvent,
    RemoteSdpEvent,
)


class SipEvents(abc.ABC):
    """业务层实现的事件回调集合（在 SIP 适配层线程被调用，须只做投递）。"""

    @abc.abstractmethod
    def on_reg_state(self, ev: RegStateEvent) -> None: ...

    @abc.abstractmethod
    def on_message(self, ev: MessageEvent) -> None: ...

    @abc.abstractmethod
    def on_incoming_call(self, ev: IncomingCallEvent) -> None: ...

    @abc.abstractmethod
    def on_call_state(self, ev: CallStateEvent) -> None: ...

    @abc.abstractmethod
    def on_remote_sdp(self, ev: RemoteSdpEvent) -> None:
        """对端在呼叫过程中携带的 SDP（出呼时即 200 OK 的 SDP）。"""

    @abc.abstractmethod
    def on_message_status(self, ev: MessageStatusEvent) -> None: ...

    @abc.abstractmethod
    def on_log(self, level: int, msg: str) -> None: ...


class SipStack(abc.ABC):
    """协议栈抽象：注册 / MESSAGE / 呼叫应答。"""

    @abc.abstractmethod
    def start(self, local_ip: str, local_port: int) -> None: ...

    @abc.abstractmethod
    def shutdown(self, unregister: bool = True) -> None:
        """销毁协议栈。

        unregister=False 表示业务层已自行完成注销，适配层不得再次发起
        注销（否则会破坏在途的注销事务，导致 REGISTER 发不出去）。
        """

    @abc.abstractmethod
    def register_thread(self, name: str) -> None:
        """把当前线程注册给 pjlib，避免 os_core_win32.c:658 断言。

        主线程由 libInit 内部的 pj_init 自动注册；worker 线程必须显式调用。
        """

    @abc.abstractmethod
    def create_account(
        self,
        device_uri: str,
        registrar_uri: str,
        auth_username: str,
        password: str,
        expires: int,
        retry_interval: int,
    ) -> None: ...

    @abc.abstractmethod
    def set_registration(self, enable: bool) -> None:
        """enable=True 发起注册；False 注销（Expires=0）。"""

    @abc.abstractmethod
    def wait_unregister_done(self, timeout: float = 5.0) -> bool:
        """等待注销事务结束（收到响应或超时），返回是否已结束。

        必须在 shutdown() 前调用：销毁账号会强制销毁注册上下文，
        使后续的 401 挑战无法应答。
        """

    @abc.abstractmethod
    def send_message(self, to_uri: str, content_type: str, body: str,
                     user_data: str = "") -> None:
        """发送带外 MESSAGE（MANSCDP）。body 为 ASCII 安全的 XML 字符串
        （非 ASCII 字符已由 manscdp 转义为数字字符引用）。"""

    @abc.abstractmethod
    def answer_call(self, call_id: int, code: int, sdp: str | None = None) -> None:
        """应答 INVITE。code=200 时 sdp 为自定义 SDP 全文。"""

    @abc.abstractmethod
    def make_call(self, to_uri: str, subject: str, sdp: str) -> int:
        """发起出呼（UAC），返回 call_id。

        国标语音广播中本工具是「语音流接收者」，需主动向语音流发送者发起
        INVITE（9.12.1.2 信令 5），因此必须支持 UAC 方向。

        - ``to_uri``：请求目标（形如 ``sip:<SourceID>@<平台域>``），同时作为 To 头域；
        - ``subject``：附录 K 的 Subject 头域，用作媒体链路标识；
        - ``sdp``：业务层构造的 Offer 全文，以 ``application/sdp`` 覆盖协议栈自动生成体。

        调用为非阻塞：呼叫进展经 ``CallStateEvent`` 上报。失败抛异常。
        """

    @abc.abstractmethod
    def hangup_call(self, call_id: int) -> None: ...

    @abc.abstractmethod
    def drop_call(self, call_id: int) -> None:
        """业务层确认会话已结束后调用，清理适配层持有的呼叫引用。"""
