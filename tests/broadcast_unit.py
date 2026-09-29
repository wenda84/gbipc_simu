"""国标广播功能单元自测（无网络、无音频设备依赖）。

覆盖：
- A 律（PCMA）编解码与标准向量；
- RMS → dBFS → 归一化电平的映射与包络整形；
- 语音广播 Offer / Subject 构造（9.12.1.3.2、附录 F、附录 K）；
- SDP 解析的 prefer 语义（audio / video 双 m 行）；
- 137 语音输出通道 ID 派生（附录 D.1）与广播应答报文（A.2.6 l）；
- PcmaReceiver 的乱序 / 丢包 / 重复 / 预缓冲行为（本机 UDP 注入）；
- 配置 JSON 往返（含旧配置缺字段的兼容）。

运行：
  runtime\\venv312\\Scripts\\python.exe tests\\broadcast_unit.py
"""
import json
import os
import socket
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import gbsdp, manscdp
from app.core.config import AppConfig, load_config, save_config
from app.media import alaw
from app.media.pcma_receiver import PcmaReceiver

_RESULTS = []


def check(name, cond, detail=""):
    _RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def _rtp(seq, ssrc, payload: bytes, pt=8, ts=None):
    return struct.pack("!BBHII", 0x80, pt, seq,
                       ts if ts is not None else seq * 160, ssrc) + payload


# ---------------- A 律 ----------------

def test_alaw():
    print("\n===== A 律（PCMA）编解码 =====", flush=True)
    t = alaw.ALAW_DECODE_TABLE
    # ITU-T G.711 标准特征值：0xD5 -> +8，0x55 -> -8（零点两侧）
    check("A 律零点 0xD5 -> +8", t[0xD5] == 8, f"got {t[0xD5]}")
    check("A 律零点 0x55 -> -8", t[0x55] == -8, f"got {t[0x55]}")
    check("A 律最大正向 0xAA -> 32256", t[0xAA] == 32256, f"got {t[0xAA]}")
    check("A 律最大负向 0x2A -> -32256", t[0x2A] == -32256, f"got {t[0x2A]}")

    # 编解码往返：A 律为有损编码，最高段量化步长 512，
    # 实测全量程最大绝对误差 512（出现在 -32768），即满幅的 1.56%（G.711 规格内）。
    worst = 0
    for v in range(-32768, 32768, 37):
        r = t[alaw.alaw_encode_sample(v)]
        worst = max(worst, abs(r - v))
    check("A 律往返误差 ≤ 512", worst <= 512, f"max={worst}")
    check("A 律往返误差 < 满幅 2%", worst / 32768.0 < 0.02,
          f"{100.0 * worst / 32768.0:.2f}%")

    # 字节级解码与逐点解码一致
    raw = bytes(range(256))
    pcm = alaw.alaw_decode(raw)
    vals = struct.unpack("<256h", pcm)
    check("alaw_decode 字节流长度/内容正确",
          len(pcm) == 512 and vals[0] == t[0] and vals[255] == t[255])


def test_level():
    print("\n===== 电平映射与包络 =====", flush=True)
    check("静音 RMS=0", alaw.pcm16_rms(bytes(320)) == 0.0)
    check("静音 dBFS 钳到 -96", alaw.rms_to_dbfs(0.0) == -96.0)
    check("满幅 32768 -> 0 dBFS", abs(alaw.rms_to_dbfs(32768.0)) < 1e-9)
    check("level(-60dBFS)=0", alaw.level_from_dbfs(-60.0) == 0.0)
    check("level(0dBFS)=1", alaw.level_from_dbfs(0.0) == 1.0)
    check("level(-30dBFS)=0.5", abs(alaw.level_from_dbfs(-30.0) - 0.5) < 1e-9)
    check("level 单调", alaw.level_from_dbfs(-50) < alaw.level_from_dbfs(-40)
          < alaw.level_from_dbfs(-10))

    # 正弦音 RMS 与理论幅度一致（±3% 覆盖量化误差）
    ab, _ = alaw.alaw_encode_tone(1000, 0.5, 800)
    rms = alaw.pcm16_rms(alaw.alaw_decode(ab))
    theory = 0.5 * 32767 / (2 ** 0.5)
    check("正弦音 RMS 与理论值相符", abs(rms - theory) / theory < 0.03,
          f"got {rms:.0f} expect {theory:.0f}")

    env = alaw.LevelEnvelope()
    env.update(0.8)
    lv, pk = env.update(0.4)
    check("攻击立即 / 释放缓慢", lv > 0.4 and lv < 0.8, f"level={lv:.3f}")
    # 峰值保持但每帧按 peak_decay 回落（本帧无新峰值，故已跌落一步 0.02）
    check("峰值保持（含单帧跌落）", 0.75 < pk < 0.8, f"peak={pk:.3f}")
    for _ in range(60):
        lv, pk = env.update(0.0)
    check("静音后电平回落至近 0", lv < 0.01 and pk < 0.01, f"level={lv:.4f}")

    check("增益 50% 削顶保护",
          max(struct.unpack("<4h", alaw.pcm16_apply_gain(struct.pack("<4h", 32767, -32768, 1000, -1000), 200)))
          == 32767)


