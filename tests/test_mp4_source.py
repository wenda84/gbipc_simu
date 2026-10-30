"""mp4_source / ps_muxer 的单元与集成测试（不依赖系统 ffmpeg，使用仓库内置 libav）。

运行:
    python tests/test_mp4_source.py         # 直接跑
    pytest tests/test_mp4_source.py         # 或 pytest
"""
from __future__ import annotations

import os
import sys
import tempfile

# 让脚本既能被 pytest 收集，也能 `python tests/xxx.py` 直跑
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# 仓库内置 ffmpeg(libav/PyAV) 位于 libs/av，确保可直接 import av
_LIBS_AV = os.path.join(_ROOT, "libs", "av")
if os.path.isdir(_LIBS_AV) and _LIBS_AV not in sys.path:
    sys.path.insert(0, _LIBS_AV)


def _make_synthetic_mp4(path: str, with_aac: bool):
    """用仓库内置 libav(PyAV) 合成一个 H264(+AAC) 的 MP4 测试夹具。"""
    import av

    w = av.open(path, "w")
    v = w.add_stream("libx264", rate=25)
    v.width, v.height, v.pix_fmt = 320, 240, "yuv420p"
    a = w.add_stream("aac", rate=44100, layout="stereo") if with_aac else None

    def fill(vf, i):
        vf.planes[0].update(bytes([(i * 7) % 255]) * (320 * 240))
        vf.planes[1].update(bytes([128]) * (160 * 120))
        vf.planes[2].update(bytes([128]) * (160 * 120))

    for i in range(30):
        vf = av.VideoFrame(320, 240, "yuv420p")
        fill(vf, i)
        vf.pts = i
        for pkt in v.encode(vf):
            w.mux(pkt)
        if a:
            af = av.AudioFrame(format="fltp", layout="stereo", samples=1024)
            af.sample_rate = 44100
            af.pts = i * 1024
            z = b"\x00\x00\x00\x00" * 1024
            af.planes[0].update(z)
            af.planes[1].update(z)
            for pkt in a.encode(af):
                w.mux(pkt)
    for pkt in v.encode(None):
        w.mux(pkt)
    if a:
        for pkt in a.encode(None):
            w.mux(pkt)
    w.close()


def test_ps_contains():
    from app.media.mp4_source import _ps_contains
    import tempfile as _t
    f = os.path.join(_t.gettempdir(), "t_psc.ps")
    with open(f, "wb") as fh:
        fh.write(b"abc\x00\x00\x01\xe0def\x00\x00\x01\xc0ghi")
    assert _ps_contains(f, b"\x00\x00\x01\xe0") is True
    assert _ps_contains(f, b"\x00\x00\x01\xc0") is True
    assert _ps_contains(f, b"\x00\x00\x01\xbd") is False
    os.remove(f)


def test_prepare_routing_unsupported_ext():
    from app.media.mp4_source import prepare_media_source, Mp4SourceError
    for ext in (".txt", ".mkv", ".avi", ".movie"):
        f = os.path.join(tempfile.gettempdir(), "x" + ext)
        open(f, "w").close()
        try:
            try:
                prepare_media_source(f)
                assert False, "应拒绝不支持的扩展名"
            except Mp4SourceError as e:
                assert "媒体文件类型" in str(e)
        finally:
            os.remove(f)


def test_prepare_ps_passthrough():
    from app.media.mp4_source import prepare_media_source
    f = os.path.join(tempfile.gettempdir(), "x.ps")
    with open(f, "wb") as fh:
        fh.write(b"\x00\x00\x01\xba")
    try:
        path, is_temp = prepare_media_source(f)
        assert path == f and is_temp is False
    finally:
        os.remove(f)


