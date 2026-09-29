"""迷你 GB 平台仿真（测试夹具，非产品代码）。

用原始 socket 模拟 178 平台行为，全链路验证被测设备：
REGISTER(401 Digest 校验) -> Keepalive -> DeviceInfo 查询应答 ->
INVITE(TCP/UDP 两种) -> 200 SDP 校验 -> 媒体帧校验(RFC4571/SSRC/PT/PS) -> BYE -> 注销。
"""
from __future__ import annotations

import hashlib
import random
import socket
import threading
import time

SERVER_ID = "34020000002000000001"
DEVICE_ID = "34020000001320000001"
PLATFORM_IP = "127.0.0.1"
PLATFORM_PORT = 5080
DEVICE_PORT = 5060
MEDIA_PORT_TCP = 21006
MEDIA_PORT_UDP = 21008
REALM = "3402000000"
NONCE = "0123456789abcdef0123456789abcdef"
PASSWORD = "test1234"
SSRC_Y = "0000000062"


def log(msg):
    print(f"[platform] {msg}", flush=True)


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def parse_sip(data: bytes):
    text = data.decode("utf-8", errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    start = lines[0]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return start, headers, body


def build_response(code: int, reason: str, req_headers: dict,
                   extra_headers=(), body: bytes = b"") -> bytes:
    lines = [f"SIP/2.0 {code} {reason}"]
    for h in ("via", "from", "call-id", "cseq"):
        if h in req_headers:
            lines.append(f"{h.title()}: {req_headers[h]}")
    to = req_headers.get("to", "")
    if to and "tag=" not in to:
        to += f";tag=pf{random.randint(100000, 999999)}"
    lines.append(f"To: {to}")
    lines.append(f"User-Agent: MiniPlatform")
    for k, v in extra_headers:
        lines.append(f"{k}: {v}")
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


class MiniPlatform:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((PLATFORM_IP, PLATFORM_PORT))
        self.sock.settimeout(0.5)
        self.inbox = []           # (start, headers, body, addr)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._rx_loop, daemon=True)
        self.device_addr = (PLATFORM_IP, DEVICE_PORT)
        self.failures = []

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.thread.join(timeout=2)
        self.sock.close()

    def fail(self, msg):
        log(f"!! FAIL: {msg}")
        self.failures.append(msg)

    def dump_inbox(self):
        """诊断用：打印当前 inbox 中缓存的报文摘要。"""
        with self._lock:
            items = list(self.inbox)
        log(f"inbox 残留 {len(items)} 条:")
        for s, h, b, addr in items:
            log(f"   {s}  (from {addr}, {len(b)}B)")

    # ---------- 收发 ----------

    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                parsed = parse_sip(data)
            except Exception:
                continue
            s, h, b = parsed
            with self._lock:
                self.inbox.append((*parsed, addr))
            # 真实平台会对设备的周期 Keepalive MESSAGE 回 200 OK。
            # 夹具必须在后台自动应答，否则设备侧 _ka_pending 会持续累积、
            # 误判平台离线并触发重注册（进而干扰注销阶段）。
            # 注意：仅自动应答设备发来的心跳 MESSAGE，不影响其余校验逻辑。
            if s.startswith("MESSAGE") and "Keepalive" in b:
                try:
                    self.send(build_response(200, "OK", h), addr)
                except Exception:
                    pass

    def wait_msg(self, predicate, timeout=10.0, desc=""):
        deadline = time.monotonic() + timeout
        idx = 0
        while time.monotonic() < deadline:
            with self._lock:
                buf, self.inbox = self.inbox, []
            for item in buf:
                if predicate(item[0], item[1], item[2]):
                    return item
                self.inbox.append(item)
            time.sleep(0.05)
        self.dump_inbox()
        self.fail(f"等待超时: {desc}")
        return None

    def send(self, data: bytes, addr=None):
        self.sock.sendto(data, addr or self.device_addr)

    # ---------- 阶段 1：注册（401 Digest 校验） ----------

    def expect_register(self, expires_expect=None, timeout=15):
        """返回 True/False。expires_expect=None 普通注册，0 注销。

        与真实平台一致的 Digest 处理：
        - 首趟 REGISTER 不带 Authorization 时回 401 挑战；
        - 设备补发的带鉴权 REGISTER 按当前 nonce 严格校验 Digest；
        - 收到带鉴权 REGISTER 后回 200（注销则 Expires:0）。

        注意：本方法必须在设备仍存活时调用——注销的 401 挑战需要设备侧
        的注册上下文来应答。若设备已 shutdown，其注册上下文被销毁，
        401 将无人应答。
        """
        got = self.wait_msg(lambda s, h, b: s.startswith("REGISTER"), timeout,
                            "首趟 REGISTER")
        if not got:
            return False
        _, h, _, addr = got
        self.device_addr = addr

        if "authorization" not in h:
            # 无鉴权：回 401 挑战，等第二轮
            self.send(build_response(401, "Unauthorized", h, [
                ("WWW-Authenticate",
                 f'Digest realm="{REALM}",nonce="{NONCE}",opaque="abc123",qop="auth,auth-int"'),
            ]), addr)
            got = self.wait_msg(lambda s, h, b: s.startswith("REGISTER")
                                and "authorization" in h, timeout, "带鉴权 REGISTER")
            if not got:
                return False
            _, h, _, addr = got

        auth = h.get("authorization", "")
        kv = {}
        for part in auth[len("Digest "):].split(","):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                kv[k] = v.strip('"')
        ha1 = md5(f"{kv.get('username')}:{REALM}:{PASSWORD}")
        ha2 = md5(f"REGISTER:{kv.get('uri')}")
        expect = md5(f"{ha1}:{NONCE}:{kv.get('nc')}:{kv.get('cnonce')}:"
                     f"{kv.get('qop')}:{ha2}")
        if kv.get("response") != expect:
            self.fail(f"Digest 校验失败: got {kv.get('response')} expect {expect}")
            return False
        exp_hdr = h.get("expires", "")
        if expires_expect is not None and exp_hdr != str(expires_expect):
            self.fail(f"Expires 不符: got {exp_hdr} expect {expires_expect}")
            return False
        extra = [("Expires", exp_hdr or "3600"),
                 ("Date", time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()))]
        contact = h.get("contact", "")
        if contact:
            extra.append(("Contact", f"{contact};expires={exp_hdr or '3600'}"))
        self.send(build_response(200, "OK", h, extra), addr)
        log(f"注册{'注销' if expires_expect == 0 else ''}校验通过 "
            f"(qop={kv.get('qop')}, nc={kv.get('nc')})")
        return True

    def expect_unregister(self, timeout=15):
        """注销专用：在设备存活期间完成「REGISTER(Expires:0) -> 401 -> 带鉴权 -> 200」。

        必须在 dev.stop() 之前于后台线程调用，或与触发注销的调用并行，
        以便设备的注册上下文仍能应答 401 挑战。
        """
        return self.expect_register(expires_expect=0, timeout=timeout)

    # ---------- 阶段 2：心跳 ----------

    def expect_keepalive(self, timeout=10):
        got = self.wait_msg(lambda s, h, b: s.startswith("MESSAGE")
                            and "Keepalive" in b, timeout, "Keepalive MESSAGE")
        if not got:
            return False
        _, h, b, addr = got
        if "<Status>OK</Status>" not in b:
            self.fail("Keepalive 缺少 Status=OK")
            return False
        if DEVICE_ID not in b:
            self.fail("Keepalive DeviceID 不符")
            return False
        self.send(build_response(200, "OK", h), addr)
        log("心跳校验通过")
        return True

    # ---------- 阶段 3：DeviceInfo 查询 ----------

    def query_device_info(self, timeout=10):
        body = ('<?xml version="1.0" encoding="GBK"?>\n<Query><CmdType>DeviceInfo</CmdType>'
                f'<SN>3301</SN><DeviceID>{DEVICE_ID}</DeviceID></Query>').encode("gbk")
        call_id = f"pf-query-{random.randint(10000, 99999)}"
        req = (f"MESSAGE sip:{DEVICE_ID}@{PLATFORM_IP}:{DEVICE_PORT} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {PLATFORM_IP}:{PLATFORM_PORT};rport;branch=z9hG4bK-pf{random.randint(1000,9999)}\r\n"
               f"From: <sip:{SERVER_ID}@{PLATFORM_IP}:{PLATFORM_PORT}>;tag=pfq1\r\n"
               f"To: <sip:{DEVICE_ID}@{PLATFORM_IP}:{PLATFORM_PORT}>\r\n"
               f"Call-ID: {call_id}\r\nCSeq: 1 MESSAGE\r\n"
               f"Contact: <sip:{PLATFORM_IP}:{PLATFORM_PORT}>\r\nMax-Forwards: 70\r\n"
               f"Content-Type: Application/MANSCDP+xml\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
        self.send(req)
        got = self.wait_msg(lambda s, h, b: s.startswith("SIP/2.0 200")
                            and h.get("call-id") == call_id, timeout, "Query 的 200")
        if not got:
            return False
        got = self.wait_msg(lambda s, h, b: s.startswith("MESSAGE")
                            and "DeviceInfo" in b and "<Response>" in b, timeout,
                            "DeviceInfo Response")
        if not got:
            return False
        _, h, b, addr = got
        for needle in ("<CmdType>DeviceInfo</CmdType>", "<SN>3301</SN>",
                       "<Result>OK</Result>", DEVICE_ID, "GBIPC-SIM"):
            if needle not in b:
                self.fail(f"DeviceInfo Response 缺少: {needle}")
                return False
        self.send(build_response(200, "OK", h), addr)
        log("DeviceInfo 查询应答校验通过")
        return True

    # ---------- 阶段 4/5：INVITE + 媒体 + BYE ----------

    def _build_invite(self, use_tcp: bool, media_port: int):
        offer = (
            "v=0\r\n"
            f"o={DEVICE_ID} 0 0 IN IP4 {PLATFORM_IP}\r\n"
            "s=Play\r\n"
            f"c=IN IP4 {PLATFORM_IP}\r\n"
            "t=0 0\r\n"
            f"m=video {media_port} {'TCP/RTP/AVP' if use_tcp else 'RTP/AVP'} 96\r\n"
        )
        if use_tcp:
            offer += "a=setup:passive\r\na=connection:new\r\n"
        offer += f"a=recvonly\r\na=rtpmap:96 PS/90000\r\ny={SSRC_Y}\r\nf=v/////a///\r\n"
        call_id = f"pf-call-{random.randint(10000, 99999)}"
        branch = f"z9hG4bK-pf{random.randint(1000, 9999)}"
        req = (f"INVITE sip:{DEVICE_ID}@{PLATFORM_IP}:{DEVICE_PORT} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {PLATFORM_IP}:{PLATFORM_PORT};rport;branch={branch}\r\n"
               f"From: <sip:{SERVER_ID}@{PLATFORM_IP}:{PLATFORM_PORT}>;tag=pfc1\r\n"
               f"To: <sip:{DEVICE_ID}@{PLATFORM_IP}:{PLATFORM_PORT}>\r\n"
               f"Call-ID: {call_id}\r\nCSeq: 1 INVITE\r\n"
               f"Contact: <sip:{PLATFORM_IP}:{PLATFORM_PORT}>\r\nMax-Forwards: 70\r\n"
               f"Subject: {DEVICE_ID}:00,{SERVER_ID}:0\r\n"
               f"Content-Type: application/sdp\r\nContent-Length: {len(offer)}\r\n\r\n").encode() + offer.encode()
        return req, call_id

    def _check_answer_sdp(self, body: str, use_tcp: bool) -> bool:
        ok = True
        for needle in ("s=Play", "m=video", "a=sendonly", "a=rtpmap:96 PS/90000",
                       f"y={SSRC_Y}"):
            if needle not in body:
                self.fail(f"200 SDP 缺少: {needle}")
                ok = False
        proto = "TCP/RTP/AVP" if use_tcp else "RTP/AVP"
        if f"m=video" not in body or proto not in body.split("m=video")[1].split("\n")[0]:
            self.fail(f"200 SDP m 行传输协议不符（期望 {proto}）: {body.splitlines()}")
            ok = False
        if use_tcp and "a=setup:active" not in body:
            self.fail("200 SDP 缺少 a=setup:active")
            ok = False
        return ok

    def run_call(self, use_tcp: bool, send_bye: bool = True, timeout=20):
        """完成 INVITE -> 200 -> ACK -> 媒体帧校验。

        send_bye=True（默认，兼容旧测试）：平台侧主动发 BYE 并校验停流后返回；
        此时呼叫已 DISCONNECTED，pjsip 调用字典已清空。
        send_bye=False（复现「呼叫中停止」）：校验媒体帧后**不**发 BYE，
        把媒体 socket 留在 self 上（保持呼叫活跃）并返回 call_id，由调用方
        触发设备侧停止，再经 expect_bye() 应答设备 BYE。
        返回 call_id（str）表示成功，None 表示失败。
        """
        media_port = MEDIA_PORT_TCP if use_tcp else MEDIA_PORT_UDP
        tag = "TCP" if use_tcp else "UDP"

        # 媒体侧就绪
        if use_tcp:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((PLATFORM_IP, media_port))
            srv.listen(1)
            srv.settimeout(timeout)
        else:
            msock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            msock.bind((PLATFORM_IP, media_port))
            msock.settimeout(timeout)

        req, call_id = self._build_invite(use_tcp, media_port)
        self.send(req)

        # 等 200（100 Trying 可能在前面，wait_msg 按谓词过滤）
        got = self.wait_msg(lambda s, h, b: s.startswith("SIP/2.0 200")
                            and h.get("call-id") == call_id, timeout, f"{tag} INVITE 200")
        if not got:
            return False
        _, h, b, addr = got
        if not self._check_answer_sdp(b, use_tcp):
            return False
        to = h.get("to", "")
        contact = h.get("contact", f"<sip:{DEVICE_ID}@{PLATFORM_IP}:{DEVICE_PORT}>")
        ruri = contact.strip("<>")

        # ACK
        ack = (f"ACK {ruri} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {PLATFORM_IP}:{PLATFORM_PORT};rport;branch=z9hG4bK-pfack{random.randint(100,999)}\r\n"
               f"From: <sip:{SERVER_ID}@{PLATFORM_IP}:{PLATFORM_PORT}>;tag=pfc1\r\n"
               f"To: {to}\r\nCall-ID: {call_id}\r\nCSeq: 1 ACK\r\n"
               f"Max-Forwards: 70\r\nContent-Length: 0\r\n\r\n").encode()
        self.send(ack)

        # 媒体帧校验
        frames = []
        try:
            if use_tcp:
                conn, _ = srv.accept()
                conn.settimeout(timeout)
                buf = b""
                while len(frames) < 6:
                    while len(buf) < 2:
                        chunk = conn.recv(65535)
                        if not chunk:
                            raise ConnectionError("TCP 对端关闭")
                        buf += chunk
                    flen = int.from_bytes(buf[:2], "big")
                    while len(buf) < 2 + flen:
                        chunk = conn.recv(65535)
                        if not chunk:
                            raise ConnectionError("TCP 对端关闭")
                        buf += chunk
                    frames.append(buf[2:2 + flen])
                    buf = buf[2 + flen:]
            else:
                while len(frames) < 6:
                    pkt, _ = msock.recvfrom(65535)
                    frames.append(pkt)
        except (socket.timeout, ConnectionError) as e:
            self.fail(f"{tag} 媒体接收异常: {e}")
            return False

        if not self._check_rtp_frames(frames, tag):
            return False

        if not send_bye:
            # 复现「呼叫中停止」：保留媒体 socket、不回 BYE，让呼叫保持活跃。
            if use_tcp:
                self._call_conn = conn
                self._call_srv = srv
            else:
                self._call_msock = msock
            self._call_id_active = call_id
            log(f"{tag} 呼叫已建立并保持活跃（不回 BYE），call-id={call_id}")
            return call_id

        # BYE
        bye = (f"BYE {ruri} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {PLATFORM_IP}:{PLATFORM_PORT};rport;branch=z9hG4bK-pfbye{random.randint(100,999)}\r\n"
               f"From: <sip:{SERVER_ID}@{PLATFORM_IP}:{PLATFORM_PORT}>;tag=pfc1\r\n"
               f"To: {to}\r\nCall-ID: {call_id}\r\nCSeq: 2 BYE\r\n"
               f"Max-Forwards: 70\r\nContent-Length: 0\r\n\r\n").encode()
        self.send(bye)
        got = self.wait_msg(lambda s, h, b: s.startswith("SIP/2.0 200")
                            and h.get("call-id") == call_id and h.get("cseq", "").startswith("2"),
                            timeout, f"{tag} BYE 200")
        if not got:
            return False

        # 确认停流
        # 注意 TCP 语义：设备在 BYE 之前写入的数据会排在接收缓冲里，
        # 必须先排空这些残留数据，才能读到 FIN（EOF）。因此这里循环读到
        # EOF，并对整个排空过程设总时限；若时限内始终收不到 EOF，才判定
        # 设备未停流（仍在持续发送新数据）。
        #
        # 帧校验只读了 6 帧，而设备可能已把整段 PS 一次性写入缓冲，
        # 所以排空阶段允许读到数据，关键是最终必须收敛到 EOF。
        time.sleep(1.0)
        stopped = False
        try:
            if use_tcp:
                conn.settimeout(2)
                drain_deadline = time.monotonic() + 5.0
                while True:
                    try:
                        more = conn.recv(65535)
                    except socket.timeout:
                        # 2 秒内没有任何数据：没有再持续发送，视为已停流
                        stopped = True
                        break
                    if more == b"":
                        # 读到 EOF：对端已关闭连接（BYE 后正常行为）
                        stopped = True
                        break
                    if time.monotonic() > drain_deadline:
                        # 持续有新数据涌入，说明停流失败
                        stopped = False
                        break
                conn.close()
            else:
                # UDP 无连接语义：先排空 socket 缓冲中在 BYE 之前入队的
                # 数据报，直到出现一次 2 秒静默，才判定设备已停流。
                msock.settimeout(2)
                drain_deadline = time.monotonic() + 5.0
                while True:
                    try:
                        msock.recvfrom(65535)
                    except socket.timeout:
                        # 2 秒内没有新数据报：确认已停流
                        stopped = True
                        break
                    if time.monotonic() > drain_deadline:
                        # 仍有数据报持续涌入，说明停流失败
                        stopped = False
                        break
        except socket.timeout:
            stopped = True
        except OSError:
            # 对端 RST/FIN 导致的错误同样说明流已终止
            stopped = True
        if not stopped:
            self.fail(f"{tag} BYE 后媒体仍在发送")
            return False
        if use_tcp:
            srv.close()
        else:
            msock.close()
        log(f"{tag} 呼叫全流程校验通过")
        return True

    def expect_bye(self, call_id, timeout=15) -> bool:
        """应答设备侧发起的 BYE（call_id 相同），校验媒体已停流。

        用于「呼叫中停止」复现：run_call(send_bye=False) 后，设备被停止会
        主动发 BYE，本方法接收并回 200，再确认媒体连接关闭（TCP EOF / UDP 静默）。
        """
        got = self.wait_msg(lambda s, h, b: s.startswith("BYE")
                            and h.get("call-id") == call_id, timeout,
                            "设备侧 BYE")
        if not got:
            return False
        _, h, _, addr = got
        self.send(build_response(200, "OK", h), addr)
        log(f"已应答设备 BYE (call-id={call_id})")

        use_tcp = hasattr(self, "_call_conn")
        stopped = False
        try:
            if use_tcp:
                conn = self._call_conn
                conn.settimeout(2)
                drain_deadline = time.monotonic() + 5.0
                while True:
                    try:
                        more = conn.recv(65535)
                    except socket.timeout:
                        stopped = True
                        break
                    if more == b"":
                        stopped = True
                        break
                    if time.monotonic() > drain_deadline:
                        stopped = False
                        break
                conn.close()
                self._call_srv.close()
            else:
                msock = self._call_msock
                msock.settimeout(2)
                drain_deadline = time.monotonic() + 5.0
                while True:
                    try:
                        msock.recvfrom(65535)
                    except socket.timeout:
                        stopped = True
                        break
                    if time.monotonic() > drain_deadline:
                        stopped = False
                        break
                msock.close()
        except socket.timeout:
            stopped = True
        except OSError:
            stopped = True
        if not stopped:
            self.fail("设备 BYE 后媒体仍在发送")
            return False
        log("设备 BYE 后媒体已停流")
        return True

    def _check_rtp_frames(self, frames, tag) -> bool:
        expect_ssrc = int(SSRC_Y)
        for i, fr in enumerate(frames):
            if len(fr) < 12 or (fr[0] & 0xC0) != 0x80:
                self.fail(f"{tag} 帧{i} 不是 RTP 头: {fr[:8].hex()}")
                return False
            pt = fr[1] & 0x7F
            ssrc = int.from_bytes(fr[8:12], "big")
            if pt != 96:
                self.fail(f"{tag} 帧{i} PT={pt}，期望 96")
                return False
            if ssrc != expect_ssrc:
                self.fail(f"{tag} 帧{i} SSRC={ssrc}，期望 {expect_ssrc}（y 字段）")
                return False
        payload = frames[0][12:]
        if payload[:4] != b"\x00\x00\x01\xba":
            self.fail(f"{tag} 首帧负载不是 PS 起始码: {payload[:8].hex()}")
            return False
        log(f"{tag} 媒体帧校验通过（{len(frames)} 帧, PT=96, SSRC={expect_ssrc}, PS 起始码正确）")
        return True
