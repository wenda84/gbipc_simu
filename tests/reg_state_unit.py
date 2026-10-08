"""注册状态机纯单元测试（无需真实 pjsua2 / 网络 / 音频）。

覆盖本次修复的四个 P0 缺陷，全部以注入的 FakeStack 与桩 pjsua2 完成，
可在无头 CI 上直接运行：

    python tests/reg_state_unit.py

A. 注销成功回调（is_active=False + 200）不得被判为「注册未成功」；
B. set_registration(True) 必须复位 _unregistering，否则「注销完成」信号
   会被后续任何注册失效回调误置位；
C. 心跳超时直接重注册，不再先发注销 REGISTER（Expires=0）；
D. 停止时以 _ever_registered（而非当前 state）决定是否需要注销，覆盖
   「曾 200 OK、现因心跳超时/刷新失败处于 RETRYING」的场景。
"""
from __future__ import annotations

import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_pjsua2_stub() -> None:
    """真实 pjsua2 缺失时（无头 CI / 未构建）注入最小桩模块。

    app.core.device 经由 app.sip.pjsip_stack 顶层 import pjsua2，本机测试的
    是业务层状态机，与 pjsua 无关，因此桩只需让模块可导入、类名可继承。
    """
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

from app.core.config import AppConfig                      # noqa: E402
from app.core.device import DeviceEvents, GbDevice, RegState             # noqa: E402
from app.sip.events import RegStateEvent                   # noqa: E402
from app.sip.pjsip_stack import PjsipStack                 # noqa: E402
from app.sip.stack import SipEvents, SipStack              # noqa: E402


class Recorder(DeviceEvents):
    """业务层事件（DeviceEvents）最小实现，只记账。"""

    def __init__(self):
        self.logs = []

    def on_log(self, msg):
        self.logs.append(msg)


class SipRecorder(SipEvents):
    """SIP 适配层事件（SipEvents）最小实现，只记账。"""

    def __init__(self):
        self.logs = []

    def on_reg_state(self, ev):
        pass

    def on_message(self, ev):
        pass

    def on_incoming_call(self, ev):
        pass

    def on_call_state(self, ev):
        pass

    def on_remote_sdp(self, ev):
        pass

    def on_message_status(self, ev):
        pass

    def on_log(self, level, msg):
        self.logs.append(msg)


class FakeStack(SipStack):
    """协议栈替身：记录注册/注销/销毁调用，不触碰 pjsua。"""

    def __init__(self):
        self.reg_calls: list[bool] = []
        self.shutdown_calls: list[bool] = []
        self.unreg_result = True

    def start(self, local_ip, local_port):
        pass

    def create_account(self, device_uri, registrar_uri, auth_username,
                       password, expires, retry_interval):
        pass

    def register_thread(self, name):
        pass

    def set_registration(self, enable):
        self.reg_calls.append(enable)

    def wait_unregister_done(self, timeout=5.0):
        return self.unreg_result

    def send_message(self, to_uri, content_type, body, user_data=""):
        pass

    def shutdown(self, unregister=True):
        self.shutdown_calls.append(unregister)

    def answer_call(self, call_id, code, sdp=None):
        pass

    def make_call(self, to_uri, subject, sdp):
        raise RuntimeError("not used in this test")

    def hangup_call(self, call_id):
        pass

    def drop_call(self, call_id):
        pass


def _new_device():
    cfg = AppConfig()
    cfg.local_ip = "127.0.0.1"
    stack = FakeStack()
    dev = GbDevice(cfg, Recorder(), stack)
    dev._stop.clear()
    return dev, stack


_FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILURES.append(name)


