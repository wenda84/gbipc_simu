#!/usr/bin/env python3
"""Reassemble the IPC->platform TCP media stream in IPC呼叫.pcap and inspect framing."""
import os
from scapy.all import rdpcap, IP, TCP

path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "resource", "pcap", "IPC呼叫.pcap")
pkts = rdpcap(path)

# IPC 172.220.20.246:15060 -> 172.220.0.178:21344 (setup:active side sends media)
segs = {}
for pkt in pkts:
    if IP not in pkt or TCP not in pkt:
        continue
    ip, tcp = pkt[IP], pkt[TCP]
    payload = bytes(tcp.payload)
    if not payload:
        continue
    if ip.src == "172.220.20.246" and tcp.sport == 15060 and tcp.dport == 21344:
        seq = tcp.seq
        if seq not in segs:  # dedup retransmissions
            segs[seq] = payload

data = b"".join(segs[k] for k in sorted(segs))
print(f"reassembled bytes: {len(data)}")
print("first 128 bytes:", data[:128].hex(" "))

# Hypothesis A: RFC4571 framing = 2-byte BE length + RTP (starts 0x80)
print("\n--- hypothesis A: 2-byte BE length prefix + RTP ---")
off, frames, ok = 0, 0, True
for _ in range(10):
    if off + 2 > len(data):
        break
    n = int.from_bytes(data[off:off+2], "big")
    b = data[off+2]
    print(f"frame@{off}: len={n} first_byte=0x{b:02x}")
    if n <= 0 or n > 65535 or (b & 0xC0) != 0x80:
        ok = False
        break
    off += 2 + n
    frames += 1
print("hypothesis A plausible:", ok)

# Hypothesis B: raw RTP without framing
print("\n--- hypothesis B: raw RTP stream ---")
print("first byte: 0x%02x (0x80 expected for RTP v2)" % data[0])

# If A: dump first few RTP headers (pt, seq, ts, ssrc)
if ok:
    off = 0
    for i in range(5):
        n = int.from_bytes(data[off:off+2], "big")
        rtp = data[off+2:off+2+n]
        pt = rtp[1] & 0x7F
        marker = (rtp[1] >> 7) & 1
        seq = int.from_bytes(rtp[2:4], "big")
        ts = int.from_bytes(rtp[4:8], "big")
        ssrc = int.from_bytes(rtp[8:12], "big")
        payload_head = rtp[12:16].hex(" ")
        print(f"RTP#{i}: len={n} pt={pt} marker={marker} seq={seq} ts={ts} ssrc={ssrc} payload_head={payload_head}")
        off += 2 + n
