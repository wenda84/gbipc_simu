"""最小但规格正确的 MPEG-2 Program Stream 封装器（PS / vob）。

仅负责把解封装后的 AnnexB 视频(H264/H265)与裸 AAC 音频帧封装为 PS，便于
PsStreamer 直接 RTP 发送。不依赖 ffmpeg 命令行——解封装由仓库内置的 libav(PyAV)
完成，这里的「打包」是纯 Python 实现的 MPEG-2 PS 容器。

与 PsStreamer 的契约（见 ps_streamer.analyze_ps_stream）：
- 每个 pack 以 00 00 01 BA 起；
- 视频 PES 以 00 00 01 E0、音频 PES 以 00 00 01 C0 起；
- PES 可选头里 PTS/DTS 按 33-bit 标准编码（PsStreamer 据此做帧级流控）。

说明：
- 未写 system_header（可选，多数播放器/国标平台可省）；如需极致兼容可后续补。
- 一个 PES 超过单 pack 负载上限时会跨多个 pack 续发，这是 PS 标准行为。
"""
from __future__ import annotations

import struct
import zlib
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

TS_HZ = 90000
PACK_SIZE = 2048          # 每个 pack 的总字节数上限（含 14 字节 pack 头）
PACK_HEADER_LEN = 14      # 无填充时的 pack 头固定长度
PAYLOAD_CAP = PACK_SIZE - PACK_HEADER_LEN   # 单个 pack 内可承载的净数据上限

# SCR(系统时钟参考) 相对 DTS/PTS 的提前量：保证数据包「到达时刻」不晚于解码时刻。
# ffmpeg 的 PS muxer 用 max_delay(默认 0.7s) 做同样的事。
SCR_DELAY_TICKS = 63000
# program_mux_rate 单位是 50 字节/秒；默认按 1MB/s 给一个合理值，实际值由调用方按码率传入。
DEFAULT_MUX_RATE = 20000

VIDEO_STREAM_ID = 0xE0
AUDIO_STREAM_ID = 0xC0
PSM_STREAM_ID = 0xBC      # program_stream_map

# MPEG-2 stream_type（PSM 用，供解封装/解码器识别流类型）
STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_HEVC = 0x24
STREAM_TYPE_AAC = 0x0F


def _build_psm_pack(es_entries: Sequence[Tuple[int, int]], scr: int = 0,
                    mux_rate: int = DEFAULT_MUX_RATE) -> bytes:
    """构造一个携带 Program Stream Map 的 pack。

    国标平台/ffmpeg 类解封装器据此把 0xe0/0xc0 识别为 H264/H265 与 AAC。
    es_entries: [(stream_type, stream_id), ...]
    """
    body = b""
    for st, sid in es_entries:
        body += bytes([st & 0xFF, sid & 0xFF, 0x00, 0x00])   # type, id, es_info_length=0
    psm_data = (bytes([0xC0, 0xFF])           # current_next=1+reserved(2)+version(5); reserved(7)+marker=1
                + struct.pack(">H", 0)         # program_stream_info_length = 0
                + struct.pack(">H", len(body))
                + body)
    crc = zlib.crc32(psm_data) & 0xFFFFFFFF
    pes_length = len(psm_data) + 4                            # + CRC(4)
    pes = b"\x00\x00\x01\xbc" + struct.pack(">H", pes_length) + psm_data + struct.pack(">I", crc)
    return _build_pack(scr, mux_rate) + pes



def _pts_field(prefix: int, pts: int) -> bytes:
    """5 字节 PTS/DTS 字段（33-bit 标准编码）。

    prefix: 0x2=仅 PTS；0x3=PTS(DTS+PTS 组合中的 PTS)；0x1=DTS。
    """
    p = pts & 0x1FFFFFFFF
    return bytes([
        ((prefix & 0x0F) << 4) | (((p >> 30) & 0x07) << 1) | 0x01,
        (p >> 22) & 0xFF,
        (((p >> 15) & 0x7F) << 1) | 0x01,
        (p >> 7) & 0xFF,
        ((p & 0x7F) << 1) | 0x01,
    ])


def _build_pes(stream_id: int, payload: bytes,
               pts: Optional[int], dts: Optional[int]) -> bytes:
    """构造一个 PES 包（不含 pack 头）。
    PES 可选头按 ISO 13818-1 标准布局（此前实现把 PTS_DTS_flags 与
    header_data_length 放错位置，解封装器会把首个 PTS 字节当成可选头长度，
    从而把载荷起点算错 33 字节 -> 视频花屏、音频无 PTS/无法解码）：
      byte0 = '10' + scrambling(2) + priority(1) + align(1) + copyright(1) + original(1) = 0x80
      byte1 = PTS_DTS_flags(2) + 其余标志(6)      -> 0x80(仅 PTS) / 0xC0(PTS+DTS)
      byte2 = PES_header_data_length              -> 5 / 10
      随后为 33-bit PTS / DTS 字段（每字段 5 字节）
    PsStreamer._parse_pts 按同一布局解析。
    """
    if dts is not None:                 # PTS + DTS（PTS_DTS_flags=0b11）
        opt = bytes([0x80, 0xC0, 10]) + _pts_field(0x3, pts) + _pts_field(0x1, dts)
    elif pts is not None:               # 仅 PTS（PTS_DTS_flags=0b10）
        opt = bytes([0x80, 0x80, 5]) + _pts_field(0x2, pts)
    else:                               # 无时间戳：仍保留最短可选头（flags=0, len=0）
        opt = bytes([0x80, 0x00, 0x00])
    length = len(opt) + len(payload)
    if length > 0xFFFF:
        length = 0                       # 视频可超 65535，置 0 表示不限长
    header = b"\x00\x00\x01" + bytes([stream_id]) + struct.pack(">H", length)
    return header + opt + payload


