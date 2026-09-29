"""国标语音广播会话编排。

把「收流 → 抖动缓冲 → A 律解码 → 增益 → 播放 → 电平计算」收敛为一个可
``start()/stop()`` 的对象，使业务层（core/device.py）保持轻薄：
业务层只负责 SIP 信令与状态机，媒体细节全部封在本模块内。

对应 GB/T 28181-2016：
- 9.12.1 语音广播（本工具为语音流接收者）；
- C.2.4 音频流 RTP 封装（PT=8 / PCMA / 8 kHz / 单声道）；
- 附录 M b) 媒体流保活：入流中断超时后主动释放链路（经 ``on_timeout`` 上报）。
"""
from __future__ import annotations

import threading
import time
import wave
from typing import Callable, Optional

from .alaw import (
    LevelEnvelope, level_from_dbfs, pcm16_apply_gain, pcm16_rms, rms_to_dbfs,
)
from .audio_sink import AudioSink, create_audio_sink
from .pcma_receiver import PCMA_SAMPLE_RATE, PcmaReceiver

#: 电平上报节流间隔（秒）。50 包/秒的原始速率直接上报会挤占 SIP 事件队列。
_LEVEL_EMIT_INTERVAL = 0.04      # 25 Hz
#: 入流超时巡检间隔（秒）
_TIMEOUT_POLL = 1.0
#: WAV 落盘上限（秒），避免长时间会话占用内存
_DUMP_MAX_SECONDS = 120


