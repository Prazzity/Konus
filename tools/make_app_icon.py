"""One-off: build konus/assets/konus.ico out of assets/konus.svg.

Run with the project's interpreter: .venv/Scripts/python.exe tools/make_app_icon.py
An .ico is a small directory followed by the images; Windows accepts PNG payloads, so the
sizes are rendered from the SVG and stored as PNG.
"""

import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QBuffer, QByteArray, QRectF, Qt
from PyQt5.QtGui import QGuiApplication, QImage, QPainter
from PyQt5.QtSvg import QSvgRenderer

SIZES = (16, 24, 32, 48, 64, 128, 256)
SVG = "konus/assets/konus.svg"
OUT = "konus/assets/konus.ico"

app = QGuiApplication(sys.argv)
renderer = QSvgRenderer(SVG)
if not renderer.isValid():
    raise SystemExit(f"{SVG} did not load")

images = []
for size in SIZES:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    buf = QBuffer(QByteArray())
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    images.append((size, bytes(buf.data())))

entries, payloads, offset = [], [], 6 + 16 * len(images)
for size, data in images:
    side = 0 if size >= 256 else size          # 256 is written as 0
    entries.append(struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset))
    payloads.append(data)
    offset += len(data)

with open(OUT, "wb") as fh:
    fh.write(struct.pack("<HHH", 0, 1, len(images)))
    for e in entries:
        fh.write(e)
    for d in payloads:
        fh.write(d)

print(f"{OUT}: {len(images)} sizes ({', '.join(str(s) for s, _ in images)}), "
      f"{os.path.getsize(OUT)} bytes")