def test_ps_pack_and_pes_layout():
    """回归：PES 头必须为标准 MPEG-2 布局，且每个 PES 完整落在单个 pack 内。

    历史 bug：①PES 可选头 flags/长度错位（解封装器把首个 PTS 字节当作可选头
    长度，载荷偏移 33 字节 -> 花屏）；②大帧用 length=0 跨多个 pack 续发
    （libav mpegps 不支持，静默截断 -> 大片丢帧）。这两点一旦回归，真实平台
    会立刻花屏/丢帧，故用结构断言锁死。
    """
    import struct
    from app.media import ps_muxer as M
    from app.media.ps_streamer import _parse_pts, PS_HEADER

    big = bytes((i * 31 + 5) % 251 + 1 for i in range(20000))   # 需切成多个 PES
    frames = [
        {"stream_id": 0xE0, "payload": big, "pts": 90000, "dts": 81000},
        {"stream_id": 0xC0, "payload": b"\xff\xf1" + b"\x00" * 100, "pts": 90900, "dts": None},
    ]
    f = os.path.join(tempfile.gettempdir(), "t_layout.ps")
    with open(f, "wb") as fo:
        n = M.write_ps_from_frames(fo, frames)
    data = open(f, "rb").read()
    try:
        # 1) 逐 pack 切分：每个 pack 必须只含一个 PES，且总长 <= PACK_SIZE
        off, packs = 0, 0
        first_pts = None
        while off < len(data):
            assert data[off:off + 4] == PS_HEADER, (off, data[off:off + 4])
            assert data[off + 14:off + 17] == b"\x00\x00\x01", "pack 内首个字节必须是 PES 起始码"
            sid = data[off + 17]
            assert sid in (0xE0, 0xC0), hex(sid)
            plen = struct.unpack(">H", data[off + 18:off + 20])[0]
            assert 0 < plen <= 0xFFFF, "PES 长度必须有效（不允许 length=0）"
            pes_start = off + 14
            pes = data[pes_start:pes_start + 6 + plen]
            assert len(pes) == 6 + plen
            assert pes[0:4] == b"\x00\x00\x01" + bytes([sid])
            # 2) 标准可选头：byte0='10'xxxxxx，byte1 高两位为 PTS_DTS_flags
            assert (pes[6] & 0xC0) == 0x80, "PES byte0 必须以 '10' 开头"
            flags = (pes[7] >> 6) & 0x03
            assert flags in (0x00, 0x02, 0x03)
            assert pes[8] in (0, 5, 10), "PES_header_data_length 非法"
            if flags in (0x02, 0x03):
                assert _parse_pts(pes) is not None
                if first_pts is None:
                    first_pts = _parse_pts(pes)
            pack_total = 6 + plen + 14
            assert pack_total <= M.PACK_SIZE, pack_total
            off = pes_start + 6 + plen
            packs += 1
        assert packs == n
        assert first_pts == 90000, first_pts
        # 3) 大帧被切成多个 PES：每个访问单元的首个 PES 才带 PTS
        assert n > 2, n
        # 4) PTS 回读一致性（PTS+DTS 与仅 PTS 两种布局）
        for pts, dts in ((123456, 100000), (987654321, None)):
            pes = M._build_pes(0xE0, b"\x00" * 10, pts, dts)
            assert _parse_pts(pes) == pts
    finally:
        os.remove(f)


def test_mp4_to_ps_h264_aac_roundtrip():
    from app.media.mp4_source import prepare_media_source, Mp4SourceError
    from app.media.ps_streamer import analyze_ps_stream

    p = os.path.join(tempfile.gettempdir(), "t_h264_aac.mp4")
    if os.path.exists(p):
        os.remove(p)
    _make_synthetic_mp4(p, with_aac=True)
    try:
        ps_path, is_temp = prepare_media_source(p)
        # 产物进入进程内缓存（同一文件重复点播零开销），调用方不得删除
        assert is_temp is False
        data = open(ps_path, "rb").read()
        # PsStreamer 解析：视频帧 + 音频帧都有，且含 0xe0 / 0xc0 PES
        packs, vf, af, step = analyze_ps_stream(data)
        assert vf > 0 and af > 0, (vf, af)
        assert b"\x00\x00\x01\xe0" in data
        assert b"\x00\x00\x01\xc0" in data
        # PSM 必须存在，否则解封装器会把 AAC 误判为 mp2
        assert b"\x00\x00\x01\xbc" in data
        # 用 libav 回解验证规格正确（AAC 被正确识别）
        import av
        with av.open(ps_path) as c:
            names = [(s.type, s.codec.name) for s in c.streams]
        assert ("video", "h264") in names
        assert ("audio", "aac") in names
    finally:
        from app.media.mp4_source import clear_media_cache
        clear_media_cache()
        if os.path.exists(p):
            os.remove(p)


def test_mp4_to_ps_video_only():
    from app.media.mp4_source import prepare_media_source
    from app.media.ps_streamer import analyze_ps_stream

    p = os.path.join(tempfile.gettempdir(), "t_h264_only.mp4")
    if os.path.exists(p):
        os.remove(p)
    _make_synthetic_mp4(p, with_aac=False)
    try:
        ps_path, _ = prepare_media_source(p)
        data = open(ps_path, "rb").read()
        packs, vf, af, step = analyze_ps_stream(data)
        assert vf > 0 and af == 0
        assert b"\x00\x00\x01\xe0" in data
        assert b"\x00\x00\x01\xc0" not in data
    finally:
        from app.media.mp4_source import clear_media_cache
        clear_media_cache()
        if os.path.exists(p):
            os.remove(p)


