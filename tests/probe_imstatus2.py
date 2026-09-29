"""对照实验：定位 onInstantMessageStatus 段错误的确切条件。"""
import pjsua2 as pj
import time, socket, threading, sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "empty_override"

def responder():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 5080))
    s.settimeout(8)
    try:
        data, addr = s.recvfrom(65535)
        text = data.decode('utf-8', errors='replace')
        hdrs = {}
        for l in text.split('\r\n')[1:]:
            if ':' in l:
                k, v = l.split(':', 1); hdrs[k.strip()] = v.strip()
        resp = ('SIP/2.0 200 OK\r\nVia: %s\r\nFrom: %s\r\nTo: %s;tag=t1\r\nCall-ID: %s\r\nCSeq: %s\r\nContent-Length: 0\r\n\r\n'
                % (hdrs['Via'], hdrs['From'], hdrs['To'], hdrs['Call-ID'], hdrs['CSeq']))
        s.sendto(resp.encode(), addr)
        print('responder: 200 sent', flush=True)
    finally:
        s.close()

threading.Thread(target=responder, daemon=True).start()

ep = pj.Endpoint(); ep.libCreate()
cfg = pj.EpConfig(); cfg.logConfig.level = 1; cfg.logConfig.consoleLevel = 1
ep.libInit(cfg)
tp = pj.TransportConfig(); tp.port = 15060
ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp)
ep.libStart()

acc = pj.Account()
acfg = pj.AccountConfig(); acfg.idUri = 'sip:34020000001320000001@127.0.0.1:5080'
acc.create(acfg)

if MODE == "empty_override":
    class Bud(pj.Buddy):
        def onInstantMessageStatus(self, prm):
            print('callback entered (no prm access)', flush=True)
    bud = Bud()
elif MODE == "no_override":
    class Bud2(pj.Buddy):
        pass
    bud = Bud2()
else:  # plain
    bud = pj.Buddy()

bcfg = pj.BuddyConfig(); bcfg.uri = 'sip:34020000002000000001@127.0.0.1:5080'; bcfg.subscribe = False
bud.create(acc, bcfg)
prm = pj.SendInstantMessageParam(); prm.contentType = 'Application/MANSCDP+xml'
prm.content = '<Notify><CmdType>Keepalive</CmdType><SN>1</SN></Notify>'
bud.sendInstantMessage(prm)
print('sent', flush=True)
time.sleep(3)
print('survived, destroying', flush=True)
ep.libDestroy()
print('done', flush=True)
