r"""GUI 启动入口。

用法（在工程根目录执行）：
  runtime\venv312\Scripts\python.exe -m app

也可直接运行：
  runtime\venv312\Scripts\python.exe app\__main__.py
"""
from __future__ import annotations

import os
import sys

# 源码运行：把工程根目录加入 sys.path，确保 `import app` 可用。
# 兼容两种启动方式：`python -m app`（Python 已自动包含 cwd）与
# `python app/__main__.py`（此时 cwd 未必是工程根，需手动补）。
# 打包后（sys.frozen=True）由 PyInstaller 接管路径，不再需要此处理。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QIcon
    except ImportError:
        print("未安装 PySide6。请执行：\n"
              "  runtime\\venv312\\Scripts\\python.exe -m pip install "
              "-i https://mirrors.aliyun.com/pypi/simple/ PySide6",
              file=sys.stderr)
        return 2

    from app.core.app_icon import resolve_app_icon
    from app.core.applog import install as install_applog, mark_quit
    from app.ui.main_window import MainWindow

    # 应用日志（崩溃/异常本地化）：复现偶发崩溃时落盘确定性的线程栈与未捕获异常
    _log = install_applog()
    if _log:
        print(f"[applog] 应用日志: {_log}")

    app = QApplication(sys.argv)
    app.setApplicationName("GB28181 IPC 模拟工具")
    # 收起到系统托盘后窗口被隐藏/关闭，但应用需继续驻留；
    # 真正的退出只经由托盘菜单「退出」，故关闭最后一个窗口不自动退出。
    app.setQuitOnLastWindowClosed(False)
    # 干净退出标记，用于区分「正常退出」与「崩溃」
    app.aboutToQuit.connect(mark_quit)
    # 应用图标：同时作用于窗口标题栏与任务栏运行按钮
    # （exe 文件图标由 build_exe.spec 的 icon= 控制，用于桌面/资源管理器）。
    _icon_path = resolve_app_icon()
    if _icon_path:
        app.setWindowIcon(QIcon(_icon_path))

    # 单实例（按 exe 路径）：同路径已运行时，仅把其窗口恢复到前台，本进程退出。
    from app.core.single_instance import SingleInstance
    si = SingleInstance()
    if si.is_another_running():
        si.notify_foreground()
        return 0

    win = MainWindow()
    win.show()
    # 注册为唯一实例：后续同路径启动会唤醒本窗口而非新开。
    si.listen(win.bring_to_front)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
