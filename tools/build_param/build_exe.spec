# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — GB28181 IPC 模拟工具（GUI）。

用法（两种模式，默认 onedir）：
    set MODE=onedir
    runtime\\venv312\\Scripts\\python.exe -m PyInstaller tools\\build_param\\build_exe.spec --noconfirm

关键取舍见文件内注释，改动前请先读。
"""
import os
import sys

# SPECPATH = spec 文件所在目录（tools/build_param/），
# 工程根目录是它的上两级（build_param -> tools -> 工程根）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPECPATH)))
SP = os.path.join(sys.prefix, "Lib", "site-packages")

MODE = os.environ.get("MODE", "onedir").strip().lower()
APP_NAME = "GB28181_IPC模拟工具"
# 排障用：CONSOLE=1 构建带控制台的版本，可直接看到 Python 异常输出
CONSOLE = os.environ.get("CONSOLE", "0").strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------- 隐藏导入
# pjsua2/__init__.py 用「模块级 __getattr__」惰性加载真实绑定模块，
# 静态分析看不到 pjsua2._pjsua2，不显式声明的话打包后 import 即失败。
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
    # 配置层已迁移到 TOML（tomlkit 做注释保留型就地读写）
    "tomlkit",
    "app.ui.main_window", "app.ui.settings_page",
    "app.ui.status_page", "app.ui.log_page",
]

# ---------------------------------------------------------------- 原生 DLL
# pjsua2 是本工程用 MSYS2/UCRT64(MinGW) 编译的 .pyd，链接了 libwinpthread。
# 该 dll 位于 site-packages/pjsua2/ 下，不在 PATH 里，PyInstaller 自动分析
# 找不到，必须显式塞进包体（放到根目录，确保与 .pyd 同处可被搜索到）。
binaries = []
for _name in ("libwinpthread-1.dll", "libgcc_s_seh-1.dll", "libstdc++-6.dll"):
    _p = os.path.join(SP, "pjsua2", _name)
    if os.path.exists(_p):
        binaries.append((_p, "."))

# ---------------------------------------------------------------- 数据文件
# 说明（重要，勿改回 datas）：
# PyInstaller 的 `datas` 目标**一律**落在 contents_directory（默认 `_internal`）之下，
# 且目标名里含 `.` 也**不会**被提升到 exe 同级（那只适用 shutil.copytree）。
# 已实测确认：datas=[(f,'resource.ps_samples')] -> _internal/resource.ps_samples/。
# 因此「需要落在 exe 同级、可被用户替换」的资源（PS 素材、Readme.md 用户手册）不在此处声明，
# 改由 build.bat 的 Stage 4 统一拷贝到 exe 同级目录。
datas = []

# 应用图标：ipc.ico（多尺寸，用于 exe/窗口）+ ipc.png。
# 图标属程序自身运行时资源，放 _internal/ 内即可，由 app/core/app_icon.py 解析。
_res_dir = os.path.join(ROOT, "resource", "ui")
for _f in ("ipc.ico", "ipc.png"):
    _p = os.path.join(_res_dir, _f)
    if os.path.exists(_p):
        datas.append((_p, "resource/ui"))

# ---------------------------------------------------------------- 瘦身
excludes = [
    "tkinter", "unittest", "pydoc", "doctest", "lib2to3", "pytest",
]

# ---------------------------------------------------------------- 内置 ffmpeg(libav)
# MP4 -> PS 转封装使用仓库内置的 libav（PyAV wheel，位于 libs/av/，含 ffmpeg 的
# libav*.dll）。业务代码在 app/media/mp4_source.py 的 _load_av() 中经 sys.path 注入
# 加载该内置副本，不依赖本机安装 ffmpeg（满足「自下载 ffmpeg 并内置仓库」的需求）。
# 因此把整个 libs/av 树作为数据文件打进包体（落点 _internal/libs/av）。其中
# av.libs/ 与 av/ 必须保持「兄弟目录」布局——av/__init__.py 的 delvewheel 补丁会在
# 导入时把 av.libs 加入 DLL 搜索路径，从而正确加载 libav*.dll。
# 同时把 av 显式加入 excludes：若构建机后续装了 PyAV，避免被 PyInstaller 从
# site-packages 收集（版本可能不一致），强制运行期一律用我们的内置副本。
_av_vendor = os.path.join(ROOT, "libs", "av")
if os.path.isdir(_av_vendor):
    datas.append((_av_vendor, "libs/av"))
else:
    raise SystemExit(
        "[build] 未找到内置 ffmpeg(libav) 目录: {}\n"
        "        请先按《GB28181_支持mp4发送功能设计.md》下载 PyAV wheel 并解压到 libs/av。".format(_av_vendor))
excludes.append("av")

a = Analysis(
    [os.path.join(ROOT, "app", "__main__.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

APP_ICON = os.path.join(ROOT, "resource", "ui", "ipc.ico")

if MODE == "onedir":
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=APP_NAME,
        console=CONSOLE,
        icon=APP_ICON,
        disable_windowed_traceback=False,
    )
    coll = COLLECT(
        exe, a.binaries, a.datas, [],
        strip=False,
        upx=False,
        name=APP_NAME,
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name=APP_NAME,
        console=CONSOLE,
        icon=APP_ICON,
        disable_windowed_traceback=False,
    )
