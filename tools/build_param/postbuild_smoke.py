"""打包后原生依赖冒烟（仅供构建验证，不是产品入口）。

为什么需要它：
  pjsua2/__init__.py 用「模块级 __getattr__」惰性加载，
  `import pjsua2` 本身**不会**加载 `_pjsua2.pyd`；只有访问 `pj.Endpoint`
  这类属性时才会真正 LoadLibrary。因此「GUI 窗口能弹出来」并不能证明
  原生模块在打包环境里可用——必须显式触发一次并打印版本。

  `import PySide6.QtWidgets` 同理，这里一并验证 Qt 能否创建 QApplication。

用法：
  源码态： runtime\\venv312\\Scripts\\python.exe tools\\postbuild_smoke.py
  打包态： dist\\打包冒烟\\打包冒烟.exe
"""
from __future__ import annotations

import os
import sys

# 以脚本方式直接运行时，确保能 import 到 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ok = True

    # ---- 1) 原生 SIP 栈：真正触发 .pyd 加载 ----
    try:
        import pjsua2 as pj
        ep = pj.Endpoint()                       # 触发 _pjsua2.pyd 加载
        ep.libCreate()
        ver = ep.libVersion()
        print(f"[OK]   pjsua2 原生模块已加载: {ver.full}")
        ep.libDestroy()
    except Exception as e:
        ok = False
        print(f"[FAIL] pjsua2 加载失败: {type(e).__name__}: {e}")

    # ---- 2) Qt：确认平台插件（qwindows.dll）可用 ----
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication(sys.argv)
        print(f"[OK]   Qt 已初始化, platform={app.platformName()}")
    except Exception as e:
        ok = False
        print(f"[FAIL] Qt 初始化失败: {type(e).__name__}: {e}")

    # ---- 3) 业务层可导入 + 素材就位 ----
    try:
        from app.core.config import AppConfig, DEFAULT_CONFIG_PATH, load_config, save_config
        from app.core.device import GbDevice          # 会拉起 pjsip_stack
        cfg = AppConfig()
        print(f"[OK]   业务层导入正常; 配置文件路径={DEFAULT_CONFIG_PATH}")
        print(f"[OK]   PS 默认素材存在={os.path.exists(cfg.ps_file_abs())} "
              f"({cfg.ps_file})")
    except Exception as e:
        ok = False
        print(f"[FAIL] 业务层导入失败: {type(e).__name__}: {e}")

    # ---- 4) 国标广播的音频播放后端：DLL 定位必须成立 ----
    # sounddevice 通过 `next(iter(_sounddevice_data.__path__))` + 'portaudio-binaries'
    # 拼出 PortAudio DLL 的绝对路径并经 cffi dlopen。在 PyInstaller 冻结环境下
    # `_sounddevice_data.__path__` 是否仍指向随包目录，静态分析看不出来——
    # 必须实跑一次 dlopen。这里的验证等价于「广播能否真的出声」。
    try:
        import _sounddevice_data
        print(f"[.]    _sounddevice_data.__path__={list(_sounddevice_data.__path__)}")
        import sounddevice as sd
        # 触发 _lib 的 dlopen 与 PortAudio 初始化（模块导入时即发生）
        devs = sd.query_devices()
        outs = [d for d in devs if d.get("max_output_channels", 0) > 0]
        print(f"[OK]   sounddevice 原生后端可用: {len(devs)} 个设备, "
              f"其中 {len(outs)} 个输出设备")
        try:
            st = sd.RawOutputStream(samplerate=8000, channels=1, dtype="int16")
            st.start()
            st.write(b"\x00" * (160 * 2))
            st.abort()
            st.close()
            print("[OK]   8 kHz 单声道 PCM 输出流可打开（广播出声链路就绪）")
        except Exception as e:
            print(f"[WARN] 无法打开 8 kHz 输出流（无音频设备的机器属正常）: {e}")
    except Exception as e:
        ok = False
        print(f"[FAIL] sounddevice 后端不可用: {type(e).__name__}: {e}")

    # ---- 5) 配置真的能落盘并读回（打包后「保存配置」的核心）----
    # onefile 下 sys._MEIPASS 是临时目录、重启即失效，因此 frozen 时
    # 配置必须落在 exe 同级目录。这里真实写一次再读回比对。
    try:
        cfg = AppConfig()
        cfg.device_name = "PACKAGED-SMOKE"
        cfg.password = "smoke_pwd_123"
        save_config(cfg, DEFAULT_CONFIG_PATH)
        back = load_config(DEFAULT_CONFIG_PATH)
        same = (back.device_name == "PACKAGED-SMOKE"
                and back.password == "smoke_pwd_123")
        print(f"[{'OK' if same else 'FAIL'}]   配置写入并读回一致={same} "
              f"(文件={os.path.exists(DEFAULT_CONFIG_PATH)})")
        ok = ok and same
        # 清理测试痕迹，避免污染用户真实配置
        try:
            os.remove(DEFAULT_CONFIG_PATH)
        except OSError:
            pass
    except Exception as e:
        ok = False
        print(f"[FAIL] 配置持久化失败: {type(e).__name__}: {e}")

    print("SMOKE_OK" if ok else "SMOKE_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
