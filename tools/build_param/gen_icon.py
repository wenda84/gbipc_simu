"""一键从 resource/ui/ipc.svg 生成多尺寸 ipc.ico 与 256px ipc.png。

用 QtSvg 矢量渲染（各尺寸独立渲染，保证小图标清晰），再用 Pillow
合成 Windows 多分辨率 .ico。offscreen 平台避免需要显示器。
"""
import io
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QBuffer, QByteArray
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI_RES = os.path.join(ROOT, "resource", "ui")
SVG = os.path.join(UI_RES, "ipc.svg")
OUT_ICO = os.path.join(UI_RES, "ipc.ico")
OUT_PNG = os.path.join(UI_RES, "ipc.png")

SIZES = [16, 24, 32, 48, 64, 128, 256]

app = QGuiApplication(sys.argv)
renderer = QSvgRenderer(SVG)
if not renderer.isValid():
    raise SystemExit("SVG 无效，无法渲染")

frames = []
for s in SIZES:
    img = QImage(s, s, QImage.Format_RGBA8888)
    img.fill(0)  # 全透明
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    renderer.render(painter, QRect(0, 0, s, s))
    painter.end()
    qba = QByteArray()
    qbuf = QBuffer(qba)
    qbuf.open(QBuffer.WriteOnly)
    img.save(qbuf, "PNG")
    qbuf.close()
    frames.append(Image.open(io.BytesIO(bytes(qba))).convert("RGBA"))

# 多尺寸 ICO：标准 ICO 容器，每个尺寸以 PNG 编码为独立条目
# （Pillow 12 的 ICO 插件在本环境对 sizes/append_images 不生效，故手工打包；
#  PNG 编码 ICO 自 Windows Vista 起原生支持，Win10/11 任务栏/桌面均正常）
import struct

_pngs = []
for _f in frames:
    _b = io.BytesIO()
    _f.save(_b, "PNG")
    _pngs.append(_b.getvalue())

_count = len(_pngs)
_header = struct.pack("<HHH", 0, 1, _count)
_entries = b""
_data = b""
_offset = 6 + 16 * _count
for _f, _png in zip(frames, _pngs):
    _w, _h = _f.size
    _bw = _w if _w < 256 else 0
    _bh = _h if _h < 256 else 0
    _entries += struct.pack("<BBBBHHII", _bw, _bh, 0, 0, 1, 32, len(_png), _offset + len(_data))
    _data += _png
with open(OUT_ICO, "wb") as _fo:
    _fo.write(_header + _entries + _data)

# 256px PNG 备用（源码态窗口图标 / 文档）
frames[-1].save(OUT_PNG, format="PNG")

print("ICO 嵌入尺寸:", [f.size for f in frames])
print("OK ico=%d bytes  png=%d bytes" % (os.path.getsize(OUT_ICO), os.path.getsize(OUT_PNG)))