def case_a_unregister_callback_not_mistaken() -> None:
    print("A. 注销成功回调不得被判为注册失败")
    dev, stack = _new_device()
    dev._set_state(RegState.REGISTERING)
    dev._on_reg_state(RegStateEvent(is_active=True, code=200, reason="OK"))
    check("注册 200 OK 后进入在线", dev.state == RegState.ONLINE, dev.state.value)
    check("置位 _ever_registered", dev._ever_registered is True)

    # 平台侧正常，但服务端把本端注册解除（is_active=False + 200）
    before_retry = dev._next_retry_at
    dev._on_reg_state(RegStateEvent(is_active=False, code=200, reason="OK"))
    logs = "\n".join(dev.out.logs)
    check("日志不再误报『注册未成功』", "注册未成功" not in logs)
    check("转入重试中以便重注册", dev.state == RegState.RETRYING, dev.state.value)
    wait = dev._next_retry_at - time.monotonic()
    check("重试时刻未按注册间隔(60s)推迟", wait <= 5, f"约 {wait:.1f}s 后重试")
    assert before_retry is not None


def case_b_unregistering_flag_reset() -> None:
    print("B. set_registration(True) 必须复位 _unregistering")

    class FakeAcc:
        def __init__(self):
            self.calls = []

        def setRegistration(self, enable):
            self.calls.append(enable)

    stk = PjsipStack(SipRecorder())
    stk._acc = FakeAcc()
    stk.set_registration(True)
    stk.set_registration(False)
    check("发起注销后 _unregistering 为真", stk._unregistering is True)
    stk.set_registration(True)
    check("再次注册后 _unregistering 已复位", stk._unregistering is False)

    # 复位后，运行期一次普通的注册失效回调不应点亮「注销完成」信号
    stk._notify_reg_state(RegStateEvent(is_active=False, code=408, reason="Timeout"))
    check("注销完成信号未被误置位", not stk._unreg_done.is_set())

    # 而真正的注销流程中，失效回调必须点亮该信号
    stk.set_registration(False)
    stk._notify_reg_state(RegStateEvent(is_active=False, code=200, reason="OK"))
    check("注销流程中信号被正确置位", stk._unreg_done.is_set())


def case_c_keepalive_timeout_reregisters() -> None:
    print("C. 心跳超时直接重注册，不再先发注销")
    dev, stack = _new_device()
    dev._set_state(RegState.ONLINE)
    dev._ever_registered = True
    dev._ka_pending = dev.cfg.keepalive_timeout_count
    dev._last_ka_sent = time.monotonic()      # 避免本次 tick 又发一次心跳
    stack.reg_calls.clear()
    dev._on_tick()
    check("转入重试中", dev.state == RegState.RETRYING, dev.state.value)
    check("未发出注销 REGISTER", False not in stack.reg_calls, str(stack.reg_calls))


def case_d_teardown_unregister_criteria() -> None:
    print("D. 停止注销以 _ever_registered 为准")
    # 场景 1：曾在线、现因心跳超时处于 RETRYING —— 必须注销
    dev, stack = _new_device()
    dev._ever_registered = True
    dev._set_state(RegState.RETRYING)
    dev._teardown()
    check("曾在线则发出注销", False in stack.reg_calls, str(stack.reg_calls))
    check("销毁时不再重复注销", stack.shutdown_calls == [False], str(stack.shutdown_calls))
    check("停止后回到未注册", dev.state == RegState.IDLE, dev.state.value)

    # 场景 2：从未拿到 200 OK —— 跳过注销，避免与在途 REGISTER 抢 regc
    dev2, stack2 = _new_device()
    dev2._ever_registered = False
    dev2._set_state(RegState.REGISTERING)
    dev2._teardown()
    check("从未在线则跳过注销", False not in stack2.reg_calls, str(stack2.reg_calls))
    check("仍按 unregister=False 销毁", stack2.shutdown_calls == [False])

    # 场景 3：停止后历史标志被复位，不影响下一次启动
    check("停止后复位 _ever_registered", dev2._ever_registered is False)


def main() -> int:
    case_a_unregister_callback_not_mistaken()
    case_b_unregistering_flag_reset()
    case_c_keepalive_timeout_reregisters()
    case_d_teardown_unregister_criteria()
    print()
    if _FAILURES:
        print(f"失败 {len(_FAILURES)} 项: {_FAILURES}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