def test_mp4_to_ps_non_aac_audio_dropped():
    """音频非 AAC -> 丢弃音频，仅视频；并触发报错日志。"""
    from app.media import mp4_source as mod
    from app.media.mp4_source import prepare_media_source, MediaProbe

    p = os.path.join(tempfile.gettempdir(), "t_h264_only2.mp4")
    if os.path.exists(p):
        os.remove(p)
    _make_synthetic_mp4(p, with_aac=False)
    # 伪造探测结果：视频 h264、音频 mp3（非 AAC），但底层仍用真实文件（只有视频轨）
    orig = mod.probe_streams
    mod.probe_streams = lambda src: MediaProbe(video="h264", audio="mp3")
    try:
        logs = []
        ps_path, _ = prepare_media_source(p, on_log=logs.append)
        data = open(ps_path, "rb").read()
        assert b"\x00\x00\x01\xe0" in data
        assert b"\x00\x00\x01\xc0" not in data
        assert any("非 AAC" in m for m in logs), logs
    finally:
        mod.probe_streams = orig
        from app.media.mp4_source import clear_media_cache
        clear_media_cache()
        if os.path.exists(p):
            os.remove(p)


def test_mp4_unsupported_video_rejected():
    from app.media import mp4_source as mod
    from app.media.mp4_source import prepare_media_source, MediaProbe, Mp4SourceError

    p = os.path.join(tempfile.gettempdir(), "t_vp8.mp4")
    if os.path.exists(p):
        os.remove(p)
    open(p, "wb").close()  # 伪文件即可，探测被 mock 拦截
    orig = mod.probe_streams
    mod.probe_streams = lambda src: MediaProbe(video="vp8", audio="aac")
    try:
        try:
            prepare_media_source(p)
            assert False
        except Mp4SourceError as e:
            assert "仅支持 H264/H265" in str(e)
    finally:
        mod.probe_streams = orig
        if os.path.exists(p):
            os.remove(p)


def test_ps_scr_tracks_pts():
    """回归：pack 头的 SCR 必须跟随 PTS/DTS 时间轴，mux_rate 必须与码率同量级。

    历史 bug：SCR 用「每个 pack +1」的计数器，13 秒节目 SCR 只走 0.12 秒；
    program_mux_rate 写死 2000000（=100MB/s）。按 ISO 13818-1 P-STD 缓冲模型，
    对端会据此推断「十几秒的数据在 0.1 秒内涌进来」→ 首帧等待久、播约十秒后
    缓冲模型崩掉停止。真实平台复现即此现象。
    """
    from app.media import ps_muxer as M

    frames = []
    for i in range(50):                       # 50 帧 × 40ms ≈ 2s 视频
        frames.append({"stream_id": 0xE0, "payload": b"\x00\x00\x00\x01\x41" + bytes(100),
                       "pts": i * 3600, "dts": None})
    mux_rate = 15000
    f = os.path.join(tempfile.gettempdir(), "t_scr.ps")
    with open(f, "wb") as fo:
        M.write_ps_from_frames(fo, frames, mux_rate=mux_rate)
    data = open(f, "rb").read()
    try:
        scr_list = []
        off = 0
        while off + 14 <= len(data):
            assert data[off:off + 4] == b"\x00\x00\x01\xba"
            bits = bin(int.from_bytes(data[off + 4:off + 14], "big"))[2:].zfill(80)
            base = (int(bits[2:5], 2) << 30) | (int(bits[6:21], 2) << 15) | int(bits[22:37], 2)
            rate = int(bits[48:70], 2)
            assert rate == mux_rate, rate
            scr_list.append(base)
            pes = off + 14
            plen = int.from_bytes(data[pes + 4:pes + 6], "big")
            off = pes + 6 + plen
        # SCR 单调不减，且末值与媒体时间轴（≈2s）同量级（允许 0.7s 提前量）
        assert scr_list == sorted(scr_list)
        span = scr_list[-1] - scr_list[0]
        assert 1.0 <= span / 90000.0 <= 2.2, span / 90000.0
    finally:
        os.remove(f)


def test_prepare_media_cache_hit():
    """同一 MP4 重复点播应命中缓存（首帧不再等整文件转封装）。"""
    from app.media.mp4_source import prepare_media_source, clear_media_cache

    p = os.path.join(tempfile.gettempdir(), "t_cache.mp4")
    if os.path.exists(p):
        os.remove(p)
    _make_synthetic_mp4(p, with_aac=True)
    try:
        ps1, t1 = prepare_media_source(p)
        logs = []
        ps2, t2 = prepare_media_source(p, on_log=logs.append)
        assert ps1 == ps2 and t1 is False and t2 is False
        assert any("缓存" in m for m in logs), logs
    finally:
        clear_media_cache()
        if os.path.exists(p):
            os.remove(p)


if __name__ == "__main__":
    for fn in (test_ps_contains, test_prepare_routing_unsupported_ext,
              test_prepare_ps_passthrough, test_ps_pack_and_pes_layout,
              test_ps_scr_tracks_pts, test_prepare_media_cache_hit,
              test_mp4_to_ps_h264_aac_roundtrip,
              test_mp4_to_ps_video_only, test_mp4_to_ps_non_aac_audio_dropped,
              test_mp4_unsupported_video_rejected):
        fn()
        print("OK", fn.__name__)
    print("ALL_TESTS_OK")
