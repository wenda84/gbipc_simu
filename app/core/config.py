"""应用配置模型与 TOML 持久化。

配置项对齐海康"平台接入"设置页中本工具需要的参数（见 docs 方案 §6）。

格式由 JSON 升级为 TOML：原生支持 int/bool/float/string 类型、可用 `#` 注释、
支持 [分组]，更适合长期维护。关键点：应用每次「保存配置」会整体覆写文件，因此
save_config 对**已存在**的文件采用 tomlkit 就地更新值，**完整保留注释与排版**；
对缺失的文件则写入一份带中文注释的分组模板。旧的 app_config.json 会在首次加载时
一次性迁移为 TOML（LEGACY_IMPORTED_FROM 记录来源，供 UI 提示）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import typing
from dataclasses import dataclass

import tomlkit

# 工程根目录（app/core/config.py -> app/core -> app -> 根目录）
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 打包后 sys._MEIPASS 是每次启动重建的临时解压区（onefile）或只读分发目录，
# 配置绝不能落在里面，否则「保存配置」重启即失效。改为 exe 同级目录（便携）。
IS_FROZEN = getattr(sys, "frozen", False)
USER_BASE_DIR = os.path.dirname(sys.executable) if IS_FROZEN else ROOT_DIR
DEFAULT_CONFIG_PATH = os.path.join(USER_BASE_DIR, "config", "app_config.toml")


@dataclass
class AppConfig:
    # ---- 本地 ----
    local_ip: str = ""                 # 本机 IP；空 = 启动时按到平台的路由自动探测
    local_sip_port: int = 5060         # 本地 SIP 端口（UDP）
    # ---- 平台 ----
    server_id: str = "34020100002000000178"   # SIP 服务器 ID（20 位）
    server_domain: str = "172.220.0.178:5080"  # SIP 服务器域
    server_ip: str = "172.220.0.178"          # SIP 服务器地址
    server_port: int = 5080                    # SIP 服务器端口
    # ---- 设备 ----
    device_id: str = "32128401001311782461"    # SIP 用户名（设备 20 位 ID，132=IPC）
    auth_username: str = "32128401001311782461"  # SIP 用户认证 ID
    password: str = "12345678"
    # ---- 注册 / 心跳 ----
    register_expires: int = 3600        # 注册有效期（秒，>=3600）
    register_retry_interval: int = 60   # 注册间隔（失败重试，秒，>=60）
    keepalive_interval: int = 60        # 心跳周期（秒）
    keepalive_timeout_count: int = 3    # 最大心跳超时次数
    unregister_grace: float = 2.0       # 注销 REGISTER 发出后等待送达的时间（秒）
    # ---- 媒体 ----
    local_media_port: int = 15060       # 本机媒体端口（UDP 源端口 / TCP 本地端口）
    ps_file: str = os.path.join("resource", "ps_samples", "HK264.ps")
    loop_play: bool = True              # 循环播放
    # ---- 国标广播（语音广播，本工具为语音流接收者）----
    broadcast_enabled: bool = False     # UI 勾选框；仅停止态可改
    broadcast_media_port: int = 15062   # 本机音频收流端口（UDP 绑定 / TCP 监听）
    broadcast_jitter_ms: int = 60       # 抖动缓冲目标深度（毫秒）
    broadcast_volume_percent: int = 100  # 播放增益（%，100 = 原样）
    broadcast_rtp_timeout: float = 15.0  # 入流中断判定（秒），超时按附录 M 释放链路
    broadcast_invite_timeout: float = 30.0  # 广播 INVITE 接通超时（秒）：发出后未 CONFIRMED 则主动释放
    # ---- 设备信息（DeviceInfo / Catalog 应答内容）----
    device_name: str = "GBIPC-Simulator"
    manufacturer: str = "Simulator"
    model: str = "GBIPC-SIM-1000"
    firmware: str = "V1.0.0"
    channel: int = 1
    civil_code: str = "340201"

    # ---- 派生 URI ----
    @property
    def device_uri(self) -> str:
        return f"sip:{self.device_id}@{self.server_domain}"

    @property
    def server_uri(self) -> str:
        return f"sip:{self.server_id}@{self.server_domain}"

    @property
    def broadcast_channel_id(self) -> str:
        """语音输出通道 ID。

        依据 GB/T 28181-2016 附录 D.1：20 位设备编码的第 11~13 位为类型编码，
        语音输出设备取 137，其 ParentID 为主设备（本 IPC）ID。
        设备 ID 非 20 位数字时原样返回（校验层会拦截，此处只做降级）。
        """
        if len(self.device_id) == 20 and self.device_id.isdigit():
            return self.device_id[:10] + "137" + self.device_id[13:]
        return self.device_id

    def ps_file_abs(self) -> str:
        """PS 素材的绝对路径。

        相对路径按 `USER_BASE_DIR` 解析：源码态=工程根，打包态=exe 同级目录
        （与 config/ 同级的便携布局）。这样 `resource/ps_samples/` 从程序根
        相对定位，两种运行方式命中同一份素材，且素材可直接替换而无需重打包。
        """
        if os.path.isabs(self.ps_file):
            return self.ps_file
        return os.path.join(USER_BASE_DIR, self.ps_file)


# ----------------------------------------------------------------------------
# 字段分组与注释（用于生成带注释的 TOML 模板，以及在保存/读取时定位分组）。
# 列表顺序即文件中出现的顺序；注释会随「保存配置」保留。
# ----------------------------------------------------------------------------
_CONFIG_SECTIONS = [
    ("local", [
        ("local_ip", "本机 IP；空字符串 = 启动时按到平台的路由自动探测"),
        ("local_sip_port", "本地 SIP 端口（UDP）"),
    ]),
    ("server", [
        ("server_id", "SIP 服务器 ID（20 位）"),
        ("server_domain", "SIP 服务器域（host:port）"),
        ("server_ip", "SIP 服务器地址（IP）"),
        ("server_port", "SIP 服务器端口"),
    ]),
    ("device", [
        ("device_id", "设备 ID（20 位，第 11~13 位 132=IPC）"),
        ("auth_username", "SIP 用户认证 ID（通常与 device_id 相同）"),
        ("password", "注册密码"),
    ]),
    ("register", [
        ("register_expires", "注册有效期（秒，建议 >=3600）"),
        ("register_retry_interval", "注册失败重试间隔（秒，>=60）"),
        ("keepalive_interval", "心跳周期（秒）"),
        ("keepalive_timeout_count", "最大心跳超时次数"),
        ("unregister_grace", "注销 REGISTER 发出后等待送达的时间（秒）"),
    ]),
    ("media", [
        ("local_media_port", "本机媒体端口（UDP 源端口 / TCP 本地端口）"),
        ("ps_file", "点播 PS 媒体流文件（相对工程根 / 安装根的路径）"),
        ("loop_play", "是否循环播放"),
    ]),
    ("broadcast", [
        ("broadcast_enabled", "国标广播开关（勾选后本工具作为 UAC 收流播放 PCMA）"),
        ("broadcast_media_port", "本机音频收流端口（UDP 绑定 / TCP 监听）"),
        ("broadcast_jitter_ms", "抖动缓冲目标深度（毫秒）"),
        ("broadcast_volume_percent", "播放增益（%，100 = 原样输出）"),
        ("broadcast_rtp_timeout", "入流中断判定（秒），超时按附录 M 释放链路"),
        ("broadcast_invite_timeout", "广播 INVITE 接通超时（秒）：发出后未 CONFIRMED 则主动释放"),
    ]),
    ("device_info", [
        ("device_name", "设备名称（Catalog 应答）"),
        ("manufacturer", "厂商"),
        ("model", "型号"),
        ("firmware", "固件版本"),
        ("channel", "通道号"),
        ("civil_code", "行政区划代码（6 位）"),
    ]),
]


def _value_for_toml(val):
    """写入 TOML 前归一化：字符串里的反斜杠改为正斜杠（避免 TOML 转义问题）。"""
    if isinstance(val, str):
        return val.replace("\\", "/")
    return val


def _toml_repr(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)          # 2.0 -> "2.0"，保证带小数点
    return tomlkit.string(v).as_string()


def _build_template(cfg: "AppConfig") -> str:
    """生成带中文注释的分组 TOML 文本（文件缺失时写入）。"""
    lines = [
        "# GB28181 IPC 模拟工具 — 配置文件 (TOML)",
        "# 本文件由应用自动读写；修改后重启生效。注释会随「保存配置」保留。",
        "",
    ]
    for sec_name, fields in _CONFIG_SECTIONS:
        lines.append(f"[{sec_name}]")
        for key, comment in fields:
            lines.append(f"# {comment}")
            lines.append(f"{key} = {_toml_repr(_value_for_toml(getattr(cfg, key)))}")
        lines.append("")
    return "\n".join(lines)


# 打包版首次启动时，若从旧位置（源码态的项目根 config/）迁移了配置，
# 记录来源路径，供 UI 提示用户；正常情况为空字符串。
LEGACY_IMPORTED_FROM = ""

# 解析后的字段类型（get_type_hints 会自动展开 `from __future__ import annotations`
# 产生的字符串注解，供 _set_typed 正确还原 int/float/bool/str）。
_FIELD_TYPES = typing.get_type_hints(AppConfig)


def _find_legacy_config(start_dir: str, levels: int = 3):
    """从 start_dir 逐级上溯查找 config/app_config.toml（或旧版 .json）。

    用途：打包后配置位置变为「exe 同级目录」，此前用源码版保存的配置
    留在工程根目录。若不迁移，用户会看到「配置丢了」。
    返回 (path, is_json)：命中不到时返回 ("", False)。
    拷贝到别的机器上运行时不会有任何副作用。
    """
    d = start_dir
    for _ in range(levels):
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        d = parent
        toml_cand = os.path.join(d, "config", "app_config.toml")
        if os.path.isfile(toml_cand):
            return toml_cand, False
        json_cand = os.path.join(d, "config", "app_config.json")
        if os.path.isfile(json_cand):
            return json_cand, True
    return "", False


def _migrate_json_to_toml(json_path: str, toml_path: str) -> None:
    """把旧版 JSON 配置迁移为 TOML（仅搬运已知字段，其余走默认值）。"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cfg = AppConfig()
    for k, v in data.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    save_config(cfg, toml_path)


