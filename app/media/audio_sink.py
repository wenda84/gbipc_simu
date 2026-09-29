"""音频播放后端抽象 —— 国标语音广播的出声侧。

设计要点：
- **拉模型（pull）**：由本模块按自己的播放节拍调用 ``on_pull(frames)`` 取 PCM，
  播放时钟与抖动缓冲消费点合一，无需额外定时器；电平计算放在 ``on_pull``
  内部（见 broadcast_session），因此**无音频设备时电平表依然可用**。
- **可降级**：``SoundDeviceSink`` 打不开设备、或运行中设备异常时，自动转为
  按时长节拍的「空播放」，只保节拍不出声，广播会话与电平指示不受影响；
  ``NullSink`` 为显式无设备实现，供测试与 CI 使用。
- ``sounddevice`` 惰性导入：未安装时应只影响出声，不影响进程启动。
"""
from __future__ import annotations

import abc
import threading
import time
from typing import Callable, Optional

PCMA_SAMPLE_RATE = 8000
PCMA_CHANNELS = 1
PCMA_BLOCK_SAMPLES = 160          # 20 ms @ 8 kHz


class AudioSink(abc.ABC):
    """播放后端抽象。"""

    name = "base"

    def __init__(self, block_samples: int = PCMA_BLOCK_SAMPLES,
                 sample_rate: int = PCMA_SAMPLE_RATE,
                 channels: int = PCMA_CHANNELS,
                 on_log: Optional[Callable[[str], None]] = None) -> None:
        self.block_samples = int(block_samples)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self._on_log = on_log or (lambda s: None)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, on_pull: Callable[[int], bytes]) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(on_pull,),
                                        name=f"audio-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._release()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 子类实现 ----

    @abc.abstractmethod
    def _run(self, on_pull: Callable[[int], bytes]) -> None: ...

    def _release(self) -> None:
        """stop() 时用于打断阻塞中的播放（子类覆盖）。"""

    def _pace_wait(self) -> bool:
        """按一块的时长等待；返回 True 表示已被要求停止。"""
        return self._stop_event.wait(self.block_samples / float(self.sample_rate))


class NullSink(AudioSink):
    """无播放：仅按播放节拍拉取数据。用于无音频设备环境与自动化测试。"""

    name = "null"

    def _run(self, on_pull: Callable[[int], bytes]) -> None:
        while not self._stop_event.is_set():
            on_pull(self.block_samples)
            if self._pace_wait():
                break


class SoundDeviceSink(AudioSink):
    """sounddevice（PortAudio）播放后端。

    以专用线程阻塞写入 ``RawOutputStream``，写入节奏即播放节奏。
    不使用回调形态：写入线程模型与 pull 消费模型天然一致，且无需处理
    cffi buffer 的跨语言语义。
    """

    name = "sounddevice"

    def __init__(self, *args, device=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.device = device
        self._stream = None
        self._degraded = False

    def _open(self):
        import sounddevice as sd
        stream = sd.RawOutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            device=self.device,
        )
        stream.start()
        return stream

    def _run(self, on_pull: Callable[[int], bytes]) -> None:
        try:
            self._stream = self._open()
            self._on_log(f"[media] 音频播放已开启（sounddevice, {self.sample_rate} Hz）")
        except Exception as e:
            self._degraded = True
            self._stream = None
            self._on_log(f"[media] 打开音频设备失败，降级为无播放（音量表仍可用）: {e}")

        while not self._stop_event.is_set():
            data = on_pull(self.block_samples)
            stream = self._stream
            if stream is None:
                if self._pace_wait():
                    break
                continue
            try:
                stream.write(data)
            except Exception as e:
                # 设备被拔出/被独占等：降级为空播放，保证广播会话与电平不中断
                self._on_log(f"[media] 播放写入失败，降级为无播放: {e}")
                self._degraded = True
                try:
                    stream.abort()
                except Exception:
                    pass
                self._stream = None

        self._on_log("[media] 音频播放已停止")

    def _release(self) -> None:
        stream = self._stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            self._stream = None


def create_audio_sink(on_log: Optional[Callable[[str], None]] = None,
                      force_null: bool = False) -> AudioSink:
    """创建播放后端；``force_null`` 或 sounddevice 不可用时返回 NullSink。

    注意：是否真正打开成功只有在 ``start()`` 后才确定，本函数只负责
    「依赖是否存在」这一层判断，运行期失败由 SoundDeviceSink 自行降级。
    """
    if force_null:
        return NullSink(on_log=on_log)
    try:
        import sounddevice  # noqa: F401
    except Exception as e:
        if on_log:
            on_log(f"[media] 未找到 sounddevice，使用无播放后端: {e}")
        return NullSink(on_log=on_log)
    return SoundDeviceSink(on_log=on_log)