# ---------------- SDP / Subject / 报文 ----------------

def test_sdp_and_messages():
    print("\n===== 广播 SDP / Subject / 报文 =====", flush=True)
    cfg = AppConfig()

    # 附录 D.1：第 11~13 位类型编码替换为 137
    check("137 语音输出通道 ID",
          cfg.broadcast_channel_id == cfg.device_id[:10] + "137" + cfg.device_id[13:],
          cfg.broadcast_channel_id)
    check("137 通道 ID 为 20 位数字",
          len(cfg.broadcast_channel_id) == 20 and cfg.broadcast_channel_id.isdigit())

    subj = gbsdp.build_subject("31010400001360000001", cfg.device_id, "0", "1")
    check("Subject 格式（附录 K）",
          subj == f"31010400001360000001:0,{cfg.device_id}:1", subj)
    check("Subject 发送方序列号首位为 0", subj.split(":")[1][0] == "0")

    offer = gbsdp.build_audio_offer(cfg.device_id, "192.168.1.10", 15062, "0100000001")
    for frag in ("s=Play", "m=audio 15062 RTP/AVP 8", "a=recvonly",
                 "a=rtpmap:8 PCMA/8000", "f=v/////a/1/8/1", "y=0100000001",
                 f"c=IN IP4 192.168.1.10"):
        check(f"Offer 含 {frag}", frag in offer)
    tcp = gbsdp.build_audio_offer(cfg.device_id, "192.168.1.10", 15062, "0100000001",
                                 tcp=True)
    check("TCP Offer 用 TCP/RTP/AVP + setup:passive",
          "m=audio 15062 TCP/RTP/AVP 8" in tcp and "a=setup:passive" in tcp)

    # 平台应答 SDP（9.12.1.3.2 原文）
    answer = ("v=0\r\no=64010600002020000001 00 IN IP4 172.20.16.3\r\ns=Play\r\n"
              "c=IN IP4 172.20.16.3\r\nt=0 0\r\nm=audio 8000 RTP/AVP 8\r\n"
              "a=sendonly\r\na=rtpmap:8 PCMA/8000\r\ny=0100000001\r\nf=v/////a/1/8/1\r\n")
    o = gbsdp.parse_offer(answer, prefer="audio")
    check("解析应答: media=audio", o.media.media == "audio")
    check("解析应答: y=SSRC", o.ssrc == "0100000001")
    check("解析应答: direction=sendonly", o.media.direction == "sendonly")
    check("PCMA 载荷类型识别", o.payload_for("PCMA") == 8)

    dual = ("v=0\r\ns=Play\r\nm=video 6000 RTP/AVP 96\r\na=rtpmap:96 PS/90000\r\n"
            "m=audio 8000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n")
    check("prefer=video 取视频行", gbsdp.parse_offer(dual, "video").media.media == "video")
    check("prefer=audio 取音频行", gbsdp.parse_offer(dual, "audio").media.media == "audio")
    # 点播既有行为不得回归：默认 prefer=video
    check("默认 prefer 仍为 video",
          gbsdp.parse_offer(dual).media.media == "video")

    resp = manscdp.build_broadcast_response("992", "31010403001370002272")
    for frag in ("<CmdType>Broadcast</CmdType>", "<SN>992</SN>",
                 "<DeviceID>31010403001370002272</DeviceID>", "<Result>OK</Result>",
                 "<Response>"):
        check(f"广播应答含 {frag}", frag in resp)
    err = manscdp.build_broadcast_response("7", "x")  # 缺省 Result=OK
    check("广播应答可置 ERROR",
          "<Result>ERROR</Result>" in manscdp.build_broadcast_response("7", "x", "ERROR"))

    notify = ('<?xml version="1.0"?><Notify><CmdType>Broadcast</CmdType><SN>992</SN>'
              '<SourceID>31010400001360000001</SourceID>'
              '<TargetID>31010403001370002272</TargetID></Notify>').encode()
    m = manscdp.parse(notify)
    check("解析广播通知 Notify/Broadcast",
          m is not None and m.root == "Notify" and m.cmd_type == "Broadcast" and m.sn == "992")
    check("通知 SourceID/TargetID 入 extra",
          m.extra.get("SourceID") == "31010400001360000001"
          and m.extra.get("TargetID") == "31010403001370002272")