def _build_pack(scr: int, mux_rate: int = 2000000, stuffing: int = 0) -> bytes:
    """构造一个 MPEG-2 PS pack 头（14 字节，无 system_header）。"""
    s = scr & 0x1FFFFFFFF
    v = 0
    v = (v << 2) | 0b01                      # '01'
    v = (v << 3) | ((s >> 30) & 0x07)        # SCR_base[32..30]
    v = (v << 1) | 1                         # marker
    v = (v << 15) | ((s >> 15) & 0x7FFF)     # SCR_base[29..15]
    v = (v << 1) | 1                         # marker
    v = (v << 15) | (s & 0x7FFF)             # SCR_base[14..0]
    v = (v << 1) | 1                         # marker
    v = (v << 9) | 0                         # SCR_extension
    v = (v << 1) | 1                         # marker
    v = (v << 22) | (mux_rate & 0x3FFFFF)     # program_mux_rate
    v = (v << 1) | 1                         # marker
    v = (v << 1) | 1                         # marker
    v = (v << 5) | 0                         # reserved
    v = (v << 3) | (stuffing & 0x07)         # pack_stuffing_length
    return b"\x00\x00\x01\xba" + v.to_bytes(10, "big") + (b"\x00" * stuffing)


def _write_pes(out, pes: bytes, scr: int, mux_rate: int = DEFAULT_MUX_RATE) -> int:
    """把一个 PES 连同其 pack 头写入 out（PES 已保证不超过单 pack 容量）。

    历史实现会把一个超大 PES 切到多个 pack 里续发，并置 PES 长度=0（不限长）。
    实测 ffmpeg/libav 的 mpegps 解封装器**不支持跨 pack 的 PES**：要么判定
    Invalid data，要么在下一个 pack 头处截断载荷（真实 276MB 样本因此丢掉
    约 70MB 视频数据 -> 平台花屏）。因此改为：一帧载荷切成多个 PES，每个
    PES 完整落在一个 2048 字节 pack 内、且带正确的 PES 长度，与 ffmpeg 自身
    PS muxer 的做法一致。
    """
    out.write(_build_pack(scr, mux_rate))
    out.write(pes)
    return scr + 1


def _opt_len(pts: Optional[int], dts: Optional[int]) -> int:
    if dts is not None:
        return 13
    if pts is not None:
        return 8
    return 3


def _build_pes_list(stream_id: int, payload: bytes, pts: Optional[int],
                    dts: Optional[int]) -> List[bytes]:
    """把一个访问单元切成若干「单 pack 可容纳」的 PES 包。

    首包携带 PTS/DTS（用于帧级流控与帧边界），后续包不带时间戳——标准 PS
    允许一个访问单元跨多个 PES，解码器按基本流顺序拼接。
    """
    out: List[bytes] = []
    off, total, first = 0, len(payload), True
    while off < total:
        opt = _opt_len(pts if first else None, dts if first else None)
        cap = PAYLOAD_CAP - 6 - opt          # 6 = 起始码3 + stream_id1 + 长度2
        cap = max(cap, 1)
        chunk = payload[off:off + cap]
        off += len(chunk)
        out.append(_build_pes(stream_id, chunk,
                              pts if first else None, dts if first else None))
        first = False
    return out


def write_ps_from_frames(out, frames: Iterable[Dict], mux_rate: int = DEFAULT_MUX_RATE,
                         preamble: bytes = b"") -> int:
    """把帧迭代器（解码顺序）封装为 PS 并写入二进制流 out。

    frames 每个元素: {"stream_id": int, "payload": bytes, "pts": int|None, "dts": int|None}
    preamble: 可选的前导字节（如 PSM pack），在首帧之前写入。
    返回写入的 pack 数量。

    SCR（pack 头的系统时钟参考）**必须跟随 PTS/DTS 的媒体时间轴**：此前用
    「每个 pack +1」的计数器，13 秒节目里 SCR 只前进 0.12 秒，与 PTS(0→13s)
    严重脱节。按 ISO 13818-1 的 P-STD 缓冲模型，解封装/播放器会用 SCR 推算
    每个包应到达的时刻：SCR 几乎不动而 PTS 一路向前，等于告诉对方「后面十几
    秒的数据在 0.1 秒内全涌进来」，缓冲区占用量算错，实测真实平台表现为首帧
    等很久、播十秒左右卡住并停止。现改为 scr = max(已用 SCR, DTS/PTS - 0.7s)，
    单调不减且始终略早于解码时刻。
    """
    scr = 0
    count = 0
    if preamble:
        out.write(preamble)
        count += 1
    for f in frames:
        payload = f.get("payload")
        if not payload:
            continue
        base = f.get("dts")
        if base is None:
            base = f.get("pts")
        if base is not None:
            # B 帧的 PTS 在解码顺序上会回退，取 dts 优先 + 单调钳位保证 SCR 不减
            scr = max(scr, max(0, base - SCR_DELAY_TICKS))
        for pes in _build_pes_list(f["stream_id"], payload,
                                   f.get("pts"), f.get("dts")):
            scr = _write_pes(out, pes, scr, mux_rate)
            count += 1
    return count


def mux_to_ps(frames: List[Dict], out_path: str, mux_rate: int = DEFAULT_MUX_RATE,
              preamble: bytes = b"") -> int:
    """把帧列表封装为 PS 文件，返回 pack 数量。"""
    with open(out_path, "wb") as fo:
        return write_ps_from_frames(fo, frames, mux_rate, preamble)
