"""GB/T 28181-2016 SDP 解析与构造（附录 F）。

- 解析平台 INVITE / 200 OK 的 SDP：s 操作类型、c 连接地址、m 媒体行（端口/传输/PT）、
  a=setup / a=rtpmap / a=recvonly|sendonly、y=SSRC、f=媒体参数；
- 构造设备侧 200 OK SDP answer（实时点播，m=video PS/90000）；
- 构造语音广播（9.12.1.3.2）接收侧的 Offer（m=audio PCMA/8000）与附录 K 的 Subject 头域。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

#: 附录 C.2.4 a)：G.711 A 律（PCMA）固定载荷类型
PCMA_PAYLOAD_TYPE = 8
#: 语音广播（仅音频）的 f 字段：v/////a/编码格式/码率/采样率（1=G.711, 8=64kbps, 1=8kHz）
AUDIO_F_FIELD = "v/////a/1/8/1"


@dataclass
class SdpMedia:
    media: str = "video"          # video / audio
    port: int = 0
    proto: str = "RTP/AVP"        # RTP/AVP | TCP/RTP/AVP
    payloads: List[int] = field(default_factory=list)
    conn_ip: str = ""             # 媒体级 c=（空则用会话级）
    setup: str = ""               # active / passive / actpass
    direction: str = ""           # recvonly / sendonly / sendrecv
    rtpmap: dict = field(default_factory=dict)   # pt -> "PS/90000"


@dataclass
class SdpOffer:
    session_name: str = ""        # s=：Play / Playback / Download / Talk
    conn_ip: str = ""             # 会话级 c=
    ssrc: str = ""                # y=（10 位十进制字符串）
    f_field: str = ""             # f= 原文
    media: SdpMedia = field(default_factory=SdpMedia)

    @property
    def is_tcp(self) -> bool:
        # 兼容三种 TCP 信令写法：
        #   标准附录 F:            m=video <port> TCP/RTP/AVP 96
        #   部分平台变体:          m=video <port> RTP/AVP/TCP 96
        #   非标准 setup 携带:     m=... RTP/AVP + a=setup:passive/active
        return "TCP" in self.media.proto.upper() or bool(self.media.setup)

    @property
    def dest_ip(self) -> str:
        return self.media.conn_ip or self.conn_ip

    @property
    def dest_port(self) -> int:
        return self.media.port

    def payload_for(self, encoding: str = "PCMA") -> Optional[int]:
        """按编码名（rtpmap 的值，如 "PCMA/8000"）反查载荷类型；未声明返回 None。

        附录 C.2.4 规定 G.711A 的 PT 为 8，但平台方言可能改用它值，
        因此以 rtpmap 声明为准、PT=8 作为兜底判定。
        """
        want = encoding.upper()
        for pt, name in self.media.rtpmap.items():
            if name.upper().startswith(want):
                return pt
        if want == "PCMA" and PCMA_PAYLOAD_TYPE in self.media.payloads:
            return PCMA_PAYLOAD_TYPE
        return None


def parse_offer(text: str, prefer: str = "video") -> SdpOffer:
    """解析 SDP。

    ``prefer`` 指定优先择取的媒体类型：实时点播取 ``video``（m=video PS/90000），
    语音广播取 ``audio``（m=audio PCMA/8000，见 9.12.1.3.2）。
    未命中 prefer 时退回首个媒体行，保证单 m 行场景与既有行为一致。
    """
    offer = SdpOffer()
    cur: Optional[SdpMedia] = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or len(line) < 2 or line[1] != "=":
            continue
        key, val = line[0], line[2:]
        if key == "s":
            offer.session_name = val.strip()
        elif key == "c":
            parts = val.split()
            if len(parts) >= 3:
                if cur is not None:
                    cur.conn_ip = parts[2]
                else:
                    offer.conn_ip = parts[2]
        elif key == "m":
            parts = val.split()
            if len(parts) >= 3:
                cur = SdpMedia(
                    media=parts[0],
                    port=int(parts[1]),
                    proto=parts[2],
                    payloads=[int(p) for p in parts[3:] if p.isdigit()],
                )
                if cur.media == prefer or offer.media.media != prefer:
                    offer.media = cur
        elif key == "a" and cur is not None:
            if val.startswith("setup:"):
                cur.setup = val.split(":", 1)[1].strip()
            elif val.startswith("rtpmap:"):
                rest = val.split(":", 1)[1]
                pt_s, _, enc = rest.partition(" ")
                if pt_s.isdigit():
                    cur.rtpmap[int(pt_s)] = enc.strip()
            elif val in ("recvonly", "sendonly", "sendrecv", "inactive"):
                cur.direction = val
        elif key == "y":
            offer.ssrc = val.strip()
        elif key == "f":
            offer.f_field = val.strip()
    return offer


def build_answer(device_id: str, local_ip: str, local_media_port: int,
                 offer: SdpOffer, payload_type: int = 96) -> str:
    """按附录 F 构造实时点播应答 SDP。

    - o 的 username = 设备编码；s=Play；t=0 0
    - TCP 时：Offer 为 passive → Answer 为 active（设备主动建连）
    - y 原样带回 Offer 中的 SSRC
    """
    proto = offer.media.proto or "RTP/AVP"
    if offer.is_tcp:
        # Answer 的 m 行统一规范为 TCP/RTP/AVP（附录 F 标准写法），
        # 兼容 Offer 中 RTP/AVP/TCP、仅 a=setup 等变体，保证与实际传输一致
        proto = "TCP/RTP/AVP"
    lines = [
        "v=0",
        f"o={device_id} 0 0 IN IP4 {local_ip}",
        "s=Play",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=video {local_media_port} {proto} {payload_type}",
    ]
    if proto.upper().startswith("TCP"):
        setup = "active" if offer.media.setup in ("passive", "actpass", "") else "passive"
        lines.append(f"a=setup:{setup}")
        lines.append("a=connection:new")
    lines.append("a=sendonly")
    lines.append(f"a=rtpmap:{payload_type} PS/90000")
    if offer.ssrc:
        lines.append(f"y={offer.ssrc}")
    if offer.f_field:
        lines.append(f"f={offer.f_field}")
    return "\r\n".join(lines) + "\r\n"


def build_subject(source_id: str, device_id: str, tx_seq: str = "0",
                  rx_seq: str = "1") -> str:
    """构造附录 K 的 Subject 头域。

    格式：``媒体流发送者ID:发送方媒体流序列号,媒体流接收者ID:接收方媒体流序列号``。
    序列号为「不超过 20 位的字符串」，实时流（语音广播）的发送方序列号首位固定取 0。
    """
    return f"{source_id}:{tx_seq},{device_id}:{rx_seq}"


def build_audio_offer(device_id: str, local_ip: str, local_port: int,
                      ssrc: str, payload_type: int = PCMA_PAYLOAD_TYPE,
                      tcp: bool = False) -> str:
    """构造语音广播（接收侧）Offer，依据 9.12.1.3.2 + 附录 F。

    - ``s=Play``：广播走实时点播信令（附录 F 明确 ``Talk`` 才是语音对讲）；
    - ``m=audio``：音频媒体行，PT=8 / PCMA/8000（C.2.4 a)）；
    - ``a=recvonly``：本端只收（对端为 sendonly，方向互补）；
    - ``a=setup:passive``（TCP 时）：附录 L 规定媒体流接收方宜作为 TCP 服务端；
    - ``y=`` 为本端建议 SSRC，实际以对端 200 OK 的 y 为准（附录 F 注 4）。
    """
    proto = "TCP/RTP/AVP" if tcp else "RTP/AVP"
    lines = [
        "v=0",
        f"o={device_id} 0 0 IN IP4 {local_ip}",
        "s=Play",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {local_port} {proto} {payload_type}",
    ]
    if tcp:
        lines.append("a=setup:passive")
        lines.append("a=connection:new")
    lines.append("a=recvonly")
    lines.append(f"a=rtpmap:{payload_type} PCMA/8000")
    if ssrc:
        lines.append(f"y={ssrc}")
    lines.append(f"f={AUDIO_F_FIELD}")
    return "\r\n".join(lines) + "\r\n"
