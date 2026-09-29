#!/usr/bin/env python3
"""Inspect TCP payloads between IPC (172.220.20.246) and platform/relay during call."""
import os
from scapy.all import rdpcap, IP, TCP

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for path in [os.path.join(_BASE, "resource", "pcap", "IPC注册.pcap"), os.path.join(_BASE, "resource", "pcap", "IPC呼叫.pcap")]:
    print(f"\n########## {path} ##########")
    pkts = rdpcap(path)
    flows = {}
    t0 = float(pkts[0].time) if pkts else 0
    for pkt in pkts:
        if IP not in pkt or TCP not in pkt:
            continue
        ip, tcp = pkt[IP], pkt[TCP]
        payload = bytes(tcp.payload)
        if not payload:
            continue
        a, b = (ip.src, tcp.sport), (ip.dst, tcp.dport)
        # only flows involving IPC or ports in media range
        if "172.220.20.246" in (ip.src, ip.dst) and 5060 not in (tcp.sport, tcp.dport):
            key = tuple(sorted([a, b]))
            st = flows.setdefault(key, {"pkts": 0, "bytes": 0, "first": [], "win": [1e9, 0]})
            st["pkts"] += 1
            st["bytes"] += len(payload)
            st["win"][0] = min(st["win"][0], float(pkt.time) - t0)
            st["win"][1] = max(st["win"][1], float(pkt.time) - t0)
            if len(st["first"]) < 3:
                st["first"].append(payload[:48].hex(" "))
    for key, st in flows.items():
        print(f"TCP {key} pkts={st['pkts']} bytes={st['bytes']} t={st['win'][0]:.3f}~{st['win'][1]:.3f}s")
        for h in st["first"]:
            print(f"   head: {h}")
