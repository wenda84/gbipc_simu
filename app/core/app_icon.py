"""应用图标定位。

兼容三种运行布局，统一返回 `ipc.ico` / `ipc.png` 的绝对路径：
  - 源码态：       `<工程根>/app/core/app_icon.py` → 上溯两级到 `<工程根>/resource/ui`
  - PyInstaller onedir：`<分发根>/_internal/app/core/...` → 上溯到 `<分发根>/_internal/resource/ui`
  - PyInstaller onefile： `<sys._MEIPASS>/app/core/...` → 上溯到 `<sys._MEIPASS>/resource/ui`

`build_exe.spec` 通过 `datas` 把 `resource/ui/ipc.{ico,png}` 打进包内同相对路径，
因此同一解析逻辑在三种环境下都能命中。
"""
from __future__ import annotations

import os


def resolve_app_icon() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))  # .../app/core
    # 候选目录：app/core 的父级、祖父级（覆盖 _internal / 工程根 / sys._MEIPASS）
    for d in (os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        for name in ("ipc.ico", "ipc.png"):
            cand = os.path.join(d, "resource", "ui", name)
            if os.path.exists(cand):
                return cand
    return None
