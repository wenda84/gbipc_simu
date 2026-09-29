"""验证 GIL 规避方案：threadCnt=0 + Python 线程轮询 libHandleEvents。"""
import pjsua2 as pj, time, socket, threading, hashlib

REALM = "3402000000"; NONCE = "aabbccddeeff00112233445566778899"; PWD = "test1234"

def responder():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('127.0.0.1', 5080)); s.settimeout(10)
    try:
        for round_ in range(2):
            data, addr = s.recvfrom(65535)
            text = data.decode('utf-8', errors='replace')
            hdrs = {}
            for l in text.split('\r\n')[1:]:
                if ':' in l:
                    k, v = l.split(':', 1); hdrs[k.strip().lower()] = v.strip()
            def resp(code, reason, extra=""):
                return ('SIP/2.0 %d %s\r\nVia: %s\r\nFrom: %s\r\nTo: %s;tag=t%d\r\nCall-ID: %s\r\nCSeq: %s\r\n%sContent-Length: 0\r\n\r\n'
                        % (code, reason, hdrs['via'], hdrs['from'], hdrs['to'], round_, hdrs['call-id'], hdrs['cseq'], extra)).encode()
            if 'authorization' not in hdrs:
                s.sendto(resp(401, 'Unauthorized', f'WWW-Authenticate: Digest realm="{REALM}",nonce="{NONCE}",qop="auth,auth-int"\r\n'), addr)
                print('responder: 401 sent', flush=True)
            else:
                s.sendto(resp(200, 'OK', 'Expires: 3600\r\n'), addr)
                print('responder: 200 sent', flush=True)
                break
    finally:
        s.close()

threading.Thread(target=responder, daemon=True).start()
ep = pj.Endpoint(); ep.libCreate()
cfg = pj.EpConfig()
cfg.uaConfig.threadCnt = 0          # 关键：不创建 pjsip 工作线程
cfg.logConfig.level = 3; cfg.logConfig.consoleLevel = 3
ep.libInit(cfg)
tp = pj.TransportConfig(); tp.port = 15060
ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp); ep.libStart()

class Acc(pj.Account):
    def onRegState(self, prm):
        print('onRegState entered, code:', prm.code, flush=True)
        print('onRegState isActive:', self.getInfo().regIsActive, flush=True)

acc = Acc()
acfg = pj.AccountConfig()
acfg.idUri = 'sip:34020000001320000001@127.0.0.1:5080'
acfg.regConfig.registrarUri = 'sip:34020000002000000001@127.0.0.1:5080'
acfg.regConfig.timeoutSec = 3600
acfg.regConfig.registerOnAdd = False
acfg.sipConfig.authCreds.append(pj.AuthCredInfo('digest', '*', '34020000001320000001', 0, PWD))
acc.create(acfg)

# 泵线程：Python 线程（持有 GIL）轮询处理 pjsip 事件
pump_stop = threading.Event()
def pump():
    while not pump_stop.is_set():
        ep.libHandleEvents(20)
pt = threading.Thread(target=pump, daemon=True)
pt.start()

acc.setRegistration(True)
print('registration initiated', flush=True)
time.sleep(4)
print('survived', flush=True)
pump_stop.set(); pt.join(timeout=2)
ep.libDestroy()
print('done', flush=True)
