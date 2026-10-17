"""How much a window resize costs on this machine, with nothing in the window at all.

    .venv\\Scripts\\python.exe tools\\anim_probe.py
    .venv\\Scripts\\python.exe tools\\anim_probe.py --variant nowredraw --runs 5
    .venv\\Scripts\\python.exe tools\\anim_probe.py --child native

The main window's own animation spends about 8 ms of its ~35 ms frame inside the application;
the measurement in tools/measure_anim.py cannot see the rest, because it happens under
SetWindowPos and inside the compositor. This probe takes the application out of the question: a
frameless window, one colour, no video, no children (or one native child, or one ordinary one),
animated exactly the way the player animates its window. What is left of the frame is the
system's price for moving and sizing a window, and that is the floor any implementation is up
against.

    ctrl        the bare window
    child       a Qt child widget filling it (something to be laid out and painted)
    native      a native child widget filling it (as the video output is)
    nowredraw   SWP_NOREDRAW: the window is moved and sized, nothing is repainted, nothing is
                invalidated — what the compositor does with the window meanwhile is measured
    nocopybits  SWP_NOCOPYBITS: the client area is discarded instead of blitted
    nopaint     the window is not repainted by the application at all during the animation
    sizeonly    only the size is animated, the window does not move
    xonly       neither: the animation moves the window's left edge only — the case where
                Windows' internal blit is visible (see the note in measure_anim.py)
"""

import argparse
import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt5.QtCore import (QAbstractNativeEventFilter, QEasingCurve, QPointF, QPropertyAnimation,  # noqa: E402
                          QRect, Qt)
from PyQt5.QtGui import QColor, QGuiApplication, QPainter                                          # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget                                                  # noqa: E402

WM_WINDOWPOSCHANGING = 0x0046
SWP_NOCOPYBITS = 0x0100
SWP_NOREDRAW = 0x0008
MSG_ID_AT, MSG_LPARAM_AT, WINDOWPOS_FLAGS_AT = 8, 24, 32


class FlagFilter(QAbstractNativeEventFilter):
    def __init__(self, hwnd, add):
        super().__init__()
        self.hwnd = hwnd
        self.add = add
        self.on = False

    def nativeEventFilter(self, eventtype, message):
        if self.on and eventtype == b"windows_generic_MSG":
            try:
                ptr = int(message)
                if ctypes.c_uint32.from_address(ptr + MSG_ID_AT).value == WM_WINDOWPOSCHANGING:
                    if ctypes.c_void_p.from_address(ptr).value == self.hwnd:
                        lparam = ctypes.c_void_p.from_address(ptr + MSG_LPARAM_AT).value
                        at = lparam + WINDOWPOS_FLAGS_AT
                        ctypes.c_uint.from_address(at).value |= self.add
            except (TypeError, ValueError, OSError):
                pass
        return False, 0


class Bench(QWidget):
    """A frameless window that paints one colour — or a child's worth of nothing."""

    CURVE = QEasingCurve()
    CURVE.setType(QEasingCurve.BezierSpline)
    CURVE.addCubicBezierSegment(QPointF(0.35, 0.0), QPointF(0.65, 1.0), QPointF(1.0, 1.0))

    def __init__(self, normal, maximized, child=None):
        super().__init__(None, Qt.FramelessWindowHint)
        self.normal, self.maximized = normal, maximized
        self.setGeometry(normal)
        self.paint_self = True
        if child is not None:
            c = QWidget(self)
            c.setAttribute(Qt.WA_NativeWindow) if child == "native" else None
            c.setGeometry(self.rect())
            c.setAutoFillBackground(True) if child == "ordinary" else None
            self.child = c
            if child == "native":
                self.paint_self = False      # a native child paints itself; Qt must not
        self.steps = []

    def paintEvent(self, e):
        if not self.paint_self:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1b1b1b"))
        pen = p.pen()
        pen.setColor(QColor("#404040"))
        p.setPen(pen)
        for x in range(0, self.width(), 100):
            p.drawLine(x, 0, x, self.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "child", None) is not None:
            self.child.setGeometry(self.rect())
        self.steps.append(time.perf_counter())


def run(args):
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication([sys.argv[0]])
    avail = QGuiApplication.primaryScreen().availableGeometry()
    normal = QRect(avail.x() + 120, avail.y() + 90, 1280, 760)
    maximized = QRect(avail)
    if args.size:
        maximized = QRect(avail.x(), avail.y(), args.size[0], args.size[1])
    win = Bench(normal, maximized, None if args.child == "none" else args.child)
    win.show()
    app.processEvents()
    time.sleep(0.4)
    app.processEvents()

    filt = None
    if args.variant in ("nowredraw", "nocopybits"):
        filt = FlagFilter(int(win.winId()),
                          SWP_NOREDRAW if args.variant == "nowredraw" else SWP_NOCOPYBITS)
        app.installNativeEventFilter(filt)
    def animate(target, ms):
        a = QPropertyAnimation(win, b"geometry", win)
        a.setDuration(ms)
        start = QRect(win.geometry())
        if args.variant == "sizeonly":
            start = QRect(start.x(), start.y(), start.width(), start.height())
            target = QRect(start.x(), start.y(), target.width(), target.height())
        if args.variant == "xonly":
            target = QRect(target.x(), start.y(), start.width(), target.height())
        a.setStartValue(start)
        a.setEndValue(target)
        a.setEasingCurve(Bench.CURVE)
        if filt is not None:
            filt.on = True
        win.steps = []
        a.start()
        t0 = time.perf_counter()
        while a.state() == QPropertyAnimation.Running:
            app.processEvents()
            time.sleep(0.001)
        if filt is not None:
            filt.on = False
        return win.steps, t0

    all_gaps = []
    for i in range(args.runs):
        for tag, target in (("max", maximized), ("restore", normal)):
            print(f"  waiting before {tag}", end="", flush=True)
            app.processEvents()
            time.sleep(0.7)
            steps, _t0 = animate(target, args.ms)
            gaps = [round((b - a) * 1000, 1) for a, b in zip(steps, steps[1:])]
            all_gaps += gaps
            print(f"\r  {tag:7} {len(gaps)} gaps: {gaps}  avg "
                  f"{round(sum(gaps) / max(1, len(gaps)), 1)} ms")
    if all_gaps:
        all_gaps.sort()
        print(f"  overall: {len(all_gaps)} gaps, median {all_gaps[len(all_gaps) // 2]} ms, "
              f"worst {all_gaps[-1]} ms, avg {round(sum(all_gaps) / len(all_gaps), 1)} ms")
    win.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--variant", default="ctrl",
                    choices=("ctrl", "nowredraw", "nocopybits", "nopaint", "sizeonly", "xonly"))
    ap.add_argument("--child", default="none", choices=("none", "ordinary", "native"))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--ms", type=int, default=180)
    ap.add_argument("--size", type=int, nargs=2, default=None, help="the target size, px")
    args = ap.parse_args()
    print(f"frameless window {args.variant}, child={args.child}, {args.ms} ms animation")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
