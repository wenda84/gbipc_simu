"""精确二分：找出 onInstantMessageStatus 段错误触发条件。
用法: probe3.py <subclass_acc:0|1> <access_prm:0|1>
"""
import pjsua2 as pj, time, socket, threading, sys

SUBCLASS_ACC = sys.argv[1] == "1"
ACCESS_PRM = sys.argv[2] == "1"

def responder():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('127.0.0.1', 5080)); s.settimeout(8)
    try:
        data, addr = s.recvfrom(65535)
        hdrs = {}
        for l in data.decode('utf-8', errors='replace').split('\r\n')[1:]:
            if ':' in l:
                k, v = l.split(':', 1); hdrs[k.strip()] = v.strip()
        s.sendto(('SIP/2.0 200 OK\r\nVia: %s\r\nFrom: %s\r\nTo: %s;tag=t1\r\nCall-ID: %s\r\nCSeq: %s\r\nContent-Length: 0\r\n\r\n'
                  % (hdrs['Via'], hdrs['From'], hdrs['To'], hdrs['Call-ID'], hdrs['CSeq'])).encode(), addr)
        print('responder: 200 sent', flush=True)
    finally:
        s.close()

threading.Thread(target=responder, daemon=True).start()
ep = pj.Endpoint(); ep.libCreate()
cfg = pj.EpConfig(); cfg.logConfig.level = 2; cfg.logConfig.consoleLevel = 2
ep.libInit(cfg)
tp = pj.TransportConfig(); tp.port = 15060
ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp); ep.libStart()

if SUBCLASS_ACC:
    class Acc(pj.Account): pass
    acc = Acc()
else:
    acc = pj.Account()
acfg = pj.AccountConfig(); acfg.idUri = 'sip:34020000001320000001@127.0.0.1:5080'
acc.create(acfg)

class Bud(pj.Buddy):
    def onInstantMessageStatus(self, prm):
        print('cb entered', flush=True)
        if ACCESS_PRM:
            print('cb code:', prm.code, flush=True)

bud = Bud()
bcfg = pj.BuddyConfig(); bcfg.uri = 'sip:34020000002000000001@127.0.0.1:5080'; bcfg.subscribe = False
bud.create(acc, bcfg)
prm = pj.SendInstantMessageParam(); prm.contentType = 'Application/MANSCDP+xml'
prm.content = '<Notify><CmdType>Keepalive</CmdType><SN>1</SN></Notify>'
bud.sendInstantMessage(prm)
print('sent', flush=True)
time.sleep(3)
print('survived', flush=True)
ep.libDestroy()
print('done', flush=True)