# ---------------- 配置 ----------------

def test_config():
    print("\n===== 配置持久化 (TOML) =====", flush=True)
    cfg = AppConfig()
    check("广播默认关闭", cfg.broadcast_enabled is False)
    check("广播默认端口 15062", cfg.broadcast_media_port == 15062)
    check("广播默认抖动缓冲 60ms", cfg.broadcast_jitter_ms == 60)
    check("广播默认超时 15s", abs(cfg.broadcast_rtp_timeout - 15.0) < 1e-9)

    path = os.path.join(tempfile.mkdtemp(), "app_config.toml")
    cfg.broadcast_enabled = True
    cfg.broadcast_media_port = 16123
    cfg.broadcast_volume_percent = 140
    save_config(cfg, path)
    back = load_config(path)
    check("配置往返: 广播开关",
          back.broadcast_enabled is True)
    check("配置往返: 端口/增益",
          back.broadcast_media_port == 16123 and back.broadcast_volume_percent == 140)

    # 旧版（无广播字段）的 TOML 配置必须能平滑加载并取默认值
    with open(path, "w", encoding="utf-8") as f:
        f.write('[device]\ndevice_id = "32128401001311782461"\n\n'
                '[server]\nserver_ip = "1.2.3.4"\n')
    old = load_config(path)
    check("旧配置兼容: 缺字段取默认",
          old.broadcast_enabled is False and old.broadcast_media_port == 15062
          and old.device_id == "32128401001311782461")
    check("旧配置兼容: 137 派生仍正确",
          old.broadcast_channel_id == "32128401001371782461",
          old.broadcast_channel_id)


# ---------------- 收流行为 ----------------

def test_receiver():
    print("\n===== PCMA 收流：乱序 / 丢包 / 重复 =====", flush=True)
    port = 21680
    rx = PcmaReceiver("udp", "127.0.0.1", port, expect_ssrc="62", prebuffer_ms=60)
    rx.start()
    time.sleep(0.15)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ab, _ = alaw.alaw_encode_tone(500, 0.5, 160)
    for sq in (3000, 3002, 3001, 3003, 3005, 3005, 3006, 3007):
        tx.sendto(_rtp(sq, 62, ab), ("127.0.0.1", port))
        time.sleep(0.01)
    time.sleep(0.3)
    st = rx.stats()
    check("收包数正确", st["rtp"] == 8, f"rtp={st['rtp']}")
    check("重复检测（3005 重复）", st["dup"] == 1, f"dup={st['dup']}")
    check("乱序检测（3001 迟到）", st["ooo"] == 1, f"ooo={st['ooo']}")
    # 丢包在消费（pull）时才判定：空洞需先确认缓冲里已有更新的包，
    # 避免把「暂时没到」误判成丢包。因此这里必须先 pull 再断言 lost。
    check("消费前未误判丢包", st["lost"] == 0, f"lost={st['lost']}")

    out = rx.pull(1600)
    check("pull 长度严格等于请求量", len(out) == 1600 * 2, f"{len(out)}B")
    check("pull 内容非静音", alaw.pcm16_rms(out) > 1000)
    check("丢包检测（缺 3004）", rx.stats()["lost"] == 1, f"lost={rx.stats()['lost']}")

    # SSRC 不匹配的包必须被丢弃
    before = rx.stats()["rtp"]
    for _ in range(3):
        tx.sendto(_rtp(4000, 999, ab), ("127.0.0.1", port))
        time.sleep(0.01)
    time.sleep(0.2)
    check("SSRC 不符的包被丢弃",
          rx.stats()["rtp"] == before and rx.stats()["ssrc_mismatch"] == 3,
          f"rtp={rx.stats()['rtp']} mismatch={rx.stats()['ssrc_mismatch']}")

    # 载荷类型不匹配（PT=96 的 PS）也必须丢弃
    before = rx.stats()["rtp"]
    tx.sendto(_rtp(5000, 62, ab, pt=96), ("127.0.0.1", port))
    time.sleep(0.15)
    check("PT 不符（非 8）的包被丢弃",
          rx.stats()["rtp"] == before and rx.stats()["pt_mismatch"] == 1,
          f"pt_mismatch={rx.stats()['pt_mismatch']}")

    rx.stop()
    tx.close()
    check("stop 后线程已退出", not rx.is_running())


def main():
    test_alaw()
    test_level()
    test_sdp_and_messages()
    test_config()
    test_receiver()

    failed = [n for n, ok, _ in _RESULTS if not ok]
    print(f"\n========== 单元自测：{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过 ==========")
    if failed:
        for n in failed:
            print(" - FAIL:", n)
        sys.exit(1)
    print("全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
