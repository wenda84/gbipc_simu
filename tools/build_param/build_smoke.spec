# -*- mode: python ; coding: utf-8 -*-
"""构建「打包冒烟」控制台程序，用于验证打包后原生依赖可用。

    runtime\\venv312\\Scripts\\python.exe -m PyInstaller tools\\build_param\\build_smoke.spec --noconfirm

与主程序共用同一套隐藏导入/原生 DLL 规则（见 build_exe.spec 注释）。
"""
import os
import sys

# SPECPATH = spec 文件所在目录（tools/build_param/），
# 工程根目录是它的上两级（build_param -> tools -> 工程根）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPECPATH)))
SP = os.path.join(sys.prefix, "Lib", "site-packages")

hiddenimports = [
    "pjsua2._pjsua2",
    "pjsua2.pjsua2",
    "app.core.config", "app.core.device", "app.core.manscdp", "app.core.gbsdp",
    "app.sip.stack", "app.sip.pjsip_stack", "app.media.ps_streamer",
    # 国标语音广播（收流/解码/播放/电平）
    "app.media.alaw", "app.media.pcma_receiver", "app.media.audio_sink",
    "app.media.broadcast_session", "app.ui.volume_meter",
    # sounddevice 的 PortAudio DLL 由 hooks-contrib 的 hook-sounddevice 收集，
    # 这里显式声明以防 hook 缺失时静默丢包（丢失后广播将无法出声）
    "sounddevice", "_sounddevice_data", "_cffi_backend",
    "app.ui.main_window", "app.ui.settings_page",
    "app.ui.status_page", "app.ui.log_page",
]

binaries = []
for _name in ("libwinpthread-1.dll", "libgcc_s_seh-1.dll", "libstdc++-6.dll"):
    _p = os.path.join(SP, "pjsua2", _name)
    if os.path.exists(_p):
        binaries.append((_p, "."))

datas = []

a = Analysis(
    [os.path.join(ROOT, "tools", "build_param", "postbuild_smoke.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "unittest", "pydoc", "doctest", "lib2to3", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="打包冒烟",
    console=True,                    # 需要看输出
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas, [],
    strip=False, upx=False, name="打包冒烟",
)
