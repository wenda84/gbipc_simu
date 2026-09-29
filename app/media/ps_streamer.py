"""PS 流 RTP 发送器（UDP / TCP-RFC4571）。

原为独立工具 tools/ps_sender（已并入本工程后删除），改造为可被业务层驱动的发送器：
- 参数化：目标地址、PT、SSRC、传输协议由业务层注入（原工程常量 DEST_IP/PORT/SSRC 已移除）；
- 新增 TCP 模式：按 GB/T 28181-2016 附录 L，RTP 包前加 2 字节大端长度前缀（RFC4571），
  本端作为 TCP 客户端主动连接（a=setup:active）；
- 保留原有基于 PTS 的帧级流控与 RTP 分片逻辑。
"""
from __future__ import annotations

import random
import socket
import struct
import threading
import time
from typing import Callable, Optional

PS_HEADER = b'\x00\x00\x01\xba'
VIDEO_PES_HEADER = b'\x00\x00\x01\xe0'
AUDIO_PES_HEADER = b'\x00\x00\x01\xc0'
TS_HZ = 90000
MTU = 1400
MAX_PACING_GAP_SECONDS = 5
MAX_PACING_GAP_TICKS = MAX_PACING_GAP_SECONDS * TS_HZ
MIN_LOOP_TIMESTAMP_STEP = 1


def _parse_pts(data: bytes):
    if len(data) < 19:
        return None
    pts_dts_flags = (data[7] >> 6) & 0x03
    if pts_dts_flags in (0x02, 0x03):
        b1, b2, b3, b4, b5 = data[9:14]
        pts = (((b1 >> 1) & 0x07) << 30) | ((((b2 << 8) | b3) >> 1) << 15) | (((b4 << 8) | b5) >> 1)
        return pts
    return None


def analyze_ps_stream(file_content: bytes):
    """分割 PS 包、解析 PTS、计算帧边界与循环时间戳步长。逻辑与原工程一致。"""
    ps_packs = []
    offset = 0
    total_size = len(file_content)
    while offset < total_size:
        if file_content[offset:offset + 4] != PS_HEADER:
            next_start = file_content.find(PS_HEADER, offset)
            if next_start == -1:
                break
            offset = next_start
        next_offset = file_content.find(PS_HEADER, offset + 4)
        if next_offset == -1:
            pack_data = file_content[offset:]
            offset = total_size
        else:
            pack_data = file_content[offset:next_offset]
            offset = next_offset

        pts = None
        payload_type = None
        video_index = pack_data.find(VIDEO_PES_HEADER)
        audio_index = pack_data.find(AUDIO_PES_HEADER)
        if video_index != -1:
            pts = _parse_pts(pack_data[video_index:])
            if pts is not None:
                payload_type = 'video'
        elif audio_index != -1:
            pts = _parse_pts(pack_data[audio_index:])
            if pts is not None:
                payload_type = 'audio'
        ps_packs.append({'data': pack_data, 'pts': pts, 'type': payload_type, 'is_frame_end': False})

    unique_video_pts = set()
    video_frame_count = 0
    audio_frame_count = 0
    for i, curr in enumerate(ps_packs):
        if curr['pts'] is not None:
            if curr['type'] == 'video':
                if curr['pts'] not in unique_video_pts:
                    video_frame_count += 1
                    unique_video_pts.add(curr['pts'])
            elif curr['type'] == 'audio':
                audio_frame_count += 1
        if i < len(ps_packs) - 1:
            if ps_packs[i + 1]['pts'] is not None:
                curr['is_frame_end'] = True
        else:
            curr['is_frame_end'] = True

    # 构造单调递增的时间轴（pacing_pts），正确处理 33-bit PTS 回绕与非单调跳变。
    # 原实现用 pack['pts'] - first_pts 直接相减：当文件 PTS 跨 2^33 边界或
    # PTS 基值在中段突变（如多段 PS 拼接）时，后半段 pacing_pts 会变成巨大的负值。
    # 这会导致 _send_loop 中 expected=(pacing_pts-start_pacing)/TS_HZ 为负，
    # sleep 恒为负、发送线程从不休眠 -> 半段内容以 CPU 速度倾泻（快播）；
    # 同时 max_pacing_pts 失真（仅取到一个小的正异常值），loop_timestamp_step 偏小、
    # 循环边界时戳错位。下面改为按流做 33-bit 有符号增量累加到单调时间轴。
    last_raw = {}      # 每种流上一包的 33-bit 原始 PTS
    last_pacing = {}   # 每种流当前的累计 pacing_pts
    for pack in ps_packs:
        if pack['pts'] is None or pack['type'] is None:
            pack['pacing_pts'] = None
            continue
        raw = pack['pts']
        if pack['type'] not in last_raw:
            # 该流首包：基准归零
            last_raw[pack['type']] = raw
            last_pacing[pack['type']] = 0
            pack['pacing_pts'] = 0
            continue
        # 33-bit 有符号增量（范围约 [-2^32, 2^32]）
        delta = (raw - last_raw[pack['type']]) & 0x1FFFFFFFF
        if delta > 0x100000000:
            delta -= 0x200000000
        if abs(delta) > MAX_PACING_GAP_TICKS:
            # 真正的 2^33 回绕表现为 < 450000 的小正向增量，会被正常累加；
            # 此处命中的是异常大跳变（PTS 基值突变/拼接），按不连续处理：
            # 时间轴不前进，避免产生巨大负 pacing_pts 污染流控与循环步长。
            delta = 0
        last_pacing[pack['type']] = last_pacing[pack['type']] + delta
        pack['pacing_pts'] = last_pacing[pack['type']]
        last_raw[pack['type']] = raw

    max_pacing_pts = max((p['pacing_pts'] for p in ps_packs if p['pacing_pts'] is not None), default=0)
    loop_timestamp_step = max_pacing_pts + MIN_LOOP_TIMESTAMP_STEP
    return ps_packs, video_frame_count, audio_frame_count, loop_timestamp_step


