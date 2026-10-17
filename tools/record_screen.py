"""Photograph the screen as fast as it can be done, from a process of its own.

    .venv\\Scripts\\python.exe tools\\record_screen.py --out DIR --till STOPFILE

The application under measurement must not be the one taking the pictures: a screen grab in its
own thread costs a frame or two, and the thing being measured is exactly how evenly its frames
come. This process does nothing else — it grabs the screen into a bitmap with BitBlt, writes it
out as a JPEG and stamps the moment — so the player is left alone to animate at its own speed.

Frames go to `DIR` as frame-0001.jpg... and their moments to `times.json`, both of which is what
turns them back into something to watch (see tools/measure_anim.py --video).

The window area is the whole desktop by default, scaled to half so that a frame is a couple of
milliseconds rather than ten; `--region x,y,w,h` narrows it to what is being animated.
"""

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

SRCCOPY = 0x00CC0020
HALFTONE = 4
DIB_RGB_COLORS = 0
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def grabber(region, scale):
    """A function that returns one frame (x, y, w, h, bytes) of the desktop."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    x, y, w, h = region
    dw, dh = max(1, int(w * scale)), max(1, int(h * scale))

    screen = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(screen)
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = dw
    info.bmiHeader.biHeight = -dh            # top-down: the first row in memory is the top one
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    dib = gdi32.CreateDIBSection(screen, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits),
                                None, 0)
    gdi32.SelectObject(memdc, ctypes.c_void_p(dib))       # the DIB is the drawing surface
    size = dw * dh * 4
    buf = ctypes.create_string_buffer(size)

    def grab():
        if scale == 1.0:
            gdi32.BitBlt(memdc, 0, 0, dw, dh, screen, x, y, SRCCOPY)
        else:
            # StretchBlt with COLORONCOLOR: the cheapest copy there is. This is a record, not a
            # photograph to judge sharpness by, and a smoothed one costs more than the frame.
            gdi32.SetStretchBltMode(memdc, 3)
            gdi32.StretchBlt(memdc, 0, 0, dw, dh, screen, x, y, w, h, SRCCOPY)
        ctypes.memmove(buf, bits, size)
        return dw, dh, buf.raw

    return grab


#: The QGuiApplication this process runs under, kept for its lifetime (see main).
_APP = None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--till", required=True, type=Path, help="stop when this file appears")
    ap.add_argument("--region", default="", help="x,y,w,h — the whole desktop without it")
    ap.add_argument("--scale", type=float, default=0.5)
    ap.add_argument("--max-seconds", type=float, default=60.0)
    ap.add_argument("--quality", type=int, default=85)
    args = ap.parse_args()

    from PyQt5.QtGui import QGuiApplication, QImage
    # The application object is kept at module level on purpose: QImage and its JPEG writer
    # need one alive for the whole run, and a local one would be collected at the first
    # garbage pass, after which Qt refuses to make an image.
    global _APP
    _APP = QGuiApplication([sys.argv[0]])
    screen = QGuiApplication.primaryScreen()
    if args.region:
        x, y, w, h = (int(v) for v in args.region.split(","))
    else:
        g = screen.geometry()
        x, y, w, h = g.x(), g.y(), g.width(), g.height()
    grab = grabber((x, y, w, h), args.scale)
    dw, dh = int(w * args.scale), int(h * args.scale)

    args.out.mkdir(parents=True, exist_ok=True)
    if args.till.exists():
        args.till.unlink()

    times, n = [], 0
    t_start = time.perf_counter()
    print(f"recording {w}x{h} at {x},{y} → {dw}x{dh} into {args.out}, until {args.till}",
          flush=True)
    while not args.till.exists() and time.perf_counter() - t_start < args.max_seconds:
        t = time.perf_counter()
        gw, gh, raw = grab()
        img = QImage(raw, gw, gh, gw * 4, QImage.Format_RGB32).copy()
        n += 1
        img.save(str(args.out / f"frame-{n:04d}.jpg"), "JPEG", args.quality)
        times.append([round((t - t_start) * 1000, 2), round(time.time() * 1000)])
    (args.out / "times.json").write_text(json.dumps(
        {"region": [x, y, w, h], "scale": args.scale, "times_ms": times}), encoding="utf-8")
    span = (times[-1][0] - times[0][0]) if len(times) > 1 else 0
    print(f"{n} frames in {span:.0f} ms ({1000 * (n - 1) / span if span else 0:.1f} fps) → "
          f"{args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
