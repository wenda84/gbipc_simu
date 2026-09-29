"""探测 pjsip 对 UDP/TCP 两种 SDP offer 的受理行为。
用法: probe8.py udp|tcp
"""
import pjsua2 as pj, time, socket, threading, sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "udp"
DEV_ID = "34020000001320000001"
SRV_ID = "34020000002000000001"

ep = pj.Endpoint(); ep.libCreate()
cfg = pj.EpConfig(); cfg.logConfig.level = 3; cfg.logConfig.consoleLevel = 3
ep.libInit(cfg)
print('video support:', hasattr(pj, 'PJMEDIA_HAS_VIDEO'), flush=True)
try:
    print('aud devs:', [d.name for d in ep.audDevManager().enumDev2()][:3], flush=True)
except Exception as e:
    print('aud dev enum err:', e, flush=True)
tp = pj.TransportConfig(); tp.port = 15060
ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp); ep.libStart()

got_call = threading.Event()

class Acc(pj.Account):
    def onIncomingCall(self, prm):
        print('onIncomingCall fired! callId=', prm.callId, flush=True)
        got_call.set()
        call = pj.Call(self, prm.callId)
        # 先 180 再 488 拒绝（探测用）
        prm2 = pj.CallOpParam(); prm2.statusCode = 488
        call.answer(prm2)

acc = Acc(); acfg = pj.AccountConfig(); acfg.idUri = f'sip:{DEV_ID}@127.0.0.1:5080'
acc.create(acfg)

def send_invite():
    time.sleep(0.5)
    proto = "TCP/RTP/AVP" if MODE == "tcp" else "RTP/AVP"
    setup = "a=setup:passive\r\na=connection:new\r\n" if MODE == "tcp" else ""
    offer = (f"v=0\r\no={DEV_ID} 0 0 IN IP4 127.0.0.1\r\ns=Play\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
             f"m=video 21006 {proto} 96\r\n{setup}a=recvonly\r\na=rtpmap:96 PS/90000\r\ny=0000000062\r\nf=v/////a///\r\n")
    req = (f"INVITE sip:{DEV_ID}@127.0.0.1:15060 SIP/2.0\r\n"
           f"Via: SIP/2.0/UDP 127.0.0.1:5080;rport;branch=z9hG4bK-probe8\r\n"
           f"From: <sip:{SRV_ID}@127.0.0.1:5080>;tag=p8\r\n"
           f"To: <sip:{DEV_ID}@127.0.0.1:5080>\r\n"
           f"Call-ID: probe8-{MODE}\r\nCSeq: 1 INVITE\r\n"
           f"Contact: <sip:127.0.0.1:5080>\r\nMax-Forwards: 70\r\n"
           f"Subject: {DEV_ID}:00,{SRV_ID}:0\r\n"
           f"Content-Type: application/sdp\r\nContent-Length: {len(offer)}\r\n\r\n").encode() + offer.encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(6)
    s.sendto(req, ('127.0.0.1', 15060))
    try:
        while True:
            data, _ = s.recvfrom(65535)
            line = data.decode('utf-8', errors='replace').split('\r\n')[0]
            print('UAC got:', line, flush=True)
            if not line.startswith('SIP/2.0 100'):
                break
    except socket.timeout:
        print('UAC: no response', flush=True)
    s.close()

threading.Thread(target=send_invite, daemon=True).start()
time.sleep(7)
ep.libDestroy()
print('done', flush=True)
