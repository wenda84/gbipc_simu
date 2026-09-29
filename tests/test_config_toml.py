"""config.py TOML 序列化专项测试。

覆盖：模板生成（注释/分组/全字段）、保存后注释保留、旧 JSON 迁移、

类型强制还原、ps_file 反斜杠归一。
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import (
    AppConfig, load_config, save_config, _build_template, _migrate_json_to_toml,
)


def _t(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({extra})" if extra else ""), flush=True)
    assert cond, name


def test_template_has_comments_and_all_fields():
    txt = _build_template(AppConfig())
    _t("模板含文件头注释", "# GB28181 IPC 模拟工具" in txt)
    _t("模板含分组 [broadcast]", "[broadcast]" in txt)
    _t("模板含字段注释", "# 国标广播开关" in txt)
    missing = [k for k in AppConfig.__annotations__ if k not in txt]
    _t("模板覆盖全部字段", not missing, str(missing))
    # 默认 ps_file 以正斜杠存储（避免 TOML 反斜杠转义问题）
    _t("ps_file 正斜杠", "resource/ps_samples/HK264.ps" in txt)
    _t("bool 写为 false", "broadcast_enabled = false" in txt)
    _t("loop_play 写为 true", "loop_play = true" in txt)


def test_save_preserves_comments():
    path = os.path.join(tempfile.mkdtemp(), "app_config.toml")
    cfg = AppConfig()
    cfg.server_ip = "10.0.0.5"
    cfg.broadcast_volume_percent = 77
    save_config(cfg, path)
    raw1 = open(path, encoding="utf-8").read()
    _t("首次写入含注释", "# 国标广播开关" in raw1)

    # 二次保存（模拟「保存配置」按钮反复点击）必须保留注释
    cfg.broadcast_volume_percent = 88
    cfg.local_ip = "192.168.1.9"
    save_config(cfg, path)
    raw2 = open(path, encoding="utf-8").read()
    _t("二次保存保留文件头注释", "# GB28181 IPC 模拟工具" in raw2)
    _t("二次保存保留字段注释", "# 国标广播开关" in raw2)
    _t("二次保存更新了值", "broadcast_volume_percent = 88" in raw2)
    _t("二次保存未丢失其它值", 'server_ip = "10.0.0.5"' in raw2)

    back = load_config(path)
    _t("重载: 增益", back.broadcast_volume_percent == 88)
    _t("重载: local_ip", back.local_ip == "192.168.1.9")


def test_json_migration():
    d = tempfile.mkdtemp()
    json_path = os.path.join(d, "app_config.json")
    toml_path = os.path.join(d, "app_config.toml")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "device_id": "32128401001311782463",
            "server_ip": "172.220.0.178",
            "broadcast_enabled": False,
        }, f)
    _migrate_json_to_toml(json_path, toml_path)
    _t("迁移后生成 toml", os.path.isfile(toml_path))
    cfg = load_config(toml_path)
    _t("迁移: device_id", cfg.device_id == "32128401001311782463")
    _t("迁移: server_ip", cfg.server_ip == "172.220.0.178")
    _t("迁移: 缺字段取默认", cfg.broadcast_media_port == 15062)
    _t("迁移: 137 派生",
       cfg.broadcast_channel_id == "32128401001371782463",
       cfg.broadcast_channel_id)


def test_type_coercion_and_slash():
    path = os.path.join(tempfile.mkdtemp(), "app_config.toml")
    cfg = AppConfig()
    cfg.ps_file = "tools\\ps_sender\\foo.ps"   # 反斜杠输入
    save_config(cfg, path)
    raw = open(path, encoding="utf-8").read()
    _t("ps_file 写入归一为正斜杠", "tools/ps_sender/foo.ps" in raw)
    back = load_config(path)
    _t("ps_file 重载为正斜杠", back.ps_file == "tools/ps_sender/foo.ps")
    _t("int 还原", isinstance(back.local_sip_port, int))
    _t("float 还原", isinstance(back.unregister_grace, float))
    _t("bool 还原", isinstance(back.broadcast_enabled, bool))


if __name__ == "__main__":
    print("===== config TOML 专项测试 =====", flush=True)
    test_template_has_comments_and_all_fields()
    test_save_preserves_comments()
    test_json_migration()
    test_type_coercion_and_slash()
    print("\nALL_CONFIG_TOML_OK", flush=True)
