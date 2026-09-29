"""国标广播端到端自测：被测设备(GbDevice) vs 迷你平台（广播扩展）。

覆盖 GB/T 28181-2016 语音广播完整设备侧流程（9.12.1.2）：
  注册(401 Digest) -> 心跳 -> Catalog 查询（校验 137 语音输出通道）
  -> 未启用广播时收到通知回 Result=ERROR 且不发呼叫
  -> 启用后：Notify/Broadcast -> 200 OK -> Response/Broadcast(OK)
             -> 设备发起 INVITE(UAC, Subject + m=audio PCMA/8)
             -> 100 / 200(SDP) -> ACK
             -> 注入 PCMA 语音流 -> 校验音量电平随幅度变化
             -> BYE -> 200，广播收敛且电平复位
  -> 再次通知可重新建链 -> 注销

运行（默认关掉出声，仅验证收流与电平；去掉环境变量即可实际放音）：
  set GBIPC_FORCE_NULL_SINK=1
  runtime\\venv312\\Scripts\\python.exe tests\\broadcast_selftest.py
"""
import os
import random
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 自动化测试默认不驱动音频硬件：只验证收流/解码/电平链路
os.environ.setdefault("GBIPC_FORCE_NULL_SINK", "1")

from app.core.config import AppConfig
from app.core.device import GbDevice, DeviceEvents, RegState
from app.media import alaw
from tests.mini_platform import (
    MiniPlatform, SERVER_ID, DEVICE_ID, PASSWORD, PLATFORM_IP, PLATFORM_PORT,
    DEVICE_PORT, build_response, log, parse_sip, md5, REALM, NONCE,
)

# 伪语音输入设备（类型编码 136），仅作为通知中的 SourceID
SOURCE_ID = "34020000001360000099"
BC_MEDIA_PORT = 15062          # 与 AppConfig.broadcast_media_port 保持一致
BC_SSRC = "0100000001"         # 平台媒体服务器声明使用的 SSRC
BC_RTP_PORT = 22000            # 平台侧媒体发送端口（仅用于 SDP 描述）


class RecordEvents(DeviceEvents):
    """记录业务通知，供断言使用。"""

    def __init__(self):
        self.levels = []            # (monotonic, level, peak)
        self.bc_states = []
        self.logs = []
        self.lock = threading.Lock()

    def on_log(self, msg):
        with self.lock:
            self.logs.append(msg)
        log(f"[device] {msg}")

    def on_reg_state(self, state, detail=""):
        log(f"[device] 注册状态: {state.value} {detail}")

    def on_call_state(self, text):
        log(f"[device] 呼叫状态: {text}")

    def on_stream_stats(self, text):
        pass

    def on_broadcast_state(self, text):
        with self.lock:
            self.bc_states.append(text)
        log(f"[device] 广播状态: {text}")

    def on_broadcast_level(self, level, peak):
        with self.lock:
            self.levels.append((time.monotonic(), level, peak))

    def on_broadcast_stream(self, text):
        pass

    # ---- 查询辅助 ----

    def level_range(self, t0, t1):
        with self.lock:
            vals = [l for (t, l, _p) in self.levels if t0 <= t < t1]
        return (min(vals), max(vals)) if vals else (None, None)

    def last_level(self):
        with self.lock:
            return self.levels[-1][1] if self.levels else None


