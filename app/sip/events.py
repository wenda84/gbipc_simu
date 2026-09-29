"""SIP 层中性事件对象。

业务层只依赖这些纯 dataclass，不出现任何 pjsua2 类型。
body 统一为 bytes（MANSCDP 为 GBK 编码），由业务层解码。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RegStateEvent:
    is_active: bool          # 注册是否生效
    code: int                # SIP 状态码（200 / 401 / 408 ...）
    reason: str = ""


@dataclass
class MessageEvent:
    """收到带外 MESSAGE（MANSCDP Query 等）。SIP 层已自动回 200 OK。"""
    from_uri: str
    to_uri: str
    content_type: str
    body: bytes              # 原始字节（GBK）
    raw: str = ""            # 原始报文（调试用，可能截断）


@dataclass
class IncomingCallEvent:
    call_id: int
    from_uri: str
    to_uri: str
    subject: str
    offer_sdp: str           # INVITE 携带的 SDP offer（ASCII）
    raw: str = ""


@dataclass
class CallStateEvent:
    call_id: int
    state: str               # EARLY / CONNECTING / CONFIRMED / DISCONNECTED
    status_code: int = 0
    reason: str = ""


@dataclass
class RemoteSdpEvent:
    """对端消息携带的 SDP 原文（出呼场景下即 200 OK 的 SDP）。

    时序说明：pjsua 先回调 ``onCallState(CONFIRMED)``，再回调
    ``onCallTsxState``（携带收到的响应报文），因此本事件**晚于** CONFIRMED。
    业务层用它做日志与 SSRC 交叉校验，不作为媒体启动的前提。
    """
    call_id: int
    sdp: str = ""


@dataclass
class MessageStatusEvent:
    """发出 MESSAGE 的投递结果。"""
    to_uri: str
    code: int
    reason: str = ""
    body_head: str = ""      # 消息体前若干字符，用于关联 Keepalive
