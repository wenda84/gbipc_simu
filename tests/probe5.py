"""验证 sendRequest 路径（子类化 Account）是否规避 Buddy 崩溃。"""
import pjsua2 as pj, time, socket, threading

def responder():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('127.0.0.1', 5080)); s.settimeout(8)
    try:
        data, addr = s.recvfrom(65535)
        text = data.decode('utf-8', errors='replace')
        hdrs = {}
        for l in text.split('\r\n')[1:]:
            if ':' in l:
                k, v = l.split(':', 1); hdrs[k.strip()] = v.strip()
        print('responder got request line:', text.split('\r\n')[0], flush=True)
        print('responder got body head:', text.split('\r\n\r\n')[1][:80], flush=True)
        s.sendto(('SIP/2.0 200 OK\r\nVia: %s\r\nFrom: %s\r\nTo: %s;tag=t1\r\nCall-ID: %s\r\nCSeq: %s\r\nContent-Length: 0\r\n\r\n'
                  % (hdrs['Via'], hdrs['From'], hdrs['To'], hdrs['Call-ID'], hdrs['CSeq'])).encode(), addr)
        print('responder: 200 sent', flush=True)
    finally:
        s.close()

threading.Thread(target=responder, daemon=True).start()
ep = pj.Endpoint(); ep.libCreate()
cfg = pj.EpConfig(); cfg.logConfig.level = 3; cfg.logConfig.consoleLevel = 3
ep.libInit(cfg)
tp = pj.TransportConfig(); tp.port = 15060
ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp); ep.libStart()

class Acc(pj.Account):
    def onSendRequest(self, prm):
        print('onSendRequest entered', flush=True)
        e = prm.e
        print('  error status:', e.status if hasattr(e, 'status') else e, flush=True)
        try:
            print('  error reason:', e.reason, flush=True)
        except Exception as ex:
            print('  reason access err:', ex, flush=True)

acc = Acc(); acfg = pj.AccountConfig(); acfg.idUri = 'sip:34020000001320000001@127.0.0.1:5080'
acc.create(acfg)

prm = pj.SendRequestParam()
prm.method = 'MESSAGE'
prm.txOption.targetUri = 'sip:34020000002000000001@127.0.0.1:5080'
prm.txOption.contentType = 'Application/MANSCDP+xml'
prm.txOption.msgBody = '<?xml version="1.0" encoding="GB2312"?>\n<Notify><CmdType>Keepalive</CmdType><SN>1</SN><DeviceID>34020000001320000001</DeviceID><Status>OK</Status></Notify>'
acc.sendRequest(prm)
print('sendRequest called', flush=True)
time.sleep(3)
print('survived', flush=True)
ep.libDestroy()
print('done', flush=True)