class BroadcastPlatform(MiniPlatform):
    """在既有迷你平台上叠加语音广播（平台侧）能力。"""

    def __init__(self):
        super().__init__()
        # wait_msg 会把命中的报文从 inbox 取走，回 200/BYE 又需要 INVITE 的原始
        # 头域，故这里单独留存一份。
        self.invites = {}
        # 广播场景中**设备是 UAC**、本夹具是 UAS：对话的本地 tag 由我方在
        # 100/200 OK 中产生，随后发 BYE 时必须复用同一个 tag，否则设备无法
        # 匹配对话框会回 481。
        self.local_tag = ""

    # ---------- 收发（覆盖父类以自动应答设备侧 BYE） ----------

    def _rx_loop(self):
        """复刻父类收发循环，并额外对设备侧 BYE 自动回 200。

        设备在「入流超时」等场景会主动发 BYE；若不即时回 200，pjsua 会按
        T1 退避反复重传，既拖慢收敛又干扰 inbox 断言。
        """
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                s, h, b = parse_sip(data)
            except Exception:
                continue
            if s.startswith("BYE"):
                try:
                    self.send(build_response(200, "OK", h), addr)
                except Exception:
                    pass
                continue
            if s.startswith("MESSAGE") and "Keepalive" in b:
                try:
                    self.send(build_response(200, "OK", h), addr)
                except Exception:
                    pass
            with self._lock:
                self.inbox.append((s, h, b, addr))

    # ---------- 阶段：Catalog ----------

    def query_catalog(self, expect_channel_137: bool, timeout=10):
        body = ('<?xml version="1.0" encoding="GBK"?>\n<Query><CmdType>Catalog</CmdType>'
                f'<SN>3501</SN><DeviceID>{DEVICE_ID}</DeviceID></Query>').encode("gbk")
        call_id = f"pf-cat-{random.randint(10000, 99999)}"
        req = (f"MESSAGE sip:{DEVICE_ID}@{PLATFORM_IP}:{DEVICE_PORT} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {PLATFORM_IP}:{PLATFORM_PORT};rport;branch=z9hG4bK-c{random.randint(1000, 9999)}\r\n"
               f"From: <sip:{SERVER_ID}@{PLATFORM_IP}:{PLATFORM_PORT}>;tag=pfcat\r\n"
               f"To: <sip:{DEVICE_ID}@{PLATFORM_IP}:{PLATFORM_PORT}>\r\n"
               f"Call-ID: {call_id}\r\nCSeq: 2 MESSAGE\r\n"
               f"Contact: <sip:{PLATFORM_IP}:{PLATFORM_PORT}>\r\nMax-Forwards: 70\r\n"
               f"Content-Type: Application/MANSCDP+xml\r\nContent-Length: {len(body)}\r\n\r\n"
               ).encode() + body
        self.send(req)
        if not self.wait_msg(lambda s, h, b: s.startswith("SIP/2.0 200")
                             and h.get("call-id") == call_id, timeout, "Catalog 的 200"):
            return False
        got = self.wait_msg(lambda s, h, b: s.startswith("MESSAGE")
                            and "Catalog" in b and "<Response>" in b, timeout,
                            "Catalog Response")
        if not got:
            return False
        _, h, b, addr = got
        if "<CmdType>Catalog</CmdType>" not in b or "<SN>3501</SN>" not in b:
            self.fail("Catalog Response 缺 CmdType/SN 回显")
            return False
        chan137 = DEVICE_ID[:10] + "137" + DEVICE_ID[13:]
        has137 = f"<DeviceID>{chan137}</DeviceID>" in b
        if expect_channel_137 != has137:
            self.fail(f"Catalog 137 语音输出通道上报不符: expect={expect_channel_137} got={has137}")
            return False
        if expect_channel_137:
            if "<SumNum>2</SumNum>" not in b:
                self.fail(f"Catalog 含 137 通道时 SumNum 应为 2: {b}")
                return False
            if b.count("<Item>") != 2:
                self.fail(f"Catalog 应含 2 个 Item: {b.count('<Item>')}")
                return False
            if f"<ParentID>{DEVICE_ID}</ParentID>" not in b:
                self.fail("137 通道的 ParentID 应为主设备 ID（9.12.1.1）")
                return False
        self.send(build_response(200, "OK", h), addr)
        log(f"Catalog 校验通过（137 通道{'已' if expect_channel_137 else '未'}上报）")
        return True

    # ---------- 阶段：广播通知 ----------

    def notify_broadcast(self, target_id, sn="992", source_id=SOURCE_ID):
        """发送语音广播通知（A.2.5 d）。返回该 MESSAGE 的 Call-ID。"""
        body = ('<?xml version="1.0" encoding="GB2312"?>\n<Notify>'
                '<CmdType>Broadcast</CmdType>'
                f'<SN>{sn}</SN>'
                f'<SourceID>{source_id}</SourceID>'
                f'<TargetID>{target_id}</TargetID>'
                '</Notify>').encode("gbk")
        call_id = f"pf-bc-{random.randint(10000, 99999)}"
        req = (f"MESSAGE sip:{target_id}@{PLATFORM_IP}:{DEVICE_PORT} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {PLATFORM_IP}:{PLATFORM_PORT};rport;branch=z9hG4bK-b{random.randint(1000, 9999)}\r\n"
               f"From: <sip:{SERVER_ID}@{PLATFORM_IP}:{PLATFORM_PORT}>;tag=pfbc\r\n"
               f"To: <sip:{target_id}@{PLATFORM_IP}:{PLATFORM_PORT}>\r\n"
               f"Call-ID: {call_id}\r\nCSeq: 3 MESSAGE\r\n"
               f"Contact: <sip:{PLATFORM_IP}:{PLATFORM_PORT}>\r\nMax-Forwards: 70\r\n"
               f"Content-Type: Application/MANSCDP+xml\r\nContent-Length: {len(body)}\r\n\r\n"
               ).encode() + body
        self.send(req)
        return call_id

    def expect_notify_200(self, call_id, timeout=8):
        """信令 2：语音流接收者回 200 OK（由 SIP 协议栈自动应答）。"""
        return bool(self.wait_msg(lambda s, h, b: s.startswith("SIP/2.0 200")
                                  and h.get("call-id") == call_id, timeout,
                                  f"广播通知 {call_id} 的 200 OK"))

    def expect_broadcast_response(self, sn, result="OK", timeout=8):
        """信令 3：Response/Broadcast。"""
        got = self.wait_msg(lambda s, h, b: s.startswith("MESSAGE")
                            and "Broadcast" in b and "<Response>" in b
                            and f"<SN>{sn}</SN>" in b, timeout,
                            f"Response/Broadcast (SN={sn})")
        if not got:
            return False
        _, h, b, addr = got
        if f"<Result>{result}</Result>" not in b:
            self.fail(f"Response/Broadcast 的 Result 应为 {result}: {b}")
            return False
        if "<CmdType>Broadcast</CmdType>" not in b:
            self.fail("Response/Broadcast 缺 CmdType")
            return False
        if result == "OK" and f"<DeviceID>{DEVICE_ID}</DeviceID>" not in b:
            self.fail(f"Response/Broadcast 的 DeviceID 应为 {DEVICE_ID}: {b}")
            return False
        self.send(build_response(200, "OK", h), addr)
        log(f"Response/Broadcast 校验通过 (SN={sn}, Result={result})")
        return True

    # ---------- 阶段：接收设备的广播 INVITE ----------

    def expect_broadcast_invite(self, timeout=10):
        """信令 5：设备（UAC）发起 INVITE。返回 (call_id, device_media_ip, device_media_port) 或 None。"""
        got = self.wait_msg(lambda s, h, b: s.startswith("INVITE")
                            and "m=audio" in b, timeout, "广播 INVITE")
        if not got:
            return None
        _, h, b, addr = got
        self.device_addr = addr
        call_id = h.get("call-id", "")

        # ---- Subject（附录 K）----
        subj = h.get("subject", "")
        want_prefix = f"{SOURCE_ID}:0,{DEVICE_ID}:"
        if not subj.startswith(want_prefix):
            self.fail(f"Subject 格式不符: {subj!r} 应以 {want_prefix!r} 开头")
            return None

        # ---- To 头域应为语音流发送者（9.12.1.2 信令 5）----
        if SOURCE_ID not in h.get("to", ""):
            self.fail(f"INVITE 的 To 未指向语音流发送者 SourceID: {h.get('to')!r}")
            return None

        # ---- SDP（9.12.1.3.2 + 附录 F）----
        for frag in ("s=Play", "a=recvonly", "a=rtpmap:8 PCMA/8000", "f=v/////a/1/8/1"):
            if frag not in b:
                self.fail(f"广播 INVITE 的 SDP 缺少 {frag}: {b!r}")
                return None
        if "m=audio" not in b or "RTP/AVP 8" not in b:
            self.fail(f"广播 INVITE 的 m 行应为 audio/RTP/AVP 8: {b!r}")
            return None

        media_ip, media_port = None, None
        for line in b.replace("\r\n", "\n").split("\n"):
            if line.startswith("c="):
                media_ip = line.split()[2]
            elif line.startswith("m=audio"):
                media_port = int(line.split()[1])
        if not media_ip or not media_port:
            self.fail(f"无法从广播 INVITE SDP 解析媒体地址: {b!r}")
            return None
        log(f"广播 INVITE 校验通过: Subject={subj} 收流地址={media_ip}:{media_port}")
        self.invites[call_id] = h
        self.local_tag = f"pfbc{random.randint(1000, 9999)}"
        return call_id, media_ip, media_port

    def answer_broadcast_invite(self, call_id, device_media_ip, timeout=10):
        """信令 14/15：100 -> 200(SDP) -> ACK。"""
        h = self.invites.get(call_id)
        if h is None:
            self.fail("回 200 前未留存该呼叫的 INVITE 头域")
            return False
        # 预置 To 的 tag（build_response 见到已有 tag 便不再自行生成），
        # 使 100/200 与本方后续 BYE 使用同一个本地 tag。
        resp_h = dict(h)
        resp_h["to"] = f"{h.get('to', '')};tag={self.local_tag}"
        self.send(build_response(100, "Trying", resp_h))
        sdp = ("v=0\r\n"
               f"o={SOURCE_ID} 0 0 IN IP4 {PLATFORM_IP}\r\n"
               "s=Play\r\n"
               f"c=IN IP4 {PLATFORM_IP}\r\n"
               "t=0 0\r\n"
               f"m=audio {BC_RTP_PORT} RTP/AVP 8\r\n"
               "a=sendonly\r\n"
               "a=rtpmap:8 PCMA/8000\r\n"
               f"y={BC_SSRC}\r\n"
               "f=v/////a/1/8/1\r\n").encode()
        self.send(build_response(200, "OK", resp_h, [
            ("Contact", f"<sip:{PLATFORM_IP}:{PLATFORM_PORT}>"),
            ("Content-Type", "application/sdp"),
        ], sdp))
        got = self.wait_msg(lambda s, hh, b: s.startswith("ACK")
                            and hh.get("call-id") == call_id, timeout, "广播呼叫的 ACK")
        if not got:
            return False
        log("广播呼叫已建立（100 -> 200 -> ACK）")
        return True

    # ---------- 阶段：注入 PCMA 语音流 ----------
    def send_pcma_stream(self, dst_ip, dst_port, seconds, amplitude,
                         freq=1000.0, seq_start=1000, phase=0.0, realtime=True):
        """按 20 ms/包向设备收流端口注入 PCMA（PT=8）语音流。返回 (下一 seq, 下一相位)。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tries = int(round(seconds / 0.02))
        seq = seq_start
        ph = phase
        t0 = time.monotonic()
        try:
            for i in range(tries):
                ab, ph = alaw.alaw_encode_tone(freq, amplitude, 160, phase=ph)
                pkt = struct.pack("!BBHII", 0x80, 8, seq & 0xFFFF,
                                  (i * 160) & 0xFFFFFFFF, int(BC_SSRC)) + ab
                sock.sendto(pkt, (dst_ip, dst_port))
                seq += 1
                if realtime:
                    nxt = t0 + (i + 1) * 0.02
                    gap = nxt - time.monotonic()
                    if gap > 0:
                        time.sleep(gap)
        finally:
            sock.close()
        return seq, ph

    # ---------- 阶段：BYE ----------

    def send_bye(self, call_id, timeout=10):
        h = self.invites.get(call_id)
        if h is None:
            self.fail("发 BYE 前未留存该呼叫的 INVITE 头域")
            return False
        # 本夹具是 UAS：BYE 的 From 必须带我方在 200 OK 中产生的本地 tag，
        # To 用设备 INVITE 的 From（含设备 tag），Request-URI 用设备 Contact，
        # 本地 CSeq 取设备 INVITE 的 CSeq + 1（RFC 3261 §12.2.1.1）。
        ruri = h.get("contact", f"<sip:{DEVICE_ID}@{PLATFORM_IP}:{DEVICE_PORT}>").strip("<>")
        try:
            cseq = int(h.get("cseq", "1 INVITE").split()[0]) + 1
        except ValueError:
            cseq = 2
        bye = (f"BYE {ruri} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {PLATFORM_IP}:{PLATFORM_PORT};rport;branch=z9hG4bK-byeb{random.randint(1000, 9999)}\r\n"
               f"From: <sip:{SERVER_ID}@{PLATFORM_IP}:{PLATFORM_PORT}>;tag={self.local_tag}\r\n"
               f"To: {h.get('from')}\r\n"
               f"Call-ID: {call_id}\r\nCSeq: {cseq} BYE\r\n"
               f"Max-Forwards: 70\r\nContent-Length: 0\r\n\r\n").encode()
        self.send(bye)
        got = self.wait_msg(lambda s, hh, b: s.startswith("SIP/2.0 200")
                            and hh.get("call-id") == call_id
                            and hh.get("cseq", "").startswith(str(cseq)),
                            timeout, "BYE 的 200")
        if not got:
            return False
        log("BYE 已确认（200 OK）")
        return True

    def assert_no_invite(self, timeout=2.0, desc="(期望无 INVITE)"):
        """断言指定时间内没有收到 INVITE（用于未启用广播的拒绝路径）。

        不能用 wait_msg：它以「未命中即记失败」为语义，与「期望不存在」相反。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                hit = any(item[0].startswith("INVITE") for item in self.inbox)
            if hit:
                self.fail(f"{desc} 但收到了 INVITE")
                return False
            time.sleep(0.05)
        return True


