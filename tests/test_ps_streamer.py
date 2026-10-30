"""PsStreamer / analyze_ps_stream 专项测试。

重点回归「PES 起始位置不能写死在第 15 字节」：
现场 HK264.ps 的 pack 结构是
``pack 头(14B) + FF FF + 4 字节厂商自定义 + system header(0xBB) + ... + PES``，
旧实现按 ``pack[14:18]`` 取 PES，整段解析不到 PTS -> 没有帧边界/流控 ->
整段以 CPU 速度倾泻，平台侧报 "PS frame too large / PS resync limit exceeded"。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.media.ps_streamer import analyze_ps_stream, _find_first_pes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_264 = os.path.join(ROOT, "resource", "ps_samples", "HK264.ps")


def _t(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({extra})" if extra else ""), flush=True)
    assert cond, name


def _pes(stream_id: int, pts_90k: int, payload: bytes) -> bytes:
    """构造一个带 PTS 的 PES（可选头 5 字节，flags=0x80）。"""
    body = bytes([0x80, 0x80, 0x05])
    p = pts_90k & 0x1FFFFFFFF
    body += bytes([0x21 | (((p >> 30) & 0x07) << 1),
                   (p >> 22) & 0xFF,
                   0x01 | (((p >> 15) & 0x7F) << 1),
                   (p >> 7) & 0xFF,
                   0x01 | ((p & 0x7F) << 1)])
    data = body + payload
    return b"\x00\x00\x01" + bytes([stream_id]) + len(data).to_bytes(2, "big") + data


def _pack(content: bytes, scr: int = 0) -> bytes:
    """构造 MPEG-2 PS pack 头（14 字节）+ content。"""
    b = bytearray(b"\x00\x00\x01\xba")
    b += bytes([0x44, 0x00, 0x04, 0x00, 0x04, 0x01, 0x00, 0x00, 0x03, 0xF8])
    return bytes(b) + content


def test_find_first_pes_skips_system_header_and_vendor_bytes():
    """pack 头后有填充/厂商字节/system header 时仍能定位 PES。"""
    sysh = b"\x00\x00\x01\xbb" + (6).to_bytes(2, "big") + b"\x00" * 6
    pes = _pes(0xE0, 9000, b"\x00\x00\x01\x65" + b"\xAA" * 40)
    seg = _pack(b"\xFF\xFF\x06\x65\x2C\x2C" + sysh + pes)
    sid, off = _find_first_pes(seg)
    _t("跳过厂商字节 + system header 找到 PES", sid == 0xE0, hex(sid or 0))
    _t("定位到的确实是 PES 起始码", seg[off:off + 4] == b"\x00\x00\x01\xe0")


def test_find_first_pes_plain_pack():
    """标准「pack 头 + 紧跟 PES」（本工程 ps_muxer 的产物）同样能定位。"""
    pes = _pes(0xE0, 0, b"\x00\x00\x01\x65" + b"\xBB" * 20)
    sid, off = _find_first_pes(_pack(pes))
    _t("普通 pack 定位 PES", sid == 0xE0 and off == 14)


def test_find_first_pes_continuation_pack():
    """续包（pack 头后直接是上一帧的载荷）不应误判出 PES。"""
    seg = _pack(b"\x11" * 200)
    sid, off = _find_first_pes(seg)
    _t("续包不误判 PES", sid is None, sid)


def test_analyze_synthetic_with_vendor_header():
    """带厂商头/系统头的完整小 PS 能解析出帧与单调时间轴。"""
    content = b""
    for i in range(5):
        sysh = b"\x00\x00\x01\xbb" + (6).to_bytes(2, "big") + b"\x00" * 6
        pes = _pes(0xE0, 9000 + i * 3600, b"\x00\x00\x01\x65" + bytes([i]) * 30)
        content += _pack(b"\xFF\xFF\x06\x65" + sysh + pes, scr=i)
    packs, vf, af, step = analyze_ps_stream(content)
    _t("5 个 pack 都被切出来", len(packs) == 5, len(packs))
    _t("解析出 5 帧视频", vf == 5, vf)
    _t("无音频", af == 0, af)
    _t("循环步长 = 4 帧间隔", abs(step - 4 * 3600) <= 1, step)
    pac = [p["pacing_pts"] for p in packs if p["type"] == "video"]
    _t("时间轴单调不减", pac == sorted(pac), pac)


def test_analyze_real_sample_has_pacing():
    """现场 HK264.ps：必须解析出视频帧与约 18s 的时间轴。

    这是「平台报 PS frame too large」的直接回归项——旧实现此处解析出 0 帧，
    于是没有任何流控，整段 11MB 瞬间发完。
    """
    if not os.path.exists(SAMPLE_264):
        print("  [SKIP] 未找到 resource/ps_samples/HK264.ps", flush=True)
        return
    with open(SAMPLE_264, "rb") as f:
        data = f.read()
    packs, vf, af, step = analyze_ps_stream(data)
    _t("样本切片数正常", len(packs) > 400, len(packs))
    _t("样本视频帧 > 400", vf > 400, vf)
    _t("帧时间轴约 17~19 秒", 17.0 <= step / 90000 <= 19.0, round(step / 90000, 2))
    no_pts = sum(1 for p in packs if p["pts"] is None)
    _t("每个 pack 都带 PTS", no_pts == 0, no_pts)


if __name__ == "__main__":
    print("===== PsStreamer 专项测试 =====", flush=True)
    test_find_first_pes_skips_system_header_and_vendor_bytes()
    test_find_first_pes_plain_pack()
    test_find_first_pes_continuation_pack()
    test_analyze_synthetic_with_vendor_header()
    test_analyze_real_sample_has_pacing()
    print("\nALL_PS_STREAMER_OK", flush=True)
