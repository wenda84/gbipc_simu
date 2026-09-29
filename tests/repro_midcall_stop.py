"""复现「呼叫中停止」闪退（不依赖 Qt）。

问题特征：空闲停止正常，但只要在「呼叫仍活跃（未收到 BYE）」时点停止，
shutdown() 会在 pjsua 仍有活跃呼叫时析构协议栈，触发
pjsua_call.c:2600「call_id>=0 && call_id<max_calls」原生断言（崩溃 A 同类）。

本脚本直接驱动 GbDevice + MiniPlatform（不建窗口、不建 QApplication）：
  1. 注册 -> 心跳 -> INVITE -> 200 -> ACK -> 媒体流（平台不回 BYE，呼叫保持活跃）；
  2. 主线程调用 device.stop()（模拟「呼叫中点停止」）；
  3. 设备应主动发 BYE，平台应答 200 并确认媒体停流；
  4. 进程应干净退出（退出码 0、无 Assertion failed / 2600 / 658）。

判定：到达并打印 MIDCALL_STOP_OK 且退出码为 0 即通过；若中途出现 MSVC
断言，进程会以非 0 退出码（通常 127）终止，且 stderr 含 "Assertion failed"。

运行：
  runtime\\venv312\\Scripts\\python.exe tests/repro_midcall_stop.py
"""
from __future__ import annotations

import os
import sys
import time
import threading
import faulthandler

faulthandler.enable()
faulthandler.dump_traceback_later(30, exit=True)  # 卡死兜底

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.device import GbDevice, DeviceEvents
from app.core.config import AppConfig
from tests.mini_platform import (
    MiniPlatform, SERVER_ID, DEVICE_ID, PASSWORD, PLATFORM_IP, PLATFORM_PORT,
)


class Recorder(DeviceEvents):
    def __init__(self):
        self.last_call = ""
        self.logs = []
    def on_log(self, msg):
        self.logs.append(msg)
        print(f"[pjsua] {msg}", flush=True)
    def on_call_state(self, text):
        self.last_call = text
    def on_reg_state(self, state, detail=""):
        pass
    def on_stream_stats(self, text):
        pass


def main() -> int:
    cfg = AppConfig()
    cfg.local_ip = "127.0.0.1"
    cfg.server_id = SERVER_ID
    cfg.server_domain = f"{PLATFORM_IP}:{PLATFORM_PORT}"
    cfg.server_ip = PLATFORM_IP
    cfg.server_port = PLATFORM_PORT
    cfg.device_id = DEVICE_ID
    cfg.auth_username = DEVICE_ID
    cfg.password = PASSWORD
    cfg.register_retry_interval = 3
    cfg.keepalive_interval = 2
    cfg.keepalive_timeout_count = 3
    cfg.ps_file = os.path.join("resource", "ps_samples", "g711a.ps")
    cfg.unregister_grace = 2.0

    rec = Recorder()
    pf = MiniPlatform()
    pf.start()
    dev = GbDevice(cfg, rec)
    R = {}

    def pf_worker() -> None:
        try:
            R["reg"] = pf.expect_register()
            R["ka"] = pf.expect_keepalive()
            if not os.environ.get("NO_CALL"):
                R["call_id"] = pf.run_call(use_tcp=True, send_bye=False)
                R["call_active"] = bool(R["call_id"])
                # 设备被停止后会主动发 BYE，这里应答并校验停流
                R["bye"] = pf.expect_bye(R["call_id"]) if R["call_id"] else False
            else:
                R["call_active"] = True
            # 停止流程（device._teardown）会主动发起注销（REGISTER Expires:0）。
            # 真实平台会应答使其注册事务真正结束；此处由夹具后台应答，避免
            # 注销事务悬而未决导致 pjsua_acc_del 在 regc busy 时崩溃。
            R["unreg"] = pf.expect_unregister(timeout=15)
        except Exception as e:  # pragma: no cover
            R["pf_err"] = f"{e}"

    threading.Thread(target=pf_worker, name="pf", daemon=True).start()

    dev.start()

    # 等待呼叫真正建立并进入推流（NO_CALL 时跳过）
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and not R.get("call_active"):
        time.sleep(0.05)
    if not R.get("call_active"):
        print("FAIL: 呼叫始终未建立（call_id 缺失）")
        if R.get("pf_err"):
            print("pf_err:", R["pf_err"])
        return 2

    print("[repro] 模拟停止 -> device.stop() ...")
    # 若此处触发 pjsua 原生断言，进程会以非 0 退出码终止，下面不会打印。
    dev.stop()

    time.sleep(1.0)
    print("[repro] device.stop() 已返回（未崩溃）")
    if R.get("pf_err"):
        print("pf_err:", R["pf_err"])
    print(f"[repro] 平台侧: reg={R.get('reg')} ka={R.get('ka')} "
          f"bye_answered={R.get('bye')}")
    if not os.environ.get("NO_CALL") and R.get("bye") is not True:
        print("FAIL: 平台未收到/未应答设备 BYE，或媒体未停流")
        return 1
    print("MIDCALL_STOP_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