def wait_until(pred, timeout=8.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def main():
    ps_file = os.path.join("resource", "ps_samples", "g711a.ps")
    cfg = AppConfig(
        local_ip="127.0.0.1",
        local_sip_port=DEVICE_PORT,
        server_id=SERVER_ID,
        server_domain=f"{PLATFORM_IP}:{PLATFORM_PORT}",
        server_ip=PLATFORM_IP,
        server_port=PLATFORM_PORT,
        device_id=DEVICE_ID,
        auth_username=DEVICE_ID,
        password=PASSWORD,
        register_expires=3600,
        register_retry_interval=3,
        keepalive_interval=2,
        keepalive_timeout_count=3,
        local_media_port=15060,
        ps_file=ps_file,
        loop_play=True,
        broadcast_enabled=True,
        broadcast_media_port=BC_MEDIA_PORT,
        broadcast_jitter_ms=60,
        # 取值需长于「收流结束 → 本夹具主动发 BYE」的时间窗，避免设备先按
        # 入流超时自行释放；入流超时本身的验证单独用一个更短的阈值。
        broadcast_rtp_timeout=8.0,
        device_name="GBIPC-SIM-Test",
        manufacturer="Simulator",
        model="GBIPC-SIM-1000",
        firmware="V1.0.0",
        channel=1,
        civil_code="340200",
    )

    pf = BroadcastPlatform()
    pf.start()
    ev = RecordEvents()
    dev = GbDevice(cfg, ev)
    dev.start()

    def phase(name, fn):
        print(f"\n===== 阶段: {name} =====", flush=True)
        ok = bool(fn())
        if ok:
            print(f">>> {name} PASS", flush=True)
        else:
            print(f">>> {name} FAIL", flush=True)
        return ok

    ok_all = True
    ok_all &= phase("注册(401 Digest)", lambda: pf.expect_register())
    if not ok_all:
        _finish(pf, dev)
    ok_all &= phase("心跳", lambda: pf.expect_keepalive())
    ok_all &= phase("Catalog 查询（已启用广播 -> 上报 137 通道）",
                    lambda: pf.query_catalog(expect_channel_137=True))

    # ---------- 未启用广播：明确拒绝 ----------
    def reject_phase():
        dev.cfg.broadcast_enabled = False
        cid = pf.notify_broadcast(DEVICE_ID, sn="901")
        if not pf.expect_notify_200(cid):
            return False
        if not pf.expect_broadcast_response("901", result="ERROR"):
            return False
        ok = pf.assert_no_invite(timeout=2.0)
        dev.cfg.broadcast_enabled = True
        return ok

    ok_all &= phase("未启用广播 -> 回 Result=ERROR 且不发起呼叫", reject_phase)

    # ---------- 广播全流程 ----------
    bc = {}

    def notify_phase():
        cid = pf.notify_broadcast(DEVICE_ID, sn="992")
        if not pf.expect_notify_200(cid):
            return False
        return pf.expect_broadcast_response("992", result="OK")

    def invite_phase():
        got = pf.expect_broadcast_invite()
        if not got:
            return False
        bc["call_id"], bc["ip"], bc["port"] = got
        bc["port_ok"] = (bc["port"] == BC_MEDIA_PORT)
        return bc["port_ok"]

    def answer_phase():
        return pf.answer_broadcast_invite(bc["call_id"], bc["ip"])

    def confirmed_phase():
        return wait_until(lambda: "广播中" in "".join(ev.bc_states), timeout=5)

    ok_all &= phase("广播通知 -> 200 + Response/Broadcast(OK)", notify_phase)
    ok_all &= phase("设备发起 INVITE（Subject / To / SDP 校验）", invite_phase)
    ok_all &= phase("100 -> 200(SDP) -> ACK", answer_phase)
    ok_all &= phase("设备进入广播中状态", confirmed_phase)

    # ---------- 语音流 + 电平 ----------
    def level_phase():
        t_low0 = time.monotonic()
        seq, ph = pf.send_pcma_stream(bc["ip"], bc["port"], 1.2, 0.02, seq_start=1000)
        t_low1 = time.monotonic()
        pf.send_pcma_stream(bc["ip"], bc["port"], 1.2, 0.60, seq_start=seq, phase=ph)
        t_high1 = time.monotonic()
        time.sleep(0.3)
        lo = ev.level_range(t_low0 + 0.2, t_low1)       # 跳过预缓冲
        hi = ev.level_range(t_low1 + 0.2, t_high1)
        log(f"[test] 电平上报总数={len(ev.levels)} 低幅段范围={lo} 高幅段范围={hi}")
        if lo[1] is None or hi[1] is None:
            pf.fail(f"未收到电平上报（累计 {len(ev.levels)} 条）")
            return False
        if not (hi[1] > 0.7):
            pf.fail(f"高幅度语音应点亮至 0.7 以上: {hi}")
            return False
        if not (lo[1] < 0.45):
            pf.fail(f"低幅度语音电平应低于 0.45: {lo}")
            return False
        if not (hi[1] > lo[1] + 0.3):
            pf.fail(f"电平未随音量变化: low={lo} high={hi}")
            return False
        return True

    ok_all &= phase("注入 PCMA 语音流 -> 电平随音量变化", level_phase)

    # ---------- BYE 收敛 ----------
    def bye_phase():
        if not pf.send_bye(bc["call_id"]):
            return False
        if not wait_until(lambda: "空闲" in "".join(ev.bc_states[-2:]), timeout=6):
            pf.fail(f"BYE 后广播状态未回到空闲: {ev.bc_states[-3:]}")
            return False

        def _level_reset():
            lv = ev.last_level()
            return lv is not None and lv <= 0.001      # 注意 0.0 为假值，不能用 or 兜底

        if not wait_until(_level_reset, timeout=3):
            pf.fail(f"BYE 后音量表未复位: {ev.last_level()}")
            return False
        return True

    ok_all &= phase("BYE -> 200，广播收敛且电平复位", bye_phase)

    # ---------- 再次通知可重新建链 ----------
    def rearm_phase():
        cid = pf.notify_broadcast(DEVICE_ID, sn="993")
        if not pf.expect_notify_200(cid) or not pf.expect_broadcast_response("993"):
            return False
        got = pf.expect_broadcast_invite()
        if not got:
            return False
        pf.answer_broadcast_invite(got[0], got[1])
        if not pf.send_bye(got[0]):
            return False
        return wait_until(lambda: "空闲" in "".join(ev.bc_states[-2:]), timeout=6)

    ok_all &= phase("再次广播通知可重新建链并正常收敛", rearm_phase)

    # ---------- 入流超时（附录 M b） ----------
    def timeout_phase():
        dev.cfg.broadcast_rtp_timeout = 3.0      # 缩短阈值以便快速收敛验证
        cid = pf.notify_broadcast(DEVICE_ID, sn="994")
        if not pf.expect_notify_200(cid) or not pf.expect_broadcast_response("994"):
            dev.cfg.broadcast_rtp_timeout = 8.0
            return False
        got = pf.expect_broadcast_invite()
        if not got:
            dev.cfg.broadcast_rtp_timeout = 8.0
            return False
        pf.answer_broadcast_invite(got[0], got[1])
        log("[test] 不发语音流，等待入流超时（3s 阈值）自动释放链路…")
        ok = wait_until(lambda: "空闲" in "".join(ev.bc_states[-2:]), timeout=14)
        dev.cfg.broadcast_rtp_timeout = 8.0
        return ok

    ok_all &= phase("入流中断超时 -> 按附录 M 主动释放", timeout_phase)

    # ---------- 注销 ----------
    def unregister_phase():
        result = {}

        def _wait():
            result["ok"] = pf.expect_unregister(timeout=15)

        t = threading.Thread(target=_wait, daemon=True)
        t.start()
        time.sleep(0.3)
        dev.stop()
        t.join(timeout=20)
        return result.get("ok", False)

    print("\n===== 阶段: 注销 =====", flush=True)
    if unregister_phase():
        print(">>> 注销 PASS", flush=True)
    else:
        ok_all = False

    _finish(pf, dev)

    if pf.failures:
        print(f"\n========== 广播自测失败 {len(pf.failures)} 处 ==========")
        for f in pf.failures:
            print(" -", f)
        sys.exit(1)
    if not ok_all:
        print("\n========== 广播自测存在失败阶段 ==========")
        sys.exit(1)
    print("\n========== 国标广播端到端自测全部通过 ==========")
    sys.exit(0)


def _finish(pf, dev):
    try:
        dev.stop()
    except Exception:
        pass
    try:
        pf.stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()
