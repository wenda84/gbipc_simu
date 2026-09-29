#!/usr/bin/env python3
"""Scan reassembled TCP stream for RTP sync and validate framing hypothesis."""
import os
from scapy.all import rdpcap, IP, TCP

path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "resource", "pcap", "IPC呼叫.pcap")
pkts = rdpcap(path)
segs = {}
for pkt in pkts:
    if IP not in pkt or TCP not in pkt:
        continue
    ip, tcp = pkt[IP], pkt[TCP]
    payload = bytes(tcp.payload)
    if payload and ip.src == "172.220.20.246" and tcp.sport == 15060 and tcp.dport == 21344:
        segs.setdefault(tcp.seq, payload)
data = b"".join(segs[k] for k in sorted(segs))
print(f"reassembled bytes: {len(data)}")

# find candidate RTP headers: 0x80, PT byte in {0x60, 0xE0}, ssrc=1, seq monotonic
cands = []
for i in range(len(data) - 14):
    if data[i] != 0x80 or data[i + 1] not in (0x60, 0xE0):
        continue
    ssrc = int.from_bytes(data[i + 8:i + 12], "big")
    if ssrc == 1:
        cands.append(i)
print(f"candidate RTP headers: {len(cands)}")
starts = cands[:200]

# validate seq monotonicity
seqs = [int.from_bytes(data[s + 2:s + 4], "big") for s in starts]
mono = all(b > a or (a > 60000 and b < 5000) for a, b in zip(seqs, seqs[1:]))
print(f"seq monotonic (first 200): {mono}, first seqs: {seqs[:10]}")

# framing check: distance between consecutive RTP starts vs 2-byte BE prefix
gaps, framed_ok, raw_ok = [], 0, 0
for a, b in zip(starts, starts[1:]):
    gap = b - a
    gaps.append(gap)
    if a >= 2 and int.from_bytes(data[a - 2:a], "big") == gap - 2:
        framed_ok += 1
print(f"gap stats: min={min(gaps)} max={max(gaps)}")
print(f"RFC4571-framed matches (2B BE prefix == gap-2): {framed_ok}/{len(gaps)}")

# look at bytes immediately before first 5 RTP headers
for s in starts[:5]:
    pre = data[max(0, s - 4):s].hex(" ")
    print(f"rtp@{s}: pre4={pre} head={data[s:s+16].hex(' ')}")

# check whether payload right after an RTP header starts with PS start code
ps_hits = 0
for s in starts[:50]:
    if data[s + 12:s + 16] == b"\x00\x00\x01\xba":
        ps_hits += 1
print(f"payload starts with PS start code in first 50 frames: {ps_hits}")

# find first PS start code occurrence and bytes around it
idx = data.find(b"\x00\x00\x01\xba")
print(f"first PS start code at {idx}: context={data[idx-20:idx+8].hex(' ')}")