def _unwrap(v):
    """tomlkit 不同版本下，table[key] 可能返回包装项（有 unwrap）或原生值。"""
    return v.unwrap() if hasattr(v, "unwrap") else v


def _set_typed(cfg: "AppConfig", key: str, raw) -> None:
    """按 dataclass 字段注解把读出的原生值强制转为正确类型。"""
    ann = _FIELD_TYPES.get(key)
    if ann is bool:
        setattr(cfg, key, bool(raw))
    elif ann is int:
        setattr(cfg, key, int(raw))
    elif ann is float:
        setattr(cfg, key, float(raw))
    else:
        setattr(cfg, key, str(raw))


def load_config(path: str = DEFAULT_CONFIG_PATH) -> "AppConfig":
    global LEGACY_IMPORTED_FROM
    LEGACY_IMPORTED_FROM = ""

    # 目标位置还没有配置时，尝试从旧位置导入一次（含 JSON 旧档迁移）。
    # 两种模式都执行：打包态用于「exe 同级目录」便携迁移；
    # 源码态用于读取工程根已有的 app_config.json（保持与原 JSON 版一致的行为）。
    if not os.path.exists(path):
        src, is_json = _find_legacy_config(os.path.dirname(path))
        if src:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if is_json:
                    _migrate_json_to_toml(src, path)
                else:
                    shutil.copyfile(src, path)
                LEGACY_IMPORTED_FROM = src
            except OSError:
                pass      # 只读介质等情况下静默降级为默认配置

    if not os.path.exists(path):
        # 首次运行且无可迁移的旧配置：物化一份默认配置文件，使
        # config/app_config.toml 在启动后即存在，用户可直接编辑，
        # 不必先点「保存配置」。只读介质等异常时静默降级为内存默认值。
        try:
            save_config(AppConfig(), path)
        except OSError:
            pass
        return AppConfig()

    with open(path, "r", encoding="utf-8") as f:
        doc = tomlkit.parse(f.read())
    cfg = AppConfig()
    for sec_name, fields in _CONFIG_SECTIONS:
        tbl = doc.get(sec_name)
        if tbl is None:
            continue
        for key, _ in fields:
            if key in tbl:
                _set_typed(cfg, key, _unwrap(tbl[key]))
    return cfg


def save_config(cfg: "AppConfig", path: str = DEFAULT_CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        # 已存在：就地更新值，完整保留注释、排版与分组结构
        with open(path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
    else:
        # 缺失：写入带注释的分组模板
        doc = tomlkit.parse(_build_template(cfg))

    for sec_name, fields in _CONFIG_SECTIONS:
        tbl = doc.get(sec_name)
        if tbl is None:
            tbl = tomlkit.table()
        for key, _ in fields:
            tbl[key] = _value_for_toml(getattr(cfg, key))
        doc[sec_name] = tbl

    with open(path, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))


def detect_local_ip(remote_ip: str, remote_port: int = 5080) -> str:
    """通过 UDP connect 探测到平台方向的出网卡 IP（不真正发包）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_ip, remote_port))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
