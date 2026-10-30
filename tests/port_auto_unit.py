"""端口自动分配单元测试（无需真实 pjsua2 / 网络）。

覆盖「本地 SIP 端口 / 本机媒体端口 支持留空」：
    python tests/port_auto_unit.py

A. local_sip_port = 0 → start() 自动选一个非零空闲端口并记录到 device.local_sip_port；
B. local_sip_port = 固定值 → 原样透传给协议栈；
C. local_media_port = 0 → 应答 INVITE 时自动选空闲端口，写进 200 OK 的 SDP，
   并记录在 CallContext.media_port（供 PsStreamer 绑定同一端口）；
D. local_media_port = 固定值 → SDP 用配置值。
"""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_pjsua2_stub() -> None:
    """真实 pjsua2 缺失时注入最小桩模块（只为本模块能 import device）。"""
    try:
        import pjsua2  # noqa: F401
        return
    except Exception:
        pass

    mod = types.ModuleType("pjsua2")

    class Error(Exception):
        pass

    class _Base:
        def __init__(self, *a, **k):
            pass

    for name in ("LogWriter", "Call", "Account", "Endpoint", "TransportConfig",
                 "EpConfig", "AccountConfig", "AuthCredInfo", "CallOpParam",
                 "SendRequestParam", "SipHeader"):
        setattr(mod, name, type(name, (_Base,), {}))
    for name in ("PJSIP_INV_STATE_NULL", "PJSIP_INV_STATE_CALLING",
                 "PJSIP_INV_STATE_INCOMING", "PJSIP_INV_STATE_EARLY",
                 "PJSIP_INV_STATE_CONNECTING", "PJSIP_INV_STATE_CONFIRMED",
                 "PJSIP_INV_STATE_DISCONNECTED"):
        setattr(mod, name, 0)
    mod.Error = Error
    mod.PJSIP_TRANSPORT_UDP = 1
    mod.PJSUA_INVALID_ID = -1
    sys.modules["pjsua2"] = mod


_install_pjsua2_stub()

from app.core.config import AppConfig                       # noqa: E402
from app.core.device import GbDevice                        # noqa: E402
from app.sip.events import IncomingCallEvent                # noqa: E402
from app.sip.stack import SipStack                          # noqa: E402

PLAY_OFFER = "\r\n".join([
    "v=0",
    "o=34020100002000000178 0 0 IN IP4 172.220.0.178",
    "s=Play",
    "c=IN IP4 172.220.0.178",
    "t=0 0",
    "m=video 21006 TCP/RTP/AVP 96",
    "a=setup:passive",
    "a=connection:new",
    "a=recvonly",
    "a=rtpmap:96 PS/90000",
    "y=0000000018",
    "",
])


class FakeStack(SipStack):
    """协议栈替身：只记录调用。"""

    def __init__(self):
        self.started = None
        self.answers = []

    def start(self, local_ip, local_port):
        self.started = (local_ip, local_port)

    def create_account(self, *a, **k):
        pass

    def register_thread(self, name):
        pass

    def set_registration(self, enable):
        pass

    def wait_unregister_done(self, timeout=5.0):
        return True

    def send_message(self, to_uri, content_type, body, user_data=""):
        pass

    def shutdown(self, unregister=True):
        pass

    def answer_call(self, call_id, code, sdp=None):
        self.answers.append((call_id, code, sdp))

    def hangup_call(self, call_id):
        pass

    def drop_call(self, call_id):
        pass

    def make_call(self, to_uri, subject, sdp):
        self.answers.append(("make_call", to_uri, subject))
        return 1


def _dev(**kw) -> tuple[GbDevice, FakeStack]:
    cfg = AppConfig()
    cfg.local_ip = "127.0.0.1"
    for k, v in kw.items():
        setattr(cfg, k, v)
    st = FakeStack()
    return GbDevice(cfg, sip_stack=st), st


def test_sip_port_auto():
    dev, st = _dev(local_sip_port=0)
    dev.start()
    port = st.started[1]
    assert port and 1 <= port <= 65535, port
    assert dev.local_sip_port == port, (dev.local_sip_port, port)
    dev._stop.set()


def test_sip_port_fixed():
    dev, st = _dev(local_sip_port=5099)
    dev.start()
    assert st.started[1] == 5099, st.started
    assert dev.local_sip_port == 5099
    dev._stop.set()


def _answer(dev, st):
    ev = IncomingCallEvent(call_id=1, from_uri="sip:a@h", to_uri="sip:b@h",
                           subject="", offer_sdp=PLAY_OFFER)
    dev._on_incoming_call(ev)
    assert st.answers, "未回 200 OK"
    return st.answers[-1][2]


def test_media_port_auto():
    dev, st = _dev(local_media_port=0)
    sdp = _answer(dev, st)
    ctx = dev._calls[1]
    assert ctx.media_port and 1 <= ctx.media_port <= 65535, ctx.media_port
    assert f"m=video {ctx.media_port} " in sdp, sdp


def test_media_port_busy_falls_back():
    """配置的媒体端口被占用时，SDP 应改用可用的空闲端口（不再通告占不到的端口）。"""
    import socket
    # offer 是 TCP 媒体，所以占用「TCP」端口才能模拟真实冲突
    hold = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    hold.bind(("0.0.0.0", 0))
    hold.listen(1)
    busy = hold.getsockname()[1]
    try:
        dev, st = _dev(local_media_port=busy)
        sdp = _answer(dev, st)
        ctx = dev._calls[1]
        assert ctx.media_port != busy, (ctx.media_port, busy)
        assert f"m=video {ctx.media_port} " in sdp, sdp
    finally:
        hold.close()


def test_media_port_fixed():
    dev, st = _dev(local_media_port=15088)
    sdp = _answer(dev, st)
    assert "m=video 15088 " in sdp, sdp
    assert dev._calls[1].media_port == 15088


if __name__ == "__main__":
    for fn in (test_sip_port_auto, test_sip_port_fixed,
               test_media_port_auto, test_media_port_busy_falls_back,
               test_media_port_fixed):
        fn()
        print("OK", fn.__name__)
    print("ALL_TESTS_OK")
