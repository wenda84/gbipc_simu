"""GUI 端到端集成测试（离屏）。

在真实 MainWindow 实例上程序化触发「启动」，迷你平台在后台线程完成
注册(401 Digest) -> 心跳 -> DeviceInfo -> TCP 呼叫，期间校验：
  1. UI 线程确实通过信号槽收到了注册状态并刷新了状态页 / 状态栏 / 日志页；
  2. 停止按钮后台停止 + 注销链路不卡 UI。

运行：
  runtime\\venv312\\Scripts\\python.exe .buildtmp/gui_e2e.py
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow
from tests.mini_platform import (
    MiniPlatform, SERVER_ID, DEVICE_ID, PASSWORD, PLATFORM_IP, PLATFORM_PORT,
)

R = {"reg_ok": False, "ka_ok": False, "di_ok": False, "tcp_ok": False,
     "unreg_ok": False, "ui_online": False, "ui_sb_online": False,
     "ui_log_lines": 0, "phase": "-", "err": "", "boxes": []}


def pick_ps() -> str:
    for name in ("g7221.ps", "HK264.ps", "g711a.ps"):
        p = os.path.join("resource", "ps_samples", name)
        if os.path.exists(os.path.join(ROOT, p)):
            return p
    raise FileNotFoundError("找不到 PS 素材文件")


class FakeBox:
    """替身 QMessageBox，避免测试期间弹窗阻塞事件循环。"""

    @staticmethod
    def _rec(kind, title, text):
        R["boxes"].append(f"{kind}:{title}:{text}")

    warning = critical = information = lambda *a, **k: FakeBox._rec("box", a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else "")


def main() -> int:
    import app.ui.main_window as mwmod
    mwmod.QMessageBox = FakeBox            # 仅在当前进程替换，不落盘

    app = QApplication(sys.argv)
    win = MainWindow()

    cfg = win.cfg
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
    cfg.ps_file = pick_ps()
    win.settings.write_from(cfg)
    print(f"[e2e] PS 素材: {cfg.ps_file}", flush=True)

    pf = MiniPlatform()
    pf.start()
    deadline_ext = {"stopping": False}

    # ---------- 平台（后台线程） ----------
    def platform_worker() -> None:
        try:
            R["phase"] = "注册"
            R["reg_ok"] = pf.expect_register()
            R["phase"] = "心跳"
            R["ka_ok"] = pf.expect_keepalive()
            R["phase"] = "DeviceInfo"
            R["di_ok"] = pf.query_device_info()
            R["phase"] = "TCP 呼叫"
            R["tcp_ok"] = pf.run_call(use_tcp=True)
            R["phase"] = "注销等待"
            R["unreg_ok"] = pf.expect_unregister(timeout=15)
            R["phase"] = "完成"
        except Exception as e:      # pragma: no cover
            R["err"] = f"{R['phase']} 异常: {e}"

    threading.Thread(target=platform_worker, name="pf", daemon=True).start()

    # ---------- UI 侧：延迟启动设备 ----------
    QTimer.singleShot(400, win._on_start)

    # ---------- 轮询：观察 UI 是否被正确刷新 ----------
    tick = {"n": 0}

    def _wait_stopped() -> None:
        """等待后台停止线程完成后再收尾，而不是赌一个固定延时。"""
        stopped = win.act_start.isEnabled() and not win.act_stop.isEnabled()
        elapsed = time.time() - deadline_ext.get("t0", time.time())
        if stopped:
            R["stop_elapsed"] = round(elapsed, 2)
            R["after_stop_userdata"] = win.status.lb_reg_state.text()
            QTimer.singleShot(200, app.quit)
            return
        if elapsed > 20:
            R["stop_elapsed"] = round(elapsed, 2)
            R["err"] = f"停止超时({elapsed:.1f}s)，UI 未回到未运行状态"
            app.quit()
            return
        QTimer.singleShot(200, _wait_stopped)

    def _poll() -> None:
        tick["n"] += 1
        try:
            if win.status.lb_reg_state.text() == "在线":
                R["ui_online"] = True
            if win.lb_sb_state.text() == "在线":
                R["ui_sb_online"] = True
            R["ui_log_lines"] = win.log.view.toPlainText().count("\n")
            if win.status.lb_call_state.text() != "空闲":
                R.setdefault("ui_call", []).append(win.status.lb_call_state.text())
        except Exception as e:      # 观察点异常不应中断轮询
            R["err"] = f"轮询异常: {e}"

        # 四个业务阶段跑完即触发 UI 停止（注销由 UI 侧发起，平台侧已在等待）
        if R["tcp_ok"] and not deadline_ext["stopping"]:
            deadline_ext["stopping"] = True
            deadline_ext["t0"] = time.time()
            win._on_stop()
            QTimer.singleShot(200, _wait_stopped)
            return

        if R["err"] or tick["n"] > 300:      # 300 * 200ms = 60s
            app.quit()
            return
        QTimer.singleShot(200, _poll)

    QTimer.singleShot(1200, _poll)
    # 硬超时兜底
    QTimer.singleShot(90000, app.quit)
    app.exec()

    # ---------- 断言 ----------
    pf.stop()
    time.sleep(0.5)

    checks = [
        ("注册(401 Digest)", R["reg_ok"]),
        ("心跳", R["ka_ok"]),
        ("DeviceInfo 查询应答", R["di_ok"]),
        ("TCP 呼叫全流程", R["tcp_ok"]),
        ("注销", R["unreg_ok"]),
        ("UI 状态页刷新为「在线」", R["ui_online"]),
        ("UI 状态栏刷新为「在线」", R["ui_sb_online"]),
        ("UI 日志页收到设备日志", R["ui_log_lines"] > 5),
        ("停止后 UI 回到「未运行」", win.act_start.isEnabled()
         and not win.act_stop.isEnabled()),
        ("过程中无异常弹窗", not R["boxes"]),
    ]
    print("\n========== GUI 端到端结果 ==========")
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if R["err"]:
        print("  异常:", R["err"])
    if R["boxes"]:
        for b in R["boxes"]:
            print("  弹窗:", b)
    if pf.failures:
        print("  平台校验失败项:")
        for f in pf.failures:
            print("   -", f)

    failed = [n for n, ok in checks if not ok]
    if failed or pf.failures or R["err"]:
        print(f"\n>>> 失败 {len(failed)} 项：{failed}")
        return 1
    print("\n>>> GUI 端到端全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
