"""PCMA（G.711A）RTP 接收器 —— 国标语音广播的收流侧。

对应 GB/T 28181-2016 附录 C.2.4（音频流 RTP 封装：PT=8、PCMA、8 kHz、单声道）。

职责边界：
- 收包与 RTP 解析、SSRC 校验、乱序/重复/丢包处理、抖动缓冲、A 律解码；
- **不做**播放与电平（分别由 audio_sink / broadcast_session 负责）。

消费模型为 **pull**：由播放线程调用 :meth:`pull` 取走恰好 N 个采样的 PCM。
这样播放时钟天然对齐，无需额外定时器；无音频设备时（NullSink）同样成立。

TCP 模式按附录 L：媒体流接收方宜作为 TCP 媒体流传输服务端，故本端 ``listen``
并接受一个连接，按 RFC4571 的 2 字节大端长度前缀解帧。
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, Optional

from .alaw import alaw_decode, pcm16_apply_gain

PCMA_PAYLOAD_TYPE = 8
PCMA_SAMPLE_RATE = 8000
PCMA_FRAME_SAMPLES = 160          # 20 ms @ 8 kHz
PCMA_FRAME_BYTES = PCMA_FRAME_SAMPLES * 2

_MAX_SLOTS = 96                   # 抖动缓冲上限（约 1.9 s @20 ms）
_MAX_OOO_HALF = 0x8000            # 16 位 seq 环形比较的半程
_PREBUF_GRACE_S = 0.6             # 预缓冲最长等待时间


def parse_rtp(pkt: bytes):
    """解析 RTP 包，返回 ``(marker, pt, seq, timestamp, ssrc, payload)``；非法返回 None。"""
    if len(pkt) < 12:
        return None
    b0 = pkt[0]
    if (b0 >> 6) != 2:            # 版本必须为 2
        return None
    cc = b0 & 0x0F
    has_ext = bool(b0 & 0x10)
    has_pad = bool(b0 & 0x20)
    b1 = pkt[1]
    marker = (b1 >> 7) & 0x01
    pt = b1 & 0x7F
    seq = (pkt[2] << 8) | pkt[3]
    timestamp = int.from_bytes(pkt[4:8], "big")
    ssrc = int.from_bytes(pkt[8:12], "big")
    off = 12 + cc * 4
    if has_ext:
        if len(pkt) < off + 4:
            return None
        ext_words = int.from_bytes(pkt[off + 2:off + 4], "big")
        off += 4 + ext_words * 4
    if off > len(pkt):
        return None
    end = len(pkt)
    if has_pad:
        pad = pkt[-1]
        if 0 < pad <= end - off:
            end -= pad
    payload = pkt[off:end] if end > off else b""
    return marker, pt, seq, timestamp, ssrc, payload


class PcmaReceiver:
    """一路 PCMA 收流会话。

    参数
    ----
    protocol      : "udp" | "tcp"
    local_ip      : 绑定地址（空串 = 全部网卡）
    local_port    : 绑定端口（即 INVITE Offer 中 m=audio 声明的端口）
    expect_ssrc   : 协商得到的 SSRC（Answer 的 y）；None 或空 = 锁定首包
    sample_rate   : 采样率（本项目固定 8000）
    prebuffer_ms  : 预缓冲目标深度（= 抖动缓冲深度）；决定首帧出声前的蓄流时长
    on_log        : 日志回调
    """

    def __init__(self, protocol: str, local_ip: str, local_port: int,
                 expect_ssrc: Optional[str] = None, sample_rate: int = PCMA_SAMPLE_RATE,
                 prebuffer_ms: int = 60,
                 on_log: Optional[Callable[[str], None]] = None) -> None:
        self.protocol = (protocol or "udp").lower()
        self.local_ip = local_ip or ""
        self.local_port = int(local_port)
        self.sample_rate = int(sample_rate)
        self.prebuffer_ms = max(0, int(prebuffer_ms))
        self._on_log = on_log or (lambda s: None)

        self._expect_ssrc: Optional[int] = None
        if expect_ssrc:
            try:
                self._expect_ssrc = int(str(expect_ssrc).strip()) & 0xFFFFFFFF
            except ValueError:
                self._expect_ssrc = None

        self._sock: Optional[socket.socket] = None
        self._conn: Optional[socket.socket] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self._slots: dict[int, bytes] = {}
        self._next_seq: Optional[int] = None
        self._pcm_out = bytearray()
        self._last_frame = b""
        self._prebuffering = True
        self._prebuf_bytes = max(
            int(self.sample_rate * self.prebuffer_ms / 1000.0) * 2,
            PCMA_FRAME_BYTES,
        )
        self._start_time = time.monotonic()

        self._locked_ssrc: Optional[int] = None
        self._peer_addr = ""

        self._rtp_rx = 0
        self._bytes_rx = 0
        self._pt_mismatch = 0
        self._ssrc_mismatch = 0
        self._dup = 0
        self._ooo = 0
        self._lost = 0
        self._underrun = 0
        self._last_rx_time = 0.0

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._open_socket()
        self._stop_event.clear()
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._recv_loop, name="pcma-recv", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        for sk in (self._conn, self._sock):
            if sk is not None:
                # 先 shutdown 再 close：中断阻塞中的 recv，避免线程滞留
                try:
                    sk.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sk.close()
                except OSError:
                    pass
        self._conn = None
        self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------------- 消费（播放线程调用） ----------------

    def pull(self, samples: int) -> bytes:
        """取走 ``samples`` 个采样的 PCM16LE；不足则以静音补齐。"""
        need = samples * 2
        with self._lock:
            self._fill_out(need)
            if len(self._pcm_out) >= need:
                data = bytes(self._pcm_out[:need])
                del self._pcm_out[:need]
            else:
                data = bytes(self._pcm_out)
                self._pcm_out.clear()
                if self._rtp_rx and not self._prebuffering:
                    self._underrun += 1
                data += b"\x00" * (need - len(data))
        return data

    def silence_for(self, ms: float) -> bytes:
        """生成指定时长静音 PCM16LE（停止/复位时用）。"""
        n = int(self.sample_rate * ms / 1000.0) * 2
        return b"\x00" * n

    # ---------------- 统计 ----------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "rtp": self._rtp_rx,
                "bytes": self._bytes_rx,
                "lost": self._lost,
                "dup": self._dup,
                "ooo": self._ooo,
                "underrun": self._underrun,
                "pt_mismatch": self._pt_mismatch,
                "ssrc_mismatch": self._ssrc_mismatch,
                "peer": self._peer_addr,
                "rx_age": (time.monotonic() - self._last_rx_time) if self._last_rx_time else -1.0,
                "buffered": len(self._pcm_out),
            }

    # ---------------- socket ----------------

    def _open_socket(self) -> None:
        if self.protocol == "tcp":
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.local_ip or "0.0.0.0", self.local_port))
            s.listen(1)
            s.settimeout(0.5)
            self._sock = s
            self._on_log(f"[media] TCP 监听 {self.local_ip or '0.0.0.0'}:{self.local_port}"
                         "（附录 L：接收方作服务端）")
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
            except OSError:
                pass
            s.bind((self.local_ip or "0.0.0.0", self.local_port))
            s.settimeout(0.5)
            self._sock = s
            self._on_log(f"[media] UDP 收流绑定 {self.local_ip or '0.0.0.0'}:{self.local_port}")

    def _recv_loop(self) -> None:
        try:
            if self.protocol == "tcp":
                self._tcp_loop()
            else:
                self._udp_loop()
        except OSError as e:
            if not self._stop_event.is_set():
                self._on_log(f"[media] 收流异常: {e}")
        except Exception as e:  # pragma: no cover - 防御
            self._on_log(f"[media] 收流内部错误: {e}")

    def _udp_loop(self) -> None:
        sock = self._sock
        while not self._stop_event.is_set() and sock is not None:
            try:
                pkt, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_packet(pkt, addr)

    def _tcp_loop(self) -> None:
        sock = self._sock
        while not self._stop_event.is_set():
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._conn = conn
            conn.settimeout(0.5)
            buf = bytearray()
            while not self._stop_event.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                # RFC4571：2 字节大端长度前缀 + RTP 包
                while len(buf) >= 2:
                    framelen = struct.unpack("!H", bytes(buf[:2]))[0]
                    if len(buf) < 2 + framelen:
                        break
                    self._handle_packet(bytes(buf[2:2 + framelen]), addr)
                    del buf[:2 + framelen]
            try:
                conn.close()
            except OSError:
                pass
            self._conn = None
            if self._stop_event.is_set():
                break

    # ---------------- RTP 处理 ----------------

    def _handle_packet(self, pkt: bytes, addr) -> None:
        parsed = parse_rtp(pkt)
        if parsed is None:
            return
        _marker, pt, seq, _ts, ssrc, payload = parsed

        if pt != PCMA_PAYLOAD_TYPE:
            self._pt_mismatch += 1
            return

        if self._expect_ssrc is not None:
            if ssrc != self._expect_ssrc:
                self._ssrc_mismatch += 1
                return
        elif self._locked_ssrc is None:
            self._locked_ssrc = ssrc
            self._on_log(f"[media] 锁定媒体源 SSRC={ssrc}（{addr[0]}:{addr[1]}）")
        elif ssrc != self._locked_ssrc:
            self._ssrc_mismatch += 1
            return

        if not payload:
            return

        pcm = alaw_decode(payload)
        now = time.monotonic()
        with self._lock:
            self._rtp_rx += 1
            self._bytes_rx += len(pkt)
            self._last_rx_time = now
            if not self._peer_addr:
                self._peer_addr = f"{addr[0]}:{addr[1]}"
            self._store(seq, pcm)

    def _store(self, seq: int, pcm: bytes) -> None:
        """按 seq 存入抖动缓冲（调用方须持锁）。"""
        if self._next_seq is None:
            self._next_seq = seq
        diff = (seq - self._next_seq) & 0xFFFF
        if diff >= _MAX_OOO_HALF:            # 已消费过的迟到包 / 重复包
            self._dup += 1
            return
        if seq in self._slots:
            self._dup += 1
            return
        if self._slots:
            # 判断是否为乱序（存在更大的 seq 已被收下）
            newer = any(((k - seq) & 0xFFFF) < _MAX_OOO_HALF for k in self._slots)
            if newer:
                self._ooo += 1
        self._slots[seq] = pcm
        # 溢出保护：丢弃最旧的包以追平延迟
        while len(self._slots) > _MAX_SLOTS:
            oldest = min(self._slots)
            self._slots.pop(oldest, None)
            self._next_seq = (oldest + 1) & 0xFFFF
            self._lost += 1

    def _fill_out(self, need: int) -> None:
        """把抖动缓冲中已就绪的帧搬进输出队列（调用方须持锁）。"""
        if self._prebuffering:
            have = sum(len(v) for v in self._slots.values())
            # 蓄够抖动目标深度再放行（吸收首包抖动）；超过宽限时间则先出声避免静默过久
            if have >= self._prebuf_bytes:
                self._prebuffering = False
                self._on_log(f"[media] 预缓冲完成（{len(self._slots)} 包 / "
                             f"{self.prebuffer_ms}ms 目标）")
            elif (time.monotonic() - self._start_time) > _PREBUF_GRACE_S:
                self._prebuffering = False
                self._on_log(f"[media] 预缓冲超时放行（{len(self._slots)} 包）")
            else:
                return
        if self._next_seq is None:
            return
        while len(self._pcm_out) < need:
            if self._next_seq in self._slots:
                self._last_frame = self._slots.pop(self._next_seq)
                self._pcm_out += self._last_frame
                self._next_seq = (self._next_seq + 1) & 0xFFFF
                continue
            # 空洞：仅当缓冲里还有更新的包时才判定为丢包并做遮蔽补偿，
            # 否则说明只是暂时没到，留给下一次 pull（避免把等待误判成丢包）
            newer = any(((k - self._next_seq) & 0xFFFF) < _MAX_OOO_HALF
                        for k in self._slots)
            if not newer:
                break
            self._pcm_out += self._conceal()
            self._next_seq = (self._next_seq + 1) & 0xFFFF
            self._lost += 1

    def _conceal(self) -> bytes:
        """丢包遮蔽：用上一帧半幅重复，无历史则静音。"""
        if self._last_frame:
            return pcm16_apply_gain(self._last_frame, 50)
        return b"\x00" * PCMA_FRAME_BYTES