class BroadcastSession:
    """一路广播收流会话（同时刻仅一路）。

    参数
    ----
    source_id         : 语音流发送者 ID（来自广播通知的 SourceID），仅用于日志/统计
    protocol          : "udp" | "tcp"
    local_ip          : 收流绑定地址
    media_port        : 收流端口（与 INVITE Offer 中 m=audio 声明的端口一致）
    expect_ssrc       : 协商得到的 SSRC（Answer 的 y）
    jitter_ms         : 抖动缓冲目标深度
    volume_percent    : 播放增益（100 = 原样）
    rtp_timeout       : 入流中断判定（秒），超时后回调 on_timeout
    on_level          : 电平回调 ``(level, peak)``，均归一化到 [0,1]；已限流 25 Hz
    on_timeout        : 入流中断回调（在巡检线程触发，调用方须仅做投递）
    on_log            : 日志回调
    dump_wav          : 可选，把解码后的 PCM 落盘为 WAV（自动化测试/离线取证）
    force_null_sink   : 强制无播放后端（测试用）
    """

    def __init__(self, source_id: str, protocol: str, local_ip: str, media_port: int,
                 expect_ssrc: Optional[str] = None, jitter_ms: int = 60,
                 volume_percent: int = 100, rtp_timeout: float = 15.0,
                 on_level: Optional[Callable[[float, float], None]] = None,
                 on_timeout: Optional[Callable[[], None]] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 dump_wav: Optional[str] = None,
                 force_null_sink: bool = False) -> None:
        self.source_id = source_id
        self.protocol = (protocol or "udp").lower()
        self.local_ip = local_ip or ""
        self.media_port = int(media_port)
        self.expect_ssrc = expect_ssrc
        self.jitter_ms = int(jitter_ms)
        self.volume_percent = int(volume_percent)
        self.rtp_timeout = float(rtp_timeout)
        self._on_level = on_level
        self._on_timeout = on_timeout
        self._on_log = on_log or (lambda s: None)
        self._dump_wav = dump_wav
        self._force_null_sink = force_null_sink

        self._rx: Optional[PcmaReceiver] = None
        self._sink: Optional[AudioSink] = None
        self._env = LevelEnvelope()
        self._lock = threading.Lock()
        self._last_emit = 0.0
        self._frames = 0
        self._start_time = 0.0
        self._timeout_fired = False
        self._watch: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._dump_pcm: list[bytes] = []

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._rx is not None:
            return
        self._stop_event.clear()
        self._timeout_fired = False
        self._start_time = time.monotonic()
        rx = PcmaReceiver(protocol=self.protocol, local_ip=self.local_ip,
                          local_port=self.media_port, expect_ssrc=self.expect_ssrc,
                          sample_rate=PCMA_SAMPLE_RATE, prebuffer_ms=self.jitter_ms,
                          on_log=self._on_log)
        rx.start()
        self._rx = rx
        self._on_log(f"[broadcast] 开始接收语音流 来源={self.source_id or '-'} "
                     f"{self.protocol.upper()} :{self.media_port} "
                     f"抖动缓冲={self.jitter_ms}ms 增益={self.volume_percent}%")

        sink = create_audio_sink(on_log=self._on_log, force_null=self._force_null_sink)
        sink.start(self._pull)
        self._sink = sink

        self._watch = threading.Thread(target=self._watch_timeout, name="bc-timeout",
                                       daemon=True)
        self._watch.start()

    def stop(self) -> None:
        self._stop_event.set()
        rx = self._rx
        sink = self._sink
        self._rx = None
        self._sink = None
        if sink is not None:
            sink.stop()
        if rx is not None:
            rx.stop()
            st = rx.stats()
            self._on_log(f"[broadcast] 收流结束：RTP {st['rtp']} 包 / "
                         f"{st['bytes'] / 1024:.0f} KB，丢包 {st['lost']}，"
                         f"乱序 {st['ooo']}，重复 {st['dup']}，欠载 {st['underrun']}")
        if self._watch is not None:
            self._watch.join(timeout=2)
            self._watch = None
        self._env.reset()
        self._emit_level(force=True)
        self._flush_dump()

    def is_running(self) -> bool:
        return self._rx is not None and self._rx.is_running()

    def stats(self) -> dict:
        rx = self._rx
        base = rx.stats() if rx is not None else {}
        base.update({"level": self._env.level, "peak": self._env.peak,
                     "frames": self._frames})
        return base

    def stream_summary(self) -> str:
        """给 UI 状态页用的一行摘要。"""
        rx = self._rx
        if rx is None:
            return "—"
        st = rx.stats()
        return (f"收 {st['rtp']} 包 / {st['bytes'] / 1024:.0f} KB · "
                f"丢 {st['lost']} · 乱序 {st['ooo']}")

    # ---------------- 播放拉取（在播放后端线程执行） ----------------

    def _pull(self, samples: int) -> bytes:
        rx = self._rx
        if rx is None:
            return b"\x00" * (samples * 2)
        pcm = rx.pull(samples)
        if self.volume_percent != 100:
            pcm = pcm16_apply_gain(pcm, self.volume_percent)
        level = level_from_dbfs(rms_to_dbfs(pcm16_rms(pcm)))
        self._env.update(level)
        self._frames += 1
        self._emit_level()
        if self._dump_wav:
            self._collect_dump(pcm)
        return pcm

    def _emit_level(self, force: bool = False) -> None:
        if self._on_level is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) < _LEVEL_EMIT_INTERVAL:
            return
        self._last_emit = now
        try:
            self._on_level(self._env.level, self._env.peak)
        except Exception:
            pass

    # ---------------- 入流超时巡检（附录 M b） ----------------

    def _watch_timeout(self) -> None:
        while not self._stop_event.wait(_TIMEOUT_POLL):
            rx = self._rx
            if rx is None:
                return
            st = rx.stats()
            age = st["rx_age"]
            elapsed = time.monotonic() - self._start_time
            stuck = (age >= 0 and age > self.rtp_timeout) or \
                    (age < 0 and elapsed > self.rtp_timeout)
            if stuck and not self._timeout_fired:
                self._timeout_fired = True
                self._on_log(f"[broadcast] 入流中断（无包 {age:.1f}s / 已运行 {elapsed:.1f}s，"
                             f"阈值 {self.rtp_timeout:.0f}s），按附录 M 释放链路")
                if self._on_timeout is not None:
                    try:
                        self._on_timeout()
                    except Exception:
                        pass
                return

    # ---------------- WAV 落盘（测试/取证） ----------------

    def _collect_dump(self, pcm: bytes) -> None:
        limit = _DUMP_MAX_SECONDS * PCMA_SAMPLE_RATE * 2
        if sum(len(x) for x in self._dump_pcm) < limit:
            self._dump_pcm.append(pcm)

    def _flush_dump(self) -> None:
        if not self._dump_wav or not self._dump_pcm:
            return
        try:
            with wave.open(self._dump_wav, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(PCMA_SAMPLE_RATE)
                w.writeframes(b"".join(self._dump_pcm))
            total = sum(len(x) for x in self._dump_pcm)
            self._on_log(f"[broadcast] 解码音频已落盘: {self._dump_wav} "
                         f"({total // 2} 采样)")
        except Exception as e:
            self._on_log(f"[broadcast] 音频落盘失败: {e}")
        finally:
            self._dump_pcm = []
