"""全链路自测：被测设备(GbDevice+PjsipStack+PsStreamer) vs 迷你平台。

覆盖：注册(401 Digest) -> 心跳 -> DeviceInfo 查询应答 ->
TCP 呼叫(RFC4571/SSRC/PT/PS 校验) -> UDP 呼叫 -> BYE 停流 -> 注销。

运行：
  runtime\\venv312\\Scripts\\python.exe tests\\selftest_full.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import AppConfig
from app.core.device import GbDevice, DeviceEvents, RegState
from tests.mini_platform import (
    MiniPlatform, SERVER_ID, DEVICE_ID, PASSWORD, PLATFORM_IP, PLATFORM_PORT,
)


class PrintEvents(DeviceEvents):
    def on_log(self, msg):
        print(f"[device] {msg}", flush=True)

    def on_reg_state(self, state, detail=""):
        print(f"[device] 注册状态: {state.value} {detail}", flush=True)

    def on_call_state(self, text):
        print(f"[device] 呼叫状态: {text}", flush=True)

    def on_stream_stats(self, text):
        print(f"[device] 推流: {text}", flush=True)


def main():
    ps_file = os.path.join("resource", "ps_samples", "g7221.ps")
    cfg = AppConfig(
        local_ip="127.0.0.1",
        local_sip_port=5060,
        server_id=SERVER_ID,
        server_domain=f"{PLATFORM_IP}:{PLATFORM_PORT}",
        server_ip=PLATFORM_IP,
        server_port=PLATFORM_PORT,
        device_id=DEVICE_ID,
        auth_username=DEVICE_ID,
        password=PASSWORD,
        register_expires=3600,
        register_retry_interval=3,
        keepalive_interval=2,
        keepalive_timeout_count=3,
        local_media_port=15060,
        ps_file=ps_file,
        loop_play=True,
        device_name="GBIPC-SIM-Test",
        manufacturer="Simulator",
        model="GBIPC-SIM-1000",
        firmware="V1.0.0",
        channel=1,
        civil_code="340200",
    )

    pf = MiniPlatform()
    pf.start()
    dev = GbDevice(cfg, PrintEvents())
    dev.start()

    steps = [
        ("注册(401 Digest)", lambda: pf.expect_register()),
        ("心跳", lambda: pf.expect_keepalive()),
        ("DeviceInfo 查询应答", lambda: pf.query_device_info()),
        ("TCP 呼叫全流程", lambda: pf.run_call(use_tcp=True)),
        ("UDP 呼叫全流程", lambda: pf.run_call(use_tcp=False)),
    ]
    for name, fn in steps:
        print(f"\n===== 阶段: {name} =====", flush=True)
        ok = fn()
        if not ok:
            print(f"\n>>> 自测失败于阶段: {name}", flush=True)
            break
        print(f">>> {name} PASS", flush=True)
    else:
        print("\n===== 阶段: 注销 =====", flush=True)
        # 注销需要在设备存活期间完成 401 挑战应答：dev.stop() 会在 teardown
        # 中发起注销并随后销毁注册上下文，因此必须先在后台线程里启动
        # expect_unregister（它会即时回 401），再触发 dev.stop()。
        import threading
        result = {}

        def _wait_unreg():
            result["ok"] = pf.expect_unregister(timeout=15)

        t = threading.Thread(target=_wait_unreg, daemon=True)
        t.start()
        time.sleep(0.3)          # 让夹具先进入等待 REGISTER 的状态
        dev.stop()
        t.join(timeout=20)
        ok = result.get("ok", False)
        if ok:
            print(">>> 注销 PASS", flush=True)

    try:
        dev.stop()
    except Exception:
        pass
    pf.stop()

    if pf.failures:
        print(f"\n========== 自测失败 {len(pf.failures)} 处 ==========")
        for f in pf.failures:
            print(" -", f)
        sys.exit(1)
    print("\n========== 全部自测通过 ==========")
    sys.exit(0)


if __name__ == "__main__":
    main()
