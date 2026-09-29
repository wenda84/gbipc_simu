"""规避验证：子类化 Account + Buddy 不覆写 onInstantMessageStatus 是否仍崩溃。"""
import pjsua2 as pj, time, socket, threading, sys

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

print('Endpoint util methods:', [m for m in dir(pj.Endpoint) if 'util' in m.lower() or 'send' in m.lower()], flush=True)
print('Account send-ish methods:', [m for m in dir(pj.Account) if 'send' in m.lower() or 'message' in m.lower() or 'im' in m.lower()], flush=True)

threading.Thread(target=responder, daemon=True).start()
ep = pj.Endpoint(); ep.libCreate()
cfg = pj.EpConfig(); cfg.logConfig.level = 2; cfg.logConfig.consoleLevel = 2
ep.libInit(cfg)
tp = pj.TransportConfig(); tp.port = 15060
ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp); ep.libStart()

class Acc(pj.Account): pass
acc = Acc(); acfg = pj.AccountConfig(); acfg.idUri = 'sip:34020000001320000001@127.0.0.1:5080'
acc.create(acfg)

class Bud(pj.Buddy):  # 不覆写 onInstantMessageStatus
    pass
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
