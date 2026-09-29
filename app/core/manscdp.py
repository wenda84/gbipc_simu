"""MANSCDP 消息编解码（GB/T 28181-2016 附录 A）。

- 消息体统一 GBK 编码（GBK 兼容 GB2312，6.10 信令字符集）。
- 解析：Query / Notify / Response 三类根节点，取 CmdType / SN / DeviceID 及常见字段。
- 构造：Keepalive Notify、DeviceInfo / DeviceStatus / Catalog / 通用 Result Response。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional

ENCODING = "gbk"
CONTENT_TYPE = "Application/MANSCDP+xml"

_XML_DECL = '<?xml version="1.0" encoding="GB2312"?>\n'


@dataclass
class ManscdpMessage:
    root: str               # Query / Notify / Response
    cmd_type: str           # Keepalive / DeviceInfo / DeviceStatus / Catalog ...
    sn: str = ""
    device_id: str = ""
    extra: dict = field(default_factory=dict)   # 其他一层子元素


def parse(body: bytes) -> Optional[ManscdpMessage]:
    """解析 MANSCDP 消息体；失败返回 None。"""
    try:
        text = body.decode(ENCODING, errors="replace")
    except Exception:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    msg = ManscdpMessage(root=root.tag, cmd_type="", )
    for child in root:
        tag = child.tag
        val = (child.text or "").strip()
        if tag == "CmdType":
            msg.cmd_type = val
        elif tag == "SN":
            msg.sn = val
        elif tag == "DeviceID":
            msg.device_id = val
        else:
            msg.extra[tag] = val
    return msg


def _esc(text: str) -> str:
    """XML 转义 + 非 ASCII 字符转数字字符引用。

    绑定层 str -> UTF-8 上线；全部转成 ASCII 后既是合法 GB2312 子集，
    又能被对端 XML 解析器正确还原中文（&#xXXXX; 与声明编码无关）。
    """
    out = []
    for ch in text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"):
        out.append(ch if ord(ch) < 128 else f"&#x{ord(ch):X};")
    return "".join(out)


def build(root: str, cmd_type: str, sn: str, device_id: str,
          inner: str = "") -> str:
    """按国标常见字段顺序构造 XML（ASCII 安全字符串）。"""
    parts = [f"<CmdType>{_esc(cmd_type)}</CmdType>"]
    if sn:
        parts.append(f"<SN>{_esc(sn)}</SN>")
    parts.append(f"<DeviceID>{_esc(device_id)}</DeviceID>")
    if inner:
        parts.append(inner)
    return _XML_DECL + f"<{root}>" + "".join(parts) + f"</{root}>"


# ---------------- 具体消息构造 ----------------

def build_keepalive(sn: str, device_id: str, status: str = "OK") -> str:
    return build("Notify", "Keepalive", sn, device_id, f"<Status>{status}</Status><Info></Info>")


def build_device_info_response(sn: str, device_id: str, device_name: str,
                               manufacturer: str, model: str, firmware: str,
                               channel: int) -> str:
    inner = (
        f"<Result>OK</Result>"
        f"<DeviceName>{_esc(device_name)}</DeviceName>"
        f"<Manufacturer>{_esc(manufacturer)}</Manufacturer>"
        f"<Model>{_esc(model)}</Model>"
        f"<Firmware>{_esc(firmware)}</Firmware>"
        f"<Channel>{channel}</Channel>"
    )
    return build("Response", "DeviceInfo", sn, device_id, inner)


def build_device_status_response(sn: str, device_id: str, device_time: str,
                                 online: str = "ONLINE", status: str = "OK") -> str:
    inner = (
        f"<Result>OK</Result>"
        f"<Online>{online}</Online>"
        f"<Status>{status}</Status>"
        f"<Encode>ON</Encode>"
        f"<Record>OFF</Record>"
        f"<DeviceTime>{device_time}</DeviceTime>"
        f"<Alarmstatus Num=\"0\"></Alarmstatus>"
    )
    return build("Response", "DeviceStatus", sn, device_id, inner)


def build_catalog_response(sn: str, device_id: str, items: List[dict],
                           sum_num: Optional[int] = None) -> str:
    """items: 目录项 dict 列表（DeviceID/Name/Manufacturer/Model/CivilCode/ParentID/Status 等）。"""
    if sum_num is None:
        sum_num = len(items)
    parts = [f"<SumNum>{sum_num}</SumNum>", f'<DeviceList Num="{len(items)}">']
    for it in items:
        parts.append("<Item>")
        for key in ("DeviceID", "Name", "Manufacturer", "Model", "Owner", "CivilCode",
                    "Address", "Parental", "ParentID", "RegisterWay", "Secrecy", "Status"):
            if key in it and it[key] is not None:
                parts.append(f"<{key}>{_esc(str(it[key]))}</{key}>")
        parts.append("</Item>")
    parts.append("</DeviceList>")
    return build("Response", "Catalog", sn, device_id, "".join(parts))


def build_result_response(cmd_type: str, sn: str, device_id: str,
                          result: str = "OK") -> str:
    """通用结果应答（如 DeviceControl / 未知查询的兜底）。"""
    return build("Response", cmd_type, sn, device_id, f"<Result>{result}</Result>")


def build_broadcast_response(sn: str, device_id: str, result: str = "OK") -> str:
    """语音广播应答（A.2.6 l）。

    请求为 ``Notify/Broadcast``（A.2.5 d，体含 SourceID / TargetID），
    应答体字段为 ``CmdType / SN / DeviceID / Result``；``SN`` 须回显请求值，
    ``DeviceID`` 为语音输出设备的设备编码（本工程回显通知中的 TargetID）。
    """
    return build("Response", "Broadcast", sn, device_id, f"<Result>{result}</Result>")
