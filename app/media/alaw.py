"""G.711 A 律（PCMA）编解码与音频电平度量。

用途：国标语音广播（C.2.4，PT=8 / PCMA / 8 kHz / 单声道）收流后的解码与音量计算。

设计取舍：
- 解码用 256 项查表，避免依赖 ``audioop``（3.11 起弃用、3.13 已移除），
  在项目锁定的 Python 3.12 与未来运行时上行为一致；
- ``alaw_encode`` 仅供测试与测试夹具生成码流使用（产品路径只解码）；
- 全部为纯 Python + 标准库，无第三方依赖。
"""
from __future__ import annotations

import math
import sys
from array import array

# ---------------- A 律 ↔ 线性 PCM ----------------

_QUANT_MASK = 0x0F
_SEG_MASK = 0x70
_SEG_SHIFT = 4
_SIGN_BIT = 0x80


def _alaw2linear(a: int) -> int:
    """单个 A 律字节 → 有符号 16 位线性值（ITU-T G.711 标准算法）。"""
    a ^= 0x55
    t = (a & _QUANT_MASK) << 4
    seg = (a & _SEG_MASK) >> _SEG_SHIFT
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if (a & _SIGN_BIT) else -t


#: 256 项解码表：A 律字节 → int16 线性值
ALAW_DECODE_TABLE: tuple[int, ...] = tuple(_alaw2linear(i) for i in range(256))

_ALAW_SEG_END = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)


def alaw_encode_sample(pcm_val: int) -> int:
    """有符号 16 位线性值 → A 律字节（ITU-T G.711 标准算法）。"""
    if pcm_val >= 0:
        mask = 0xD5
    else:
        pcm_val = (-pcm_val) - 1
        mask = 0x55
    seg = 8
    for i, end in enumerate(_ALAW_SEG_END):
        if pcm_val <= end:
            seg = i
            break
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << _SEG_SHIFT
    if seg < 2:
        aval |= (pcm_val >> 4) & _QUANT_MASK
    else:
        aval |= (pcm_val >> (seg + 3)) & _QUANT_MASK
    return aval ^ mask


def alaw_decode(data: bytes) -> bytes:
    """A 律字节流 → PCM16 小端字节流。"""
    if not data:
        return b""
    table = ALAW_DECODE_TABLE
    out = array("h", bytes(len(data) * 2))
    for i, b in enumerate(data):
        out[i] = table[b]
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def alaw_encode(pcm: bytes) -> bytes:
    """PCM16 小端字节流 → A 律字节流（测试用）。"""
    if not pcm:
        return b""
    vals = _pcm16_values(pcm)
    return bytes(alaw_encode_sample(v) for v in vals)


def alaw_encode_tone(freq_hz: float = 1000.0, amplitude: float = 0.5,
                     samples: int = 160, sample_rate: int = 8000,
                     phase: float = 0.0) -> tuple[bytes, float]:
    """生成一段正弦音的 A 律码流。

    返回 ``(alaw_bytes, next_phase)``，便于连续生成无缝拼接的音调，
    供测试夹具（tests/）构造确定的 PCMA 媒体流。
    """
    amp = max(0.0, min(1.0, amplitude)) * 32767.0
    vals = []
    step = 2.0 * math.pi * freq_hz / float(sample_rate)
    ph = phase
    for _ in range(samples):
        vals.append(int(amp * math.sin(ph)))
        ph += step
    if ph > 2.0 * math.pi:
        ph -= 2.0 * math.pi * int(ph / (2.0 * math.pi))
    return bytes(alaw_encode_sample(v) for v in vals), ph


# ---------------- PCM 度量 ----------------

def _pcm16_values(pcm: bytes) -> array:
    n = len(pcm) // 2
    vals = array("h")
    vals.frombytes(pcm[: n * 2])
    if sys.byteorder == "big":
        vals.byteswap()
    return vals


def pcm16_rms(pcm: bytes) -> float:
    """PCM16 小端字节流的均方根（0.0 ~ 32768.0）。"""
    vals = _pcm16_values(pcm)
    if not vals:
        return 0.0
    total = 0
    for v in vals:
        total += v * v
    return math.sqrt(total / len(vals))


#: dBFS 下限，等价于「静音」，避免 -inf 参与后续运算
DBFS_FLOOR = -96.0


def rms_to_dbfs(rms: float) -> float:
    """线性 RMS → dBFS（满幅 0 dBFS，静音钳到 ``DBFS_FLOOR``）。"""
    if rms <= 0.0:
        return DBFS_FLOOR
    db = 20.0 * math.log10(rms / 32768.0)
    return db if db > DBFS_FLOOR else DBFS_FLOOR


def level_from_dbfs(dbfs: float, floor_db: float = -60.0) -> float:
    """dBFS → [0, 1] 的归一化电平（``floor_db`` 映射到 0，0 dBFS 映射到 1）。"""
    if dbfs <= floor_db:
        return 0.0
    if dbfs >= 0.0:
        return 1.0
    return (dbfs - floor_db) / (0.0 - floor_db)


def pcm16_apply_gain(pcm: bytes, percent: int) -> bytes:
    """对 PCM16 小端字节流施加增益（``percent`` = 100 表示原样），带削顶保护。"""
    if percent == 100 or not pcm:
        return pcm
    vals = _pcm16_values(pcm)
    g = percent / 100.0
    for i, v in enumerate(vals):
        s = int(v * g)
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        vals[i] = s
    if sys.byteorder == "big":
        vals.byteswap()
    return vals.tobytes()


# ---------------- 电平包络（UI 手感） ----------------

class LevelEnvelope:
    """电平包络整形：快攻慢放 + 峰值保持。

    - 上升（攻击）立即跟随，避免音量起音被拖慢；
    - 下降（释放）按 ``release`` 比例回落，避免 LED 抖动；
    - 峰值以固定步长跌落，模拟专业 VU 表的峰值指示手感。
    """

    def __init__(self, release: float = 0.25, peak_decay: float = 0.02) -> None:
        self.release = release
        self.peak_decay = peak_decay
        self.level = 0.0
        self.peak = 0.0

    def update(self, level: float) -> tuple[float, float]:
        """输入瞬时电平，返回整形后的 ``(level, peak)``。"""
        if level >= self.level:
            self.level = level
        else:
            self.level = self.level * (1.0 - self.release) + level * self.release
        if self.level >= self.peak:
            self.peak = self.level
        else:
            self.peak = max(self.level, self.peak - self.peak_decay)
        return self.level, self.peak

    def reset(self) -> None:
        self.level = 0.0
        self.peak = 0.0
