"""MP4(H264/H265 + AAC) -> MPEG-2 PS 转封装（内置 ffmpeg / libav，API 方式）。

与旧「路线 A（拉 ffmpeg 子进程）」的区别：
- 不再拉起 ffmpeg 进程，而是直接用仓库内置的 ffmpeg 库（libav，经 PyAV 绑定）
  做解封装与比特流过滤，再调用本工程的 PS 封装器(app/media/ps_muxer.py)生成 PS
  临时文件交给 PsStreamer。全程进程内调用 ffmpeg 的 C API，不依赖本机安装 ffmpeg。
- ffmpeg 的库文件随仓库发布于 libs/av/（PyAV wheel 自带的 libav*.dll），构建时由
  PyInstaller 一并打包；源码态则通过 sys.path 注入自动加载，无需系统 PATH。

规则（见《GB28181_支持mp4发送功能设计.md》）：
- 视频必须是 H264 / H265，否则无法以 PS 封装，报错并拒绝发送；
- 音频若为 AAC，随 PS 复用（写入 0xc0 audio PES）；若为非 AAC（MP3/AC3/...），
  按需求仅发视频（丢弃音频）并在日志报错；
- 无音频轨亦正常（仅视频）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import typing
import ctypes
from dataclasses import dataclass

# 工程根目录（app/media/mp4_source.py -> app/media -> app -> 根目录）
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 打包态 exe 同级目录；源码态 = 工程根（保持与素材/配置一致）
USER_BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else _ROOT_DIR

TS_HZ = 90000
_VIDEO_OK = ("h264", "hevc", "h265")


class Mp4SourceError(RuntimeError):
    """MP4 源处理失败（ffmpeg 库缺失 / 编码不支持 / 转封装错误）。"""


@dataclass
class MediaProbe:
    video: typing.Optional[str] = None   # h264 / hevc / None
    audio: typing.Optional[str] = None   # aac / mp3 / ... / None
    duration: typing.Optional[float] = None   # 秒，用于估算 mux_rate


def _load_av():
    """加载内置的 ffmpeg(libav) 绑定 PyAV；找不到时返回 None。

    加载顺序：
    1) 直接 ``import av``（开发态已 pip 安装，或打包态被 PyInstaller 收集）；
    2) 回退到仓库内置 ``libs/av``：
       - 源码态：工程根/libs/av；
       - 打包态（PyInstaller onedir）：sys._MEIPASS/libs/av（即 _internal/libs/av）。
    ``libs/av`` 是 PyAV wheel 自带的 libav*.dll，确保不依赖本机安装 ffmpeg。
    av/__init__.py 内的 delvewheel 补丁会在导入时把同级的 av.libs 加入 DLL 搜索路径，
    从而正确加载 ffmpeg 的库文件。
    """
    try:
        import av
        return av
    except ImportError:
        pass
    # 候选目录：源码态工程根/libs/av；打包态 _internal/libs/av
    candidates = [os.path.join(_ROOT_DIR, "libs", "av")]
    if getattr(sys, "frozen", False):
        mei = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidates.append(os.path.join(mei, "libs", "av"))
    for cand in candidates:
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
    try:
        import av
        return av
    except ImportError:
        return None


def _packet_bytes(pkt) -> bytes:
    """从 PyAV Packet 取出载荷字节（PyAV 19 无 to_bytes，需用 buffer_ptr）。"""
    if getattr(pkt, "size", 0) == 0:
        return b""
    return ctypes.string_at(int(pkt.buffer_ptr), int(pkt.buffer_size))


def _to_90k(val, tb):
    """把某 time_base 下的时间戳换算到 90kHz（MPEG PTS 时钟）。"""
    if val is None or tb is None or getattr(tb, "den", 0) in (0, None):
        return None
    return (val * int(tb.num) * TS_HZ + int(tb.den) // 2) // int(tb.den)


def probe_streams(src: str) -> MediaProbe:
    """探测 MP4 的音视频编码（经 libav 解封装，纯函数可单测）。"""
    av = _load_av()
    if av is None:
        raise Mp4SourceError(
            "未找到内置 ffmpeg 库(libav/PyAV)。请确认 libs/av 已随仓库发布，"
            "或在运行环境安装 PyAV。")
    probe = MediaProbe()
    try:
        with av.open(src) as c:
            for s in c.streams:
                if s.type == "video" and probe.video is None:
                    probe.video = s.codec.name
                    if s.duration and s.time_base:
                        probe.duration = float(s.duration * s.time_base)
                elif s.type == "audio" and probe.audio is None:
                    probe.audio = s.codec.name
            if probe.duration is None and c.duration:
                probe.duration = c.duration / 1_000_000.0
    except Exception as e:  # av.error.InvalidDataError 等多种
        raise Mp4SourceError(f"无法解析该 MP4（不是合法媒体文件？）: {e}")
    return probe


def _ps_contains(path: str, needle: bytes, chunk: int = 1 << 20) -> bool:
    """在大文件里扫描字节序列（避免整文件读入内存）。"""
    try:
        with open(path, "rb") as f:
            prev = b""
            while True:
                data = f.read(chunk)
                if not data:
                    return False
                if needle in (prev + data):
                    return True
                prev = data[-(len(needle) - 1):] if len(needle) > 1 else b""
    except OSError:
        return False


def _safe_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# AAC(ADTS) 与 H264/H265 参数集(SPS/PPS/VPS) 辅助
# ---------------------------------------------------------------------------
def _aac_freq_index(sample_rate: int) -> int:
    """采样率 -> ADTS sampling_frequency_index（默认 44100）。"""
    table = {96000: 0, 88200: 1, 64000: 2, 48000: 3, 44100: 4, 32000: 5,
             24000: 6, 22050: 7, 16000: 8, 12000: 9, 11025: 10, 8000: 11,
             7350: 12}
    return table.get(int(sample_rate or 0), 4)


def _aac_adts_header(profile_field: int, freq_index: int, ch_cfg: int,
                     frame_len: int) -> bytes:
    """构造 7 字节 ADTS 帧头（无 CRC）。profile_field = AOT-1（AAC-LC=1）。

    国标 GB/T 28181 的 PS 音频必须为 ADTS 封装的 AAC；若仅放裸 AAC ES，
    多数平台无法界定帧边界与采样参数 -> 收不到声音，甚至因解复用失步
    连带破坏后续视频 PES（花屏）。
    """
    ch_cfg = max(1, min(7, int(ch_cfg or 1)))
    v = 0
    v = (v << 12) | 0xFFF                     # syncword
    v = (v << 1)  | 0                          # MPEG version (0=MPEG-4)
    v = (v << 2)  | 0                          # layer
    v = (v << 1)  | 1                          # protection_absent
    v = (v << 2)  | (profile_field & 0x03)      # profile
    v = (v << 4)  | (freq_index & 0x0F)         # sampling freq index
    v = (v << 1)  | 0                          # private
    v = (v << 3)  | ch_cfg                      # channel config
    v = (v << 1)  | 0                          # original/copy
    v = (v << 1)  | 0                          # home
    v = (v << 1)  | 0                          # copyright id bit
    v = (v << 1)  | 0                          # copyright id start
    v = (v << 13) | (frame_len & 0x1FFF)        # frame length
    v = (v << 11) | 0x7FF                       # buffer fullness (VBR)
    v = (v << 2)  | 0                          # num AAC frames - 1
    return v.to_bytes(7, "big")


def _annexb_nals(data: bytes):
    """遍历 AnnexB 字节流的 NAL，产出 (nal_type, start, end)；start 含起始码。"""
    i, n, starts = 0, len(data), []
    while True:
        p = data.find(b"\x00\x00\x01", i)
        if p < 0:
            break
        starts.append(p)
        i = p + 3
    for k, p in enumerate(starts):
        nxt = starts[k + 1] if k + 1 < len(starts) else n
        if nxt - p >= 4:
            yield data[p + 3] & 0x1F, p, nxt


def _param_set_types(hevc: bool) -> set:
    return {7, 8} if not hevc else {32, 33, 34}


def _extract_param_sets(data: bytes, hevc: bool) -> bytes:
    """取 AnnexB 流前导的参数集 NAL 字节（H264: SPS/PPS；HEVC: VPS/SPS/PPS）。"""
    types = _param_set_types(hevc)
    out = bytearray()
    for t, s, e in _annexb_nals(data):
        if t in types:
            out += data[s:e]
        else:
            break
    return bytes(out)


def _starts_with_param_set(data: bytes, hevc: bool) -> bool:
    types = _param_set_types(hevc)
    for t, *_ in _annexb_nals(data):
        return t in types
    return False


def _iter_ps_frames(av, src: str, do_audio: bool):
    """解封装 MP4，按解码顺序产出 PS 帧字典（{stream_id, payload, pts, dts}）。

    - 视频经 mp4toannexb 比特流过滤，输出 AnnexB（国标 PS 需要 start code）；
      并在每个 IDR 前补齐 SPS/PPS(VPS)，避免平台中途加入/丢首包/循环到点
      落在无参数集的 IDR 上而解码失败（花屏）；
    - 音频若为 AAC 且 do_audio=True，逐帧封装为 ADTS（国标 PS 要求）后写入
      0xc0 audio PES；裸 AAC ES 平台无法解码 -> 无声音。
    - 调用方负责将产出的帧喂给 ps_muxer 写盘。
    """
    with av.open(src) as ic:
        vstream = next((s for s in ic.streams if s.type == "video"), None)
        astream = next((s for s in ic.streams if s.type == "audio"), None)
        if vstream is None:
            return
        hevc = vstream.codec.name == "hevc"
        bsf_name = "h264_mp4toannexb" if not hevc else "hevc_mp4toannexb"
        bsf = av.bitstream.BitStreamFilterContext(bsf_name, vstream)
        sps_pps_prefix = None
        idr_types = {5} if not hevc else {19, 20}
        # 音频 ADTS 参数（国标 PS 的 AAC 须为 ADTS）
        adts_params = None
        if do_audio and astream is not None and astream.codec.name == "aac":
            adts_params = (1, _aac_freq_index(astream.sample_rate or 44100),
                           max(1, min(7, astream.channels or 2)))
        for packet in ic.demux():
            st = packet.stream
            if st is None:
                continue
            if st.type == "video":
                for bpkt in bsf.filter(packet):
                    d = _packet_bytes(bpkt)
                    if not d:
                        continue
                    # 取首个视频包的前导参数集(SPS/PPS/VPS)作为补齐前缀
                    if sps_pps_prefix is None:
                        sps_pps_prefix = _extract_param_sets(d, hevc)
                    # 每个 IDR 前必须带参数集：缺则补（避免平台中途加入花屏）
                    if any(t in idr_types for t, *_ in _annexb_nals(d)) \
                            and sps_pps_prefix \
                            and not _starts_with_param_set(d, hevc):
                        d = sps_pps_prefix + d
                    tb = bpkt.time_base or vstream.time_base
                    pts = _to_90k(bpkt.pts, tb)
                    pts = pts if pts is not None else _to_90k(bpkt.dts, tb)
                    dts = _to_90k(bpkt.dts, tb)
                    if dts is None:
                        dts = pts           # 视频统一写 PTS+DTS（与真实设备一致），
                        # 平台可用 DTS(解码顺序、单调) 做调度，避免被 PTS 回退(B 帧)干扰
                    yield {"stream_id": 0xE0, "payload": d,
                           "pts": pts, "dts": dts}
            elif st.type == "audio" and do_audio and astream is not None:
                if astream.codec.name != "aac" or adts_params is None:
                    continue
                d = _packet_bytes(packet)
                if not d:
                    continue
                prof, fi, cc = adts_params
                d = _aac_adts_header(prof, fi, cc, 7 + len(d)) + d
                tb = packet.time_base or astream.time_base
                pts = _to_90k(packet.pts, tb)
                pts = pts if pts is not None else _to_90k(packet.dts, tb)
                dts = _to_90k(packet.dts, tb)
                if dts is not None and dts == pts:
                    dts = None
                yield {"stream_id": 0xC0, "payload": d,
                       "pts": pts, "dts": dts}
        bsf.filter(None)  # flush


def convert_mp4_to_ps(src: str, on_log=None) -> str:
    """把 MP4 转封装为 MPEG-2 PS 临时文件并返回其路径。

    - 视频非 H264/H265 -> 抛 Mp4SourceError（无法以 PS 封装）；
    - 音频非 AAC -> 丢弃音频（仅视频），并 on_log 报错；
    - 产物为临时文件，调用方负责在用完后删除（prepare_media_source 返回 is_temp）。
    """
    log = on_log or (lambda s: None)
    av = _load_av()
    if av is None:
        raise Mp4SourceError(
            "未找到内置 ffmpeg 库(libav/PyAV)。请确认 libs/av 已随仓库发布，"
            "或在运行环境安装 PyAV。")
    from .ps_muxer import (write_ps_from_frames, _build_psm_pack,
                           STREAM_TYPE_H264, STREAM_TYPE_HEVC, STREAM_TYPE_AAC)

    probe = probe_streams(src)
    if probe.video not in _VIDEO_OK:
        raise Mp4SourceError(
            f"MP4 视频编码为 {probe.video or '未知'}，仅支持 H264/H265，无法以 PS 封装发送")

    drop_audio = False
    if probe.audio is None:
        log("[media] MP4 无音频轨，仅发送视频")
    elif probe.audio != "aac":
        log(f"[media] 错误：MP4 音频编码为 {probe.audio}，非 AAC；"
            f"按需求仅发送视频（丢弃音频）")
        drop_audio = True
    else:
        log("[media] MP4 音频为 AAC，随 PS 复用发送")

    # program_mux_rate：单位 50 字节/秒，用于对端按 P-STD 缓冲模型推算到达节奏。
    # 之前写死 2000000（=100MB/s），与 ~750KB/s 的真实码率差 100 倍，缓冲占用
    # 计算必然失真；这里按「文件大小/时长」估算，留 1.3 倍余量。
    mux_rate = 20000
    try:
        if probe.duration and probe.duration > 0:
            bytes_per_sec = os.path.getsize(src) / probe.duration
            mux_rate = int(bytes_per_sec * 1.3 / 50)
    except OSError:
        pass
    mux_rate = max(1000, min(0x3FFFFF, mux_rate))
    out_fd, out_path = tempfile.mkstemp(prefix="gbipc_", suffix=".ps")
    os.close(out_fd)
    # 构造 Program Stream Map（PSM）前导包：让解封装/解码器把 0xe0/0xc0 识别为
    # H264/H265 与 AAC（否则 ffmpeg 类解封装会误判音频为 mp2，导致平台无法解码）。
    es_entries = []
    if probe.video == "h264":
        es_entries.append((STREAM_TYPE_H264, 0xE0))
    elif probe.video == "hevc":
        es_entries.append((STREAM_TYPE_HEVC, 0xE0))
    if (not drop_audio) and probe.audio == "aac":
        es_entries.append((STREAM_TYPE_AAC, 0xC0))
    preamble = (_build_psm_pack(es_entries, mux_rate=mux_rate) if es_entries else b"")
    try:
        with open(out_path, "wb") as fo:
            n = write_ps_from_frames(fo, _iter_ps_frames(av, src, not drop_audio),
                                     mux_rate=mux_rate, preamble=preamble)
        if not os.path.getsize(out_path):
            raise Mp4SourceError("未产出 PS 数据（空文件）")
    except Mp4SourceError:
        _safe_remove(out_path)
        raise
    except Exception as e:
        _safe_remove(out_path)
        raise Mp4SourceError(f"MP4 转封装失败: {e}")
    # 产物基本校验：必须有视频 PES(0xe0)；若声明 AAC 却无 0xc0 音频 PES 则告警。
    if not _ps_contains(out_path, b"\x00\x00\x01\xe0"):
        _safe_remove(out_path)
        raise Mp4SourceError("产物缺少视频 PES(0xe0)，转封装异常")
    if probe.audio == "aac" and not _ps_contains(out_path, b"\x00\x00\x01\xc0"):
        log("[media] 警告：PS 中未检测到 0xc0 音频 PES，平台可能无法解码音频，请核对。")
    log(f"[media] MP4 已转封装为 PS: {out_path}（{n} 个 pack）")
    return out_path


# ---------------------------------------------------------------------------
# 转封装缓存：MP4→PS 是「整文件」转换（真实 274MB 样本约 2.5s），若每次点播都
# 现转，客户端 ACK 后要空等约 3s 才收到首个 RTP 包（首帧慢）。这里按
# (绝对路径, mtime, 大小) 缓存产物：同一文件重复点播零开销；文件变化则丢弃旧产物。
# ---------------------------------------------------------------------------
_CACHE: "dict[tuple, str]" = {}
_CACHE_LOCK = threading.Lock()
_INFLIGHT: "dict[tuple, threading.Event]" = {}


def _cache_key(src: str) -> tuple:
    try:
        st = os.stat(src)
        return (os.path.abspath(src), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (os.path.abspath(src), 0, 0)


def clear_media_cache() -> None:
    """删除全部缓存产物（进程退出/切换媒体文件时调用）。"""
    with _CACHE_LOCK:
        for path in list(_CACHE.values()):
            _safe_remove(path)
        _CACHE.clear()


import atexit as _atexit
_atexit.register(clear_media_cache)


def warm_media_cache(src: str, on_log=None) -> "typing.Optional[threading.Thread]":
    """后台预热：把 MP4 提前转成 PS，使首次点播也能立刻推流。

    返回预热线程（已在缓存或非 MP4 源时返回 None）。异常只记日志，不影响主流程。
    """
    log = on_log or (lambda s: None)
    if not src or os.path.splitext(src)[1].lower() not in (".mp4", ".m4v", ".mov"):
        return None
    key = _cache_key(src)
    with _CACHE_LOCK:
        if key in _CACHE and os.path.exists(_CACHE[key]):
            return None
        if key in _INFLIGHT:
            return None
        ev = threading.Event()
        _INFLIGHT[key] = ev

    def run():
        try:
            _convert_cached(src, key, log, prefetch=True)
        except Exception as e:
            log(f"[media] 媒体预热失败: {e}")
        finally:
            with _CACHE_LOCK:
                _INFLIGHT.pop(key, None)
            ev.set()

    th = threading.Thread(target=run, name="media-warmup", daemon=True)
    th.start()
    return th


def _convert_cached(src: str, key: tuple, log, prefetch: bool = False) -> str:
    """转换并写入缓存（调用方需已登记 _INFLIGHT 或持有缓存锁外的串行语义）。"""
    with _CACHE_LOCK:
        old = _CACHE.pop(key, None)
    if old:
        _safe_remove(old)
    ps = convert_mp4_to_ps(src, None if prefetch else log)
    with _CACHE_LOCK:
        _CACHE[key] = ps
    if prefetch:
        log(f"[media] 媒体预热完成: {os.path.basename(src)} -> {os.path.getsize(ps) // 1024} KB PS")
    return ps


def prepare_media_source(src: str, on_log=None
                         ) -> typing.Tuple[str, bool]:
    """按来源文件返回「可直接喂给 PsStreamer 的 PS 路径」与「是否为调用方私有临时文件」。

    - .ps 文件：原样返回 (src, False)；
    - .mp4/.m4v/.mov：经内置 ffmpeg(libav) 转封装为 PS。产物进入进程内缓存，
      返回 (ps_path, False)——**调用方不得删除**，避免其他并发呼叫共用的文件被抹掉；
    - 其它扩展名：抛 Mp4SourceError。
    ffmpeg(libav) 缺失时在此抛出明确错误，便于上层日志与界面提示。
    """
    if not src:
        raise Mp4SourceError("未指定媒体文件")
    ext = os.path.splitext(src)[1].lower()
    if ext == ".ps":
        return src, False
    if ext not in (".mp4", ".m4v", ".mov"):
        raise Mp4SourceError(f"不支持的媒体文件类型: {ext}（仅支持 .ps / .mp4）")

    log = on_log or (lambda s: None)
    key = _cache_key(src)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and os.path.exists(hit):
            log(f"[media] 命中转封装缓存: {hit}")
            return hit, False
        ev = _INFLIGHT.get(key)
    if ev is not None:
        log("[media] 等待后台转封装完成 ...")
        ev.wait(timeout=120)
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit and os.path.exists(hit):
            return hit, False
    # 未命中且无在途任务：本次同步转换
    with _CACHE_LOCK:
        _INFLIGHT[key] = threading.Event()
    try:
        ps = _convert_cached(src, key, log)
    finally:
        with _CACHE_LOCK:
            e = _INFLIGHT.pop(key, None)
        if e is not None:
            e.set()
    return ps, False
