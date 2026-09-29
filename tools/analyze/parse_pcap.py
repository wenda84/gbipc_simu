#!/usr/bin/env python3
"""Parse GB28181 pcaps: dump SIP messages with timing, summarize RTP flows."""
import sys
from scapy.all import rdpcap, IP, UDP, Raw

SIP_PORTS = {5060, 5061, 5080, 5070}


def parse_pcap(path):
    print(f"\n########## {path} ##########")
    pkts = rdpcap(path)
    print(f"total packets: {len(pkts)}")
    sip_count = 0
    rtp_stats = {}  # (src,sport,dst,dport) -> [count, bytes, ssrc, pt_set, first_ts, last_ts, markers]
    t0 = None
    for pkt in pkts:
        if IP not in pkt or UDP not in pkt:
            continue
        ts = float(pkt.time)
        if t0 is None:
            t0 = ts
        rel = ts - t0
        ip, udp = pkt[IP], pkt[UDP]
        payload = bytes(pkt[Raw].load) if Raw in pkt else b""
        if not payload:
            continue
        key = (ip.src, udp.sport, ip.dst, udp.dport)
        if udp.dport in SIP_PORTS or udp.sport in SIP_PORTS:
            sip_count += 1
            text = payload.decode("utf-8", errors="replace").strip()
            text = "".join(ch if (ch.isprintable() or ch in "\r\n\t") else "." for ch in text)
            print(f"\n----- #{sip_count} t=+{rel:.3f}s {ip.src}:{udp.sport} -> {ip.dst}:{udp.dport} len={len(payload)}")
            print(text)
        else:
            st = rtp_stats.setdefault(key, [0, 0, None, set(), rel, rel, 0])
            st[0] += 1
            st[1] += len(payload)
            st[4] = min(st[4], rel)
            st[5] = max(st[5], rel)
            if len(payload) >= 12 and (payload[0] & 0xC0) == 0x80:
                pt = payload[1] & 0x7F
                marker = (payload[1] >> 7) & 1
                ssrc = int.from_bytes(payload[8:12], "big")
                st[2] = ssrc
                st[3].add((pt, marker))
    print(f"\n===== summary: {sip_count} SIP messages =====")
    for key, st in rtp_stats.items():
        pts = sorted(st[3])
        print(f"RTP {key[0]}:{key[1]} -> {key[2]}:{key[3]}  pkts={st[0]} bytes={st[1]} "
              f"window={st[4]:.3f}s~{st[5]:.3f}s ssrc={st[2]} (pt,marker)={pts}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        parse_pcap(p)