class PsStreamer:
    """一路媒体流发送会话。

    protocol: "udp" | "tcp"
      - udp: RTP 直接以 UDP 发往 (dest_ip, dest_port)，本机源端口 local_port（0=随机）；
      - tcp: 作为 TCP 客户端连接 (dest_ip, dest_port)，发送 RFC4571 封帧的 RTP。
    ssrc: 国标 y 字段协商值（10 位十进制字符串或 int）。
    """

    def __init__(self, protocol: str, dest_ip: str, dest_port: int, ssrc,
                 local_port: int = 0, payload_type: int = 96,
                 on_log: Optional[Callable[[str], None]] = None,
                 on_stats: Optional[Callable[[dict], None]] = None):
        self.protocol = protocol.lower()
        self.dest_ip = dest_ip
        self.dest_port = dest_port
        self.local_port = local_port
        self.payload_type = payload_type
        try:
            self.ssrc = int(str(ssrc).strip() or "0") & 0xFFFFFFFF
        except ValueError:
            self.ssrc = random.randint(0, 0xFFFFFFFF)
        self._on_log = on_log or (lambda s: None)
        self._on_stats = on_stats or (lambda s: None)

        self._sock: Optional[socket.socket] = None
        self._seq = random.randint(0, 65535)
        self._timestamp_base = random.randint(0, 0xFFFFFFFF)
        self._loop_ts_offset = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._bytes_sent = 0
        self._rtp_sent = 0

    # ---- 对外接口 ----

    def start_file(self, ps_file: str, play_times: int = 1) -> None:
        """读取 PS 文件并开始发送。play_times=0 表示循环播放。"""
        with open(ps_file, 'rb') as f:
            content = f.read()
        packs, vframes, aframes, loop_step = analyze_ps_stream(content)
        if not packs:
            raise ValueError(f"PS 文件无有效包: {ps_file}")
        self._on_log(f"[media] PS 分析完成: {len(content)}B, {len(packs)} 包, "
                     f"视频帧 {vframes}, 音频帧 {aframes}")
        self._open_socket()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._send_loop, args=(packs, loop_step, play_times),
            name="ps-streamer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        sock = self._sock
        if sock is not None:
            # TCP 媒体流必须在 BYE 后让对端读到 EOF（见 mini_platform 校验）。
            # 发送线程可能正阻塞在 sendall()，直接 close() 不会立即产生 FIN；
            # 先 shutdown(SHUT_RDWR) 强制中断待发数据并发出 FIN，再 close()。
            if self.protocol == "tcp":
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._on_log(f"[media] 停止发送, 共 {self._rtp_sent} 个 RTP 包 / {self._bytes_sent} 字节")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 内部 ----

    def _open_socket(self) -> None:
        if self.protocol == "tcp":
            self._on_log(f"[media] TCP 主动连接 {self.dest_ip}:{self.dest_port} ...")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            if self.local_port:
                s.bind(("0.0.0.0", self.local_port))
            s.connect((self.dest_ip, self.dest_port))
            s.settimeout(None)
            self._sock = s
            self._on_log("[media] TCP 已连接")
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if self.local_port:
                s.bind(("0.0.0.0", self.local_port))
            self._sock = s
            self._on_log(f"[media] UDP 发送至 {self.dest_ip}:{self.dest_port}")

    def _send_rtp(self, marker: int, timestamp: int, payload: bytes) -> None:
        byte0 = 0x80
        byte1 = (self.payload_type & 0x7F) | (marker << 7)
        header = struct.pack('!BBHII', byte0, byte1, self._seq,
                             timestamp & 0xFFFFFFFF, self.ssrc)
        self._seq = (self._seq + 1) & 0xFFFF
        packet = header + payload
        sock = self._sock
        if sock is None:
            # stop() 已并发关闭 socket，直接抛 OSError 让 _send_loop 收敛
            raise OSError("socket closed")
        if self.protocol == "tcp":
            # RFC4571：2 字节大端长度前缀
            sock.sendall(struct.pack('!H', len(packet)) + packet)
        else:
            sock.sendto(packet, (self.dest_ip, self.dest_port))
        self._bytes_sent += len(packet)
        self._rtp_sent += 1

    def _send_loop(self, packs, loop_step: int, play_times: int) -> None:
        play_count = 0
        last_ts = None
        try:
            while not self._stop_event.is_set() and (play_times == 0 or play_count < play_times):
                if play_count > 0:
                    self._loop_ts_offset += loop_step
                start_time = time.monotonic()
                start_pacing = None
                last_pacing = None

                for pack in packs:
                    if self._stop_event.is_set():
                        break
                    pacing_pts = pack['pacing_pts']
                    if pacing_pts is not None:
                        ts = self._timestamp_base + self._loop_ts_offset + pacing_pts
                    elif last_ts is not None:
                        ts = last_ts
                    else:
                        ts = self._timestamp_base + self._loop_ts_offset

                    # PTS 流控
                    if pacing_pts is not None:
                        if start_pacing is None:
                            start_pacing = pacing_pts
                        if last_pacing is not None:
                            diff = pacing_pts - last_pacing
                            if diff > MAX_PACING_GAP_TICKS:
                                start_pacing = pacing_pts
                                start_time = time.monotonic()
                            elif diff > 0:
                                expected = (pacing_pts - start_pacing) / TS_HZ
                                actual = time.monotonic() - start_time
                                sleep = expected - actual
                                if sleep > 0.001:
                                    if self._stop_event.wait(sleep):
                                        break
                        if last_pacing is None or pacing_pts > last_pacing:
                            last_pacing = pacing_pts

                    # RTP 分片发送
                    data, offset, total = pack['data'], 0, len(pack['data'])
                    while offset < total:
                        chunk = data[offset:offset + MTU]
                        offset += len(chunk)
                        is_last_chunk = offset >= total
                        marker = 1 if (is_last_chunk and pack['is_frame_end']) else 0
                        self._send_rtp(marker, ts, chunk)
                    if pack['pts'] is not None:
                        last_ts = ts

                play_count += 1
                self._on_stats({'loops': play_count, 'rtp': self._rtp_sent,
                                'bytes': self._bytes_sent})
            if play_times == 0 or play_count < play_times:
                self._on_log("[media] 发送被中止")
            else:
                self._on_log(f"[media] 文件发送完成（{play_count} 轮）")
        except OSError as e:
            if not self._stop_event.is_set():
                self._on_log(f"[media] 发送异常: {e}")
        finally:
            try:
                if self._sock is not None:
                    self._sock.close()
            except OSError:
                pass
