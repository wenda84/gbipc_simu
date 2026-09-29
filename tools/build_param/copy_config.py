#!/usr/bin/env python3
"""打包 Stage 4(3)：把默认配置放入 exe 同级的 config/。

用法（由 build.bat 调用）：
    copy_config.py <PROJECT_ROOT> <OUTPUT_DIR>

行为：
    1. 若 <PROJECT_ROOT>/config/app_config.toml 存在 -> 整份拷贝到
       <OUTPUT_DIR>/config/app_config.toml（带项目根已调好的目标环境参数）。
    2. 否则 -> 用 AppConfig() 物化一份干净默认值（首次运行 load_config 也会兜底）。
用 Python 而非 xcopy，避免 xcopy 在中文目标路径下静默失败。
"""
import os
import sys
import shutil

# 让本脚本能 import app.core.config（项目根加入 sys.path）
PROJECT_ROOT = os.path.abspath(sys.argv[1])
OUTPUT_DIR = os.path.abspath(sys.argv[2])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def main() -> int:
    src = os.path.join(PROJECT_ROOT, "config", "app_config.toml")
    dst = os.path.join(OUTPUT_DIR, "config", "app_config.toml")
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    if os.path.exists(src):
        shutil.copy(src, dst)
        print("[4/4] config: copied from project root ->", dst)
    else:
        # 仅兜底分支需要 app.core.config（生成默认配置）
        from app.core.config import save_config, AppConfig  # noqa: E402
        save_config(AppConfig(), dst)
        print("[4/4] config: generated default    ->", dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
