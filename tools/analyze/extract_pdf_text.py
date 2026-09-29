#!/usr/bin/env python3
"""Extract text from GB/T 28181-2016 PDF, one line per page marker, for grep-based study."""
import os
import sys
from pypdf import PdfReader

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = os.path.join(_BASE, "docs", "GBT 28181-2016 公共安全视频监控联网系统信息传输、交换、控制技术要求.pdf")
dst = os.path.join(_BASE, "tools", "analyze", "gb28181_text.txt")

reader = PdfReader(src)
print(f"pages: {len(reader.pages)}")
out = []
for i, page in enumerate(reader.pages, 1):
    text = page.extract_text() or ""
    out.append(f"\n===== PAGE {i} =====\n{text}")
with open(dst, "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print(f"written: {dst}")
