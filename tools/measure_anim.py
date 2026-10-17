"""Measure the window's maximize/restore animation, frame by frame.

    .venv\\Scripts\\python.exe tools\\measure_anim.py --mode video --clip --runs 2 --ticks
    .venv\\Scripts\\python.exe tools\\measure_anim.py --mode library --runs 2 --pattern --record
    .venv\\Scripts\\python.exe tools\\measure_anim.py --compare ctrl,fixed16 --mode video --clip
    .venv\\Scripts\\python.exe tools\\measure_anim.py --scan %TEMP%\\konus-anim-lab\\video-ctrl-...

The application really runs, in a scratch data folder, and is asked to maximize and restore a
few times; `--ticks` times every frame of the animation at its source, `--events` says which
widgets the thread was painting, `--profile` wraps the application's own resize path, and
`--native-trace` writes down the window messages those frames were made of. Together they say
where a frame's milliseconds go and how evenly the frames come.

`--record` photographs the screen from a process of its own (tools/record_screen.py) while the
player animates, because a screen grab taken in the player's own thread costs a frame and would
be the stutter it is measuring. `--scan` reads such a session back and measures where the film's
picture really was in every frame.

What these measurements said, on this machine, in the order they were made:

* the bare window — no page, no video, one native child — animates at a steady 16 ms a frame
  (tools/anim_probe.py), so the system is not the limit;
* the application's own frames cost 8-22 ms of work on top of that, and QPropertyAnimation asks
  for the next frame only *after* the last one is drawn: the frames came at 19, 25, 28, 41, 44 ms
  — the geometry was right and the rate was ragged, which is what the eye reads as a jerk;
* the film's picture does *not* wobble: measured frame by frame it grows and shrinks in one
  direction only. The picture's scale follows the widget with a small lag, and that lag looks
  like movement only because the frames around it were ragged;
* the two costly parts of a frame were the strip-painted-by-hand (6-8 ms) followed by the
  system's own WM_PAINT for the same pixels, and the video widget's rounded mask (1.2 ms, and a
  full redraw of the child each time).

The variants change one thing each, from outside the application, so several can be compared in
one sitting (`--compare`). Two of them are in the application now (see konus.window):

    ctrl         nothing — the application as it is
    fixed16/24/33  frames at a fixed cadence instead of a free-running 16 ms timer: even spacing
                 even when a frame costs more than its step, which is what the application does
    nopaint      no strip painted by hand at all (the class brush covers it meanwhile)
    nowredraw    SWP_NOREDRAW on the window's position changes: the system invalidates nothing
                 and asks nothing of the application
    nocopybits   SWP_NOCOPYBITS: the client area is discarded rather than blitted — Qt 6's fix
                 for the back-and-forth (QTBUG-97774), which made this application *slower* and
                 no steadier, because it forces a full repaint of the page every frame
    nomask       the video's rounded mask is not re-applied on every frame
    noreroute    the floating windows are not put in place on every frame
    freezelayout the page keeps its size while the window grows around it
    freezeframe  the video widget keeps its size while the window grows around it — the picture
                 stops moving entirely, at the price of the video not growing until the end
"""

import argparse
import bisect
import ctypes
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

# The lab runs out of a scratch data folder: measuring must not touch the settings, the
# library or the cache of whoever runs it (see konus.portable).
LAB = Path(tempfile.gettempdir()) / "konus-anim-lab"
os.environ["KONUS_DATA_DIR"] = str(LAB / "config")

from PyQt5.QtCore import (QAbstractNativeEventFilter, QElapsedTimer, QEvent, QEventLoop,       # noqa: E402
                          QObject, QRect, QTimer, Qt)
from PyQt5.QtGui import QColor, QGuiApplication, QPainter, QPen                       # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget                                # noqa: E402

# Windows: the messages and flags this bench is about.
WM_WINDOWPOSCHANGING = 0x0046
SWP_NOCOPYBITS = 0x0100
SWP_NOREDRAW = 0x0008
# A MESSAGE carries hwnd at 0 and the message id at 8; its lParam (at 24) points at a WINDOWPOS,
# whose flags are the last field: two pointers and four ints into it.
MSG_ID_AT, MSG_LPARAM_AT = 8, 24
WINDOWPOS_FLAGS_AT = 32

VARIANT_CHOICES = ("ctrl", "nocopybits", "smartbits", "noreroute", "nomask", "freezeframe",
                   "nopaint", "nowredraw", "freezelayout", "fixed16", "fixed24", "fixed33")


# ---------------------------------------------------------------- the variants (no app code)
class NoCopyBits(QAbstractNativeEventFilter):
    """Set SWP_NOCOPYBITS on the window position changes of this app's windows.

    Qt hands a native event to the filters of the application *before* its own windows plugin
    sees it, and the flag lives in the WINDOWPOS struct the message carries — so this is Qt 6's
    fix applied from outside, with no rebuild. `only` narrows it to the window being animated.
    """

    def __init__(self, only=None, flag=True, extra=0, extra_while=False):
        super().__init__()
        self.only = only
        self.flag = flag           # False: count them without touching the flags
        self.extra = extra         # a flag to add: SWP_NOREDRAW, say
        self.extra_while = extra_while   # add it only while the geometry animation runs
        self.active = lambda: True
        self.seen = 0
        self.moves = 0
        self.last = None           # the last WINDOWPOS seen: x, y, cx, cy, flags

    def nativeEventFilter(self, eventtype, message):
        if eventtype == b"windows_generic_MSG":
            try:
                ptr = int(message)
                if ctypes.c_uint32.from_address(ptr + MSG_ID_AT).value == WM_WINDOWPOSCHANGING:
                    hwnd = ctypes.c_void_p.from_address(ptr).value
                    if self.only is None or hwnd == self.only:
                        lparam = ctypes.c_void_p.from_address(ptr + MSG_LPARAM_AT).value
                        at = lparam + WINDOWPOS_FLAGS_AT
                        flags = ctypes.c_uint.from_address(at).value
                        self.seen += 1
                        self.last = {"x": ctypes.c_int.from_address(lparam + 16).value,
                                     "y": ctypes.c_int.from_address(lparam + 20).value,
                                     "cx": ctypes.c_int.from_address(lparam + 24).value,
                                     "cy": ctypes.c_int.from_address(lparam + 28).value,
                                     "flags": flags}
                        add = SWP_NOCOPYBITS if self.flag else 0
                        if self.extra and (not self.extra_while or self.active()):
                            add |= self.extra
                        if add:
                            ctypes.c_uint.from_address(at).value = flags | add
                            self.moves += 1
            except (TypeError, ValueError, OSError):
                pass
        return False, 0


def apply_variants(app, win, spec):
    """Arrange the app so that every variant in `spec` (`a+b+c`) holds. Returns (desc, undo)."""
    descs, undos = [], []
    for name in spec.split("+"):
        name = name.strip()
        if name not in VARIANT_CHOICES:
            raise SystemExit(f"unknown variant {name!r} (choose from {', '.join(VARIANT_CHOICES)}, "
                             "joined with + to combine)")
        d, u = apply_variant(app, win, name)
        descs.append(d)
        undos.append(u)
    return "; ".join(descs), lambda: [u() for u in reversed(undos)]


def apply_variant(app, win, name):
    """Arrange the app so that `name` holds. Returns (description, undo)."""
    from konus.player import VideoFrame
    from konus.window import MainWindow

    if name == "ctrl":
        return "no change", lambda: None

    if name in ("nocopybits", "smartbits"):
        # Only the main window is touched: a flag on the floating tool windows (panels,
        # sidebar, toasts) changes how *they* repaint, and they are not what is measured.
        hwnd = int(win.winId())
        filt = NoCopyBits(only=hwnd)
        app.installNativeEventFilter(filt)
        if name == "nocopybits":
            return f"SWP_NOCOPYBITS on every position change of {hwnd:#x}", \
                   lambda: app.removeNativeEventFilter(filt)
        # smartbits: the flag only while the geometry animation runs. A move that discards
        # the whole client area costs a full repaint of the page, so it is worth having
        # exactly where the jerking is — during the growth — and not elsewhere.
        state = {"on": False}

        def flip(on):
            state["on"] = on
            filt.flag = on

        orig_anim = MainWindow._anim_geometry

        def anim(self, obj, target, ms, done):
            flip(True)

            def finish():
                flip(False)

            return orig_anim(self, obj, target, ms, lambda: (finish(), done())[1])

        MainWindow._anim_geometry = anim
        return "SWP_NOCOPYBITS while the window grows only", \
               lambda: (app.removeNativeEventFilter(filt), setattr(MainWindow, "_anim_geometry", orig_anim))

    if name == "noreroute":
        # _reposition_floating is what puts the sidebar, the panels and the mini player in
        # place, and it runs on every WM_WINDOWPOSCHANGED of the animation (see
        # MainWindow.nativeEvent): a lot of work for windows that are only along for the ride.
        orig = MainWindow._reposition_floating

        def repos(self):
            if self._maximizing():
                return
            return orig(self)

        MainWindow._reposition_floating = repos
        return "floating windows stay put during the animation", \
               lambda: setattr(MainWindow, "_reposition_floating", orig)

    if name == "nomask":
        orig = VideoFrame._mask

        def mask(self):
            win_ = self.window()
            if getattr(win_, "_maximizing", lambda: False)():
                return
            return orig(self)

        VideoFrame._mask = mask
        return "no rounded mask of the video while the animation runs", \
               lambda: setattr(VideoFrame, "_mask", orig)

    if name == "freezeframe":
        orig = VideoFrame.resizeEvent

        def resizeEvent(self, e):
            win_ = self.window()
            if getattr(win_, "_maximizing", lambda: False)():
                # keep the size libVLC knows; the window grows around the picture
                self.resize(self._frozen) if hasattr(self, "_frozen") else None
                return
            self._frozen = self.size()
            return orig(self, e)

        VideoFrame.resizeEvent = resizeEvent
        return "the video keeps its size while the window grows", \
               lambda: setattr(VideoFrame, "resizeEvent", orig)

    if name.startswith("fixed") and name[5:].isdigit():
        d, u = fixed_variant(MainWindow, int(name[5:]))
        return d, u

    if name == "freezelayout":
        # A disabled layout leaves the children where they are when the window is resized (Qt
        # skips activation), so nothing is re-laid out and nothing is invalidated because of
        # it — the window grows around content that stays where it was. Everything is put
        # right in one go when the animation ends.
        orig_anim = MainWindow._anim_geometry

        def anim(self, obj, target, ms, done):
            lay = self.layout()
            if lay is not None:
                lay.setEnabled(False)

            def finish():
                if lay is not None:
                    lay.setEnabled(True)
                    lay.activate()
                self.update()
                done()

            return orig_anim(self, obj, target, ms, finish)

        MainWindow._anim_geometry = anim
        return "the layout is frozen while the window grows", \
               lambda: setattr(MainWindow, "_anim_geometry", orig_anim)

    if name == "nopaint":
        # The strip the growth opens up is painted inside the resize, in the same turn (see
        # MainWindow._flush_growth_strip) and costs ~6.5 ms of the ~8 ms the app spends on a
        # frame. Left alone, Windows has to fill that strip itself — and the window class
        # already carries the app's colour for exactly that (see wm.erase_colour).
        orig = MainWindow._flush_growth_strip

        def flush(self, old_size, new_size):
            if self._maximizing():
                return
            return orig(self, old_size, new_size)

        MainWindow._flush_growth_strip = flush
        return "the growth strip is not painted by hand during the animation",                lambda: setattr(MainWindow, "_flush_growth_strip", orig)

    if name == "nowredraw":
        # SWP_NOREDRAW: no repainting of any kind — the window is moved and sized, nothing is
        # invalidated, and nothing is asked of the application. What the compositor shows of
        # the window in the meantime is the system's own business (and is what this measures).
        hwnd = int(win.winId())
        filt = NoCopyBits(only=hwnd, flag=False, extra=SWP_NOREDRAW, extra_while=True)
        filt.active = lambda: win._maximizing()
        app.installNativeEventFilter(filt)
        return f"SWP_NOREDRAW on every position change of {hwnd:#x}",                lambda: app.removeNativeEventFilter(filt)

    raise SystemExit(f"unknown variant {name}")


MSG_NAMES = {
    0x0003: "WM_MOVE", 0x0005: "WM_SIZE", 0x000F: "WM_PAINT", 0x0014: "WM_ERASEBKGND",
    0x0046: "WM_WINDOWPOSCHANGING", 0x0047: "WM_WINDOWPOSCHANGED", 0x0083: "WM_NCCALCSIZE",
    0x0085: "WM_NCPAINT", 0x0088: "WM_SYNCPAINT", 0x00A1: "WM_NCLBUTTONDOWN",
    0x0231: "WM_ENTERSIZEMOVE", 0x0232: "WM_EXITSIZEMOVE", 0x031E: "WM_DWMCOMPOSITIONCHANGED",
}


class NativeTrace(QAbstractNativeEventFilter):
    """A timeline of what Windows and Qt do inside one animation step.

    The frames of the animation cost far more than the application's own painting, and this is
    what says where the rest goes: every window message the main window is handed, with the
    moment it arrived. A step that spends its time between WM_WINDOWPOSCHANGING and
    WM_WINDOWPOSCHANGED is spending it in the system's own resize, and the pictures in between
    (WM_PAINT, WM_NCCALCSIZE) are the application being asked for its content."""

    def __init__(self, only=None):
        super().__init__()
        self.only = only
        self.log = []
        self.t0 = None
        self.on = False

    def start(self, t0):
        self.log = []
        self.t0 = t0
        self.on = True

    def stop(self):
        self.on = False
        return self.log

    def nativeEventFilter(self, eventtype, message):
        if self.on and eventtype == b"windows_generic_MSG":
            try:
                ptr = int(message)
                mid = ctypes.c_uint32.from_address(ptr + MSG_ID_AT).value
                hwnd = ctypes.c_void_p.from_address(ptr).value
                if self.only is None or hwnd == self.only:
                    if mid in MSG_NAMES or mid in (0x0046,):
                        wparam = ctypes.c_void_p.from_address(ptr + 16).value
                        lparam = ctypes.c_void_p.from_address(ptr + MSG_LPARAM_AT).value
                        extra = ""
                        if mid == 0x0046 and lparam:
                            extra = (f"rect={ctypes.c_int.from_address(lparam + 16).value},"
                                     f"{ctypes.c_int.from_address(lparam + 20).value},"
                                     f"{ctypes.c_int.from_address(lparam + 24).value},"
                                     f"{ctypes.c_int.from_address(lparam + 28).value} "
                                     f"flags={ctypes.c_uint.from_address(lparam + WINDOWPOS_FLAGS_AT).value:#x}")
                        self.log.append((round((time.perf_counter() - self.t0) * 1000, 2),
                                         MSG_NAMES.get(mid, f"0x{mid:04x}"), wparam, extra))
            except (TypeError, ValueError, OSError):
                pass
        return False, 0


class EventProfiler:
    """Where the interface thread's milliseconds go, by event and by the widget receiving it.

    The application's own resize handler measures 7 ms of a frame that takes 30, which leaves
    more than half of it somewhere else in the same turn. Everything the thread does goes through
    QApplication.notify, so timing that — grouped by event type and by the class of the widget —
    is what says where the rest is: the paints of this or that page, the layouts, the timers."""

    WATCHED = {QEvent.Paint: "Paint", QEvent.LayoutRequest: "LayoutRequest",
               QEvent.Resize: "Resize", QEvent.Move: "Move", QEvent.Show: "Show",
               QEvent.UpdateRequest: "UpdateRequest", QEvent.Timer: "Timer",
               QEvent.UpdateLater: "UpdateLater", QEvent.PolishRequest: "Polish",
               QEvent.DynamicPropertyChange: "PropChange", QEvent.StyleChange: "StyleChange"}

    def __init__(self, app):
        self.app = app
        self.acc = {}

    def install(self):
        acc = self.acc
        watched = self.WATCHED

        def hook(orig, receiver, event):
            name = watched.get(event.type())
            if name is None:
                return orig(receiver, event)
            t = time.perf_counter()
            try:
                return orig(receiver, event)
            finally:
                d = (time.perf_counter() - t) * 1000
                cls = type(receiver).__name__
                slot = acc.setdefault((name, cls), [0, 0.0, 0.0])
                slot[0] += 1
                slot[1] += d
                slot[2] = max(slot[2], d)

        self.app.hook = hook
        return self

    def reset(self):
        self.acc.clear()

    def report(self, steps, top=12):
        rows = sorted(self.acc.items(), key=lambda kv: -kv[1][1])[:top]
        out = []
        for (name, cls), (n, total, worst) in rows:
            out.append(f"      {name:14} {cls:22} n={n:4} total={total:8.2f}ms "
                       f"avg={total / max(1, n):6.2f} max={worst:6.2f}")
        return out

    def uninstall(self):
        self.app.hook = None




class TickTiming:
    """How long one animation tick keeps the interface thread.

    The frame period is what the eye judges; the tick is what the application does in it, and
    the difference between the two is the thread waiting — for the next tick of the animation's
    timer, or for the system. Overriding the animation's own updateCurrentTime() measures the
    whole turn it takes: the geometry write, the window's resize, the layout, the paints that
    happen inside it."""

    def __init__(self):
        self.rows = []            # (start_ms, duration_ms, geometry after the write)
        self.t0 = None

    def install(self):
        # Timed at its source, not through the session's 3 ms sampling: two frames closer
        # together than that look like one change, and it is exactly those short intervals and
        # the work inside them that say whether the pacing is even.
        from PyQt5.QtCore import QPropertyAnimation
        import konus.window as kw
        owner = self
        self.rows = []
        self.t0 = None

        class Timed(QPropertyAnimation):
            def updateCurrentTime(self, t):
                start = time.perf_counter()
                super().updateCurrentTime(t)
                d = (time.perf_counter() - start) * 1000
                if owner.t0 is not None:
                    g = self.targetObject().geometry() if self.targetObject() else None
                    owner.rows.append((round((start - owner.t0) * 1000, 2), round(d, 2),
                                       None if g is None else [g.x(), g.y(), g.width(),
                                                               g.height()], None))

        class TimedDriver(kw._GeometryDriver):
            def _tick(self):
                start = time.perf_counter()
                super()._tick()
                d = (time.perf_counter() - start) * 1000
                if owner.t0 is not None:
                    g = self.win.geometry()
                    owner.rows.append((round((start - owner.t0) * 1000, 2), round(d, 2),
                                       [g.x(), g.y(), g.width(), g.height()], self.interval))

        self.orig = kw.QPropertyAnimation
        self.orig_driver = kw._GeometryDriver
        kw.QPropertyAnimation = Timed
        kw._GeometryDriver = TimedDriver
        return self

    def start(self, t0):
        self.rows = []
        self.t0 = t0

    def stop(self):
        self.t0 = None
        return self.rows

    def uninstall(self):
        import konus.window as kw
        kw.QPropertyAnimation = self.orig
        kw._GeometryDriver = self.orig_driver



class FixedDriver(QObject):
    """The same animation, driven at a fixed cadence instead of a free-running 16 ms timer.

    QPropertyAnimation asks for a frame every 16 ms and starts the wait for the next one only
    after the current frame's work is done, so every frame is 16 ms plus however long that
    frame happened to take — 19 ms one time, 50 ms the next. The eye reads that as stutter even
    when the geometry is right. This driver puts each frame at a fixed point on a clock
    (start + n * interval) and waits for the next one from wherever the work ended, so the
    frames come evenly: the interval is the animation's speed, and the work per frame only has
    to fit inside it. It is what konus.window._GeometryDriver does, kept here to try other
    intervals against it."""

    def __init__(self, obj, start, end, ms, curve, interval, done, on_frame=None):
        super().__init__()
        self.obj, self.start, self.end, self.ms, self.curve = obj, start, end, ms, curve
        self.interval = max(1, interval)
        self.done = done
        self.on_frame = on_frame
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.clock = QElapsedTimer()
        self.n = 0
        self.t_off = 0
        self.frames = 0

    def begin(self):
        self.t_off = 0
        self.clock.start()
        self.n = 0
        self.tick()

    def tick(self):
        t = self.t_off + self.clock.elapsed()
        p = 1.0 if self.ms <= 0 else min(1.0, t / self.ms)
        v = self.curve.valueForProgress(p)
        r = QRect(int(round(self.start.x() + (self.end.x() - self.start.x()) * v)),
                  int(round(self.start.y() + (self.end.y() - self.start.y()) * v)),
                  int(round(self.start.width() + (self.end.width() - self.start.width()) * v)),
                  int(round(self.start.height() + (self.end.height() - self.start.height()) * v)))
        try:
            self.obj.setGeometry(r)
        except RuntimeError:
            return
        self.frames += 1
        if self.on_frame:
            self.on_frame(self)
        if p >= 1.0:
            self.done()
            return
        self.n += 1
        due = self.n * self.interval
        now = self.clock.elapsed()
        if due < now:                      # the frame overran its slot: start the count again
            self.t_off += self.clock.elapsed()
            self.clock.start()
            self.n = 0
            due = self.interval
            now = 0
        self.timer.start(max(1, int(due - now)))


def fixed_variant(MainWindow, interval):
    orig = MainWindow._anim_geometry

    def anim(self, obj, target, ms, done):
        driver = FixedDriver(obj, QRect(obj.geometry()), target, ms, MainWindow.CURVE,
                             interval, lambda: (setattr(self, "_geo_anim", None), done())[1])
        self._geo_anim = driver
        driver.begin()

    MainWindow._anim_geometry = anim
    return f"frames at a fixed {interval} ms cadence", \
           lambda: setattr(MainWindow, "_anim_geometry", orig)


# ------------------------------------------------------------------------------ the session
class Profiler:
    """Where the milliseconds of a frame go.

    Wraps a handful of methods of the running app and adds up what each of them costs. The
    interesting number is the frame's total, because the animation's timer asks for a frame
    every 16 ms and everything above that is the stutter the eye sees."""

    TARGETS = (
        ("window resize (flush + layout)", "konus.window", "MainWindow", "resizeEvent"),
        ("  of which the growth strip", "konus.window", "MainWindow", "_flush_growth_strip"),
        ("  of which floating windows", "konus.window", "MainWindow", "_reposition_floating"),
        ("video resize (child + mask)", "konus.player", "VideoFrame", "resizeEvent"),
        ("  of which the rounded mask", "konus.player", "VideoFrame", "_mask"),
        ("library page resize", "konus.library", "FullLibrary", "resizeEvent"),
    )

    def __init__(self):
        self.acc = {}
        self.undo = []
        self.spans = []            # (label, start_ms, duration_ms) of every wrapped call
        self.t0 = None

    def install(self):
        import importlib
        for label, module, cls, method in self.TARGETS:
            try:
                kls = getattr(importlib.import_module(module), cls)
                orig = getattr(kls, method)
            except (ImportError, AttributeError):
                continue
            self._wrap(label, kls, method, orig)
        return self

    def _wrap(self, label, kls, method, orig):
        acc = self.acc
        spans = self.spans
        owner = self

        def wrapped(self, *a, **kw):
            t = time.perf_counter()
            try:
                return orig(self, *a, **kw)
            finally:
                d = (time.perf_counter() - t) * 1000
                slot = acc.setdefault(label, [0, 0.0, 0.0])
                slot[0] += 1
                slot[1] += d
                slot[2] = max(slot[2], d)
                if owner.t0 is not None and len(spans) < 4000:
                    spans.append((label.strip(), round((t - owner.t0) * 1000, 2), round(d, 2)))

        setattr(kls, method, wrapped)
        self.undo.append(lambda k=kls, m=method, o=orig: setattr(k, m, o))

    def reset(self, t0=None):
        self.acc.clear()
        self.spans.clear()          # the wrappers hold this list: replacing it would lose them
        self.t0 = t0

    def report(self, steps):
        out = []
        for label, (n, total, worst) in self.acc.items():
            out.append(f"      {label:32} n={n:3} avg={total / max(1, n):6.2f}ms "
                       f"max={worst:6.2f}ms total={total:7.2f}ms")
        return out

    def uninstall(self):
        for f in self.undo:
            f()
        self.undo = []


class Session(QObject):
    """Watch the window while the animation runs and write down every frame of it.

    The strip each frame is photographed with is measured on the spot and only the numbers are
    kept: writing a PNG takes longer than the frame it belongs to and would be the stutter it is
    supposed to measure.
    """

    def __init__(self, app, win, out, grab=False, poll_ms=3, keep=False):
        super().__init__()
        self.app, self.win, self.out, self.grab = app, win, out, grab
        self.keep = keep
        self.rows = []
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(poll_ms)
        self.timer.timeout.connect(self.tick)
        self.last = None
        self.t0 = None
        self.n = 0
        self.region = None
        self.shots = []

    def watch_region(self, rect, band=False):
        self.region = rect
        self.band = band

    def start(self, tag):
        self.tag = tag
        self.rows = []
        self.shots = []
        self.last = None
        self.t0 = time.perf_counter()
        self.n = 0
        self.timer.start()

    def stop(self):
        self.timer.stop()
        return self.rows, self.shots

    def tick(self):
        if not self.win.isVisible():
            return
        g = self.win.geometry()
        key = (g.x(), g.y(), g.width(), g.height())
        if key == self.last:
            return
        anim = getattr(self.win, "_geo_anim", None)
        want = None
        if anim is not None:
            v = anim.currentValue() if hasattr(anim, "currentValue") else None
            if v is not None:
                want = (v.x(), v.y(), v.width(), v.height())
        t = time.perf_counter()
        try:
            vg = self.win.player.video.mapToGlobal(self.win.player.video.rect().topLeft())
            video = (vg.x(), vg.y(), self.win.player.video.width(), self.win.player.video.height())
        except (RuntimeError, AttributeError):
            video = None
        row = {"t": round((t - self.t0) * 1000, 2), "wall": t, "t_wall": round(time.time() * 1000, 1), "got": key, "want": want,
               "max": bool(self.win.isMaximized()), "fs": bool(self.win.isFullScreen()),
               "video": video, "anim": anim is not None, "tag": self.tag}
        self.rows.append(row)
        self.last = key
        self.n += 1
        row["n"] = self.n
        if self.grab and self.region is not None:
            shot = QGuiApplication.primaryScreen().grabWindow(
                0, self.region.x(), self.region.y(), self.region.width(), self.region.height())
            if not shot.isNull():
                row.update(read_edges(shot.toImage(), self.region, row))
                if self.keep:
                    name = f"f{self.n:03d}-{self.tag}.png"
                    shot.save(str(self.out / name))
                    row["frame"] = name


def read_edges(img, region, row):
    """Where the app's own content sits in one photographed strip, in screen coordinates.

    Two edges are looked for, and both have a known place in a frame that is right: the video
    widget's left edge, which the layout keeps 10 px from the window's left edge, and the left
    edge of the picture inside it, the far side of the black bar libVLC leaves when the film's
    shape does not match the widget's. The bar grows and shrinks as the window does, so the
    second one moves — but it moves one way, and it is the number that says whether the picture
    followed the window or lagged behind it and jerked back into place.

    The pixels come out of the image's own bytes rather than out of pixelColor(): a few hundred
    Qt calls per frame would cost more than the frame does, and this bench must not be the thing
    that makes the animation stutter.
    """
    from PyQt5.QtGui import QImage
    out = {}
    win_x, win_w = row["got"][0], row["got"][2]
    vid = row["video"]
    x0 = win_x - region.x()
    img = img.convertToFormat(QImage.Format_RGB32)
    if x0 >= img.width() or x0 + 4 >= img.width():
        return out
    stride = img.bytesPerLine()
    data = bytes(img.constBits().asstring(img.byteCount()))
    h = img.height()
    yr = [h // 4, h // 3, h // 2, (2 * h) // 3, (3 * h) // 4]

    def px(x, y):
        i = y * stride + x * 4
        return data[i], data[i + 1], data[i + 2]        # blue, green, red

    ref = px(x0 + 3, h // 2)
    edge = scan(px, x0, min(img.width(), x0 + win_w), yr, ref, 12)
    out["frame_edge"] = None if edge is None else edge + region.x()
    out["frame_inset"] = None if edge is None else edge + region.x() - win_x
    if vid:
        # inwards from the video widget's left edge, for the end of the black bar: the bars run
        # to a couple of hundred pixels on this screen, and past that there is nothing to find
        vx = vid[0] - region.x()
        if 0 <= vx < img.width():
            pic = scan(px, vx + 2, min(img.width(), vx + 320), yr, (0, 0, 0), 30)
            if pic is not None:
                out["pic_left"] = pic + region.x()
                out["pic_inset"] = pic - vx
    return out


def scan(px, x_from, x_to, rows, ref, thresh):
    """The first column between the two that is not `ref` over most of `rows`."""
    if not rows:
        return None
    need = max(1, int(len(rows) * 0.6))
    for x in range(max(0, x_from), min(x_to, x_from + 20000)):
        hits = 0
        for y in rows:
            b, g, r = px(x, y)
            if abs(r - ref[0]) + abs(g - ref[1]) + abs(b - ref[2]) > thresh:
                hits += 1
                if hits >= need:
                    return x
    return None


def show_merged(native, spans, steps, limit=120):
    """One timeline out of two: what Windows sent the window, and what the app did with it.

    The two are separate streams — window messages arrive at the window, spans are recorded in
    Python — so putting them side by side is what shows whether a wide gap in the messages was
    the app working or the app waiting."""
    events = [(t, "msg", f"{name} {extra}".strip()) for t, name, _wp, extra in native]
    events += [(t, "span", f"{label} done in {d} ms") for label, t, d in spans]
    events.sort()
    print("    merged timeline (ms after the round started):")
    prev = events[0][0] if events else 0.0
    for i, (t, kind, text) in enumerate(events):
        if i > limit:
            print(f"      … {len(events) - limit} more")
            break
        mark = "·" if kind == "msg" else " "
        print(f"      {t:8.2f}  (+{t - prev:6.2f}) {mark} {text}")
        prev = t


RECORD_STOP = "stop"


def start_recorder(session_dir, region, scale):
    """Start the separate-process screen recorder on the area the window moves through."""
    import subprocess
    stop = session_dir / RECORD_STOP
    if stop.exists():
        stop.unlink()
    cmd = [sys.executable, str(ROOT / "tools" / "record_screen.py"),
           "--out", str(session_dir / "frames"), "--till", str(stop),
           "--region", f"{region.x()},{region.y()},{region.width()},{region.height()}",
           "--scale", str(scale)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.8)                      # let it open the display and take its first frame
    print(f"  recorder up: {region.width()}x{region.height()} at {region.x()},{region.y()} "
          f"(scale {scale})")
    return proc


def stop_recorder(proc, session_dir):
    stop = session_dir / RECORD_STOP
    stop.write_text("stop", encoding="utf-8")
    try:
        out, _ = proc.communicate(timeout=10)
        print("  " + (out or "").strip().splitlines()[-1] if out else "  recorder stopped")
    except Exception:
        proc.kill()


def make_video(session_dir, slow=1.0, fps=60):
    """Turn the recorder's frames back into something to watch, at the speed they happened.

    Each frame is given the length of time that actually passed before the next one, so the
    result is what the screen did, at the speed it did it — which is the only way to judge an
    animation that lasts a fifth of a second. `slow` stretches it for looking closely."""
    import subprocess
    frames = sorted((session_dir / "frames").glob("frame-*.jpg"))
    if not frames:
        raise SystemExit(f"no frames in {session_dir / 'frames'} — record a session first")
    times = json.loads((session_dir / "frames" / "times.json").read_text(encoding="utf-8"))
    ts = times["times_ms"]
    if len(ts) != len(frames):
        ts = ts[:len(frames)] + [ts[-1] + 33] * max(0, len(frames) - len(ts))
    lines = []
    for i, f in enumerate(frames):
        nxt = ts[i + 1] if i + 1 < len(ts) else ts[i] + 33
        lines.append(f"file '{f.as_posix()}'")
        lines.append(f"duration {max(0.004, (nxt - ts[i]) / 1000 * slow):.4f}")
    lines.append(f"file '{frames[-1].as_posix()}'")
    (session_dir / "frames" / "list.txt").write_text(chr(10).join(lines), encoding="utf-8")
    out = session_dir / f"anim{'-slow' if slow != 1 else ''}.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-i", str(session_dir / "frames" / "list.txt"), "-vf", f"fps={fps}",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(out)]
    subprocess.run(cmd, check=True)
    span = ts[-1] - ts[0]
    print(f"{out}  {len(frames)} frames over {span:.0f} ms of screen "
          f"({1000 * (len(frames) - 1) / span if span else 0:.1f} fps captured)"
          f"{' at 1/' + str(round(slow)) + ' speed' if slow != 1 else ''}")
    return out


class BenchApp(QApplication):
    """The application with a hook in notify.

    Every event the interface thread handles is delivered through QApplication.notify, and that
    is where the frames of an animation can be followed event by event — but it has to be
    overridden in a subclass: Qt calls it through its own table, so replacing the attribute of an
    instance does nothing (a lesson learned by an hour of collecting nothing)."""

    def __init__(self, argv):
        super().__init__(argv)
        self.hook = None

    def notify(self, receiver, event):
        if self.hook is None:
            return super().notify(receiver, event)
        return self.hook(super().notify, receiver, event)


def boot(media, lang="ru", size=None):
    """Start the app the way main.py does, on a library that points at `media`."""
    import main as entry
    entry.setup_vlc_windows()
    entry.load_vlc_binding()

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    from konus import APP_ID, i18n, theme, ui
    app = BenchApp([APP_ID])
    app.setApplicationName(APP_ID)
    app.setQuitOnLastWindowClosed(False)

    from konus.store import Store
    store = Store()
    store["onboarded"] = True
    store["language_chosen"] = True
    store["video_folders"] = [str(media)] if media else []
    store["theme"] = "dark"
    store["accent"] = "#d37918"
    store["use_library"] = True
    store.save()

    i18n.set_language(lang)
    theme.load_fonts()
    app.setStyle(ui.Style("Fusion"))
    theme.apply(store.get("theme"), store.get("accent"))
    app.setFont(theme.font(14))

    from konus.engine import Engine
    from konus.window import MainWindow
    engine = Engine(store)
    win = MainWindow(engine, store)
    avail = QGuiApplication.primaryScreen().availableGeometry()
    w, h = size or (1280, 760)
    win.resize(w, h)
    win.move(avail.x() + 120, avail.y() + 90)
    win.show()
    return app, store, engine, win


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def is_settled(win):
    return getattr(win, "_geo_anim", None) is None


def wait_settled(app, win, seconds=2.0):
    """Wait for the animation to end in the real event loop, not in a hand-pumped one.

    Pumping with processEvents() and sleeping between the pumps was the first version of this,
    and it was wrong in a way that spoiled every number taken with it: the sleep sits in the
    same thread the animation's timer is due on, so every frame waited for the sleep to end,
    and the frame periods came out 10-20 ms longer than the application's own. A nested event
    loop hands the thread back to Qt, and the timing that comes out is the application's."""
    loop = QEventLoop()
    check = QTimer()
    check.setInterval(5)

    def poll():
        if is_settled(win):
            loop.quit()
    check.timeout.connect(poll)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(loop.quit)
    watchdog.start(int(seconds * 1000))
    check.start()
    loop.exec_()
    check.stop()
    watchdog.stop()


def union_region(win, avail, pad=0):
    """The screen area the animation moves through: the normal window and the whole desktop."""
    a = win.geometry()
    b = avail
    r = a.united(b)
    r = r.intersected(avail)
    return r.adjusted(-pad, -pad, pad, pad)


def band_region(win, avail, height=48):
    """A thin strip across the middle of what is on screen while the window grows.

    Photographing the whole window costs 10-20 ms and would be the thing that makes the
    animation stutter. The strip is where both the video widget's left edge and the picture
    inside it are, it is the same region in every frame (what the eye needs to compare one
    frame with the next), and grabbing it is about a millisecond."""
    mid = win.player.video.mapToGlobal(win.player.video.rect().center()).y()
    mid = max(avail.top() + height, min(avail.bottom() - height, mid))
    return QRect(avail.x(), mid - height // 2, avail.width(), height)


def run(args):
    from konus import sysutil
    media = args.media
    if media is not None and media.is_file():
        media = media.parent
    app, store, engine, win = boot(media, args.lang, tuple(args.size))
    desc, undo = apply_variants(app, win, args.variant)
    variant = args.variant
    print(f"variant: {variant} — {desc}")

    out = args.out if args.out.is_absolute() else (ROOT / args.out)
    stamp = time.strftime("%H%M%S")
    out = out / f"{args.mode}-{variant}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"session: {out}")

    if args.mode == "video":
        film = args.film
        if film is None:
            films = sorted([p for p in media.iterdir()
                            if p.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm")]) if media else []
            if not films:
                raise SystemExit("--mode video needs --film or a --media folder with films in it")
            film = films[0]
        win.play([str(film)])
        print(f"playing {film.name}")
        pump(app, 6.0)
        if args.pause:
            engine.pause()
            pump(app, 1.0)
            print("paused (libVLC holds the window but draws nothing)")
    elif args.mode == "blank":
        # the video page, nothing playing: the same window and the same layout, but the hole
        # the picture goes in stays a plain black widget — the difference to --mode video is
        # what libVLC's output costs the animation
        win.show_player()
        pump(app, 2.0)
        print("nothing playing")
    else:
        win.show_library_window()
        pump(app, 2.0)

    if args.pattern:
        Pattern(win)
        pump(app, 0.3)
    avail = QGuiApplication.primaryScreen().availableGeometry()
    rec = None
    if args.record:
        rec = start_recorder(out, union_region(win, avail), args.scale)
    session = Session(app, win, out, grab=args.grab != "none", poll_ms=args.poll, keep=args.keep)
    session.watch_region(band_region(win, avail, args.band) if args.grab == "band"
                         else union_region(win, avail), band=args.grab == "band")
    prof = Profiler().install() if args.profile else None
    events = EventProfiler(app).install() if args.events else None
    ticks = TickTiming().install() if args.ticks else None
    ntrace = NativeTrace(only=int(win.winId())) if args.native_trace else None
    if ntrace is not None:
        app.installNativeEventFilter(ntrace)
    sysutil.set_above(win, True)
    pump(app, 0.4)

    rounds = []
    for i in range(args.runs):
        for tag, call in (("max", win.maximize_animated), ("restore", win.restore_animated)):
            pump(app, 0.9)
            session.start(tag)
            if prof:
                prof.reset(session.t0)
            if events:
                events.reset()
            if ticks:
                ticks.start(session.t0)
            if ntrace is not None:
                ntrace.start(session.t0)
            call()
            wait_settled(app, win, 2.0)
            pump(app, 0.25)
            tick_rows = ticks.stop() if ticks is not None else []
            native = ntrace.stop() if ntrace is not None else []
            rows, shots = session.stop()
            rounds.append({"tag": tag, "rows": rows, "shots": shots, "native": native})
            gaps = [round(rows[j + 1]["t"] - rows[j]["t"], 1) for j in range(len(rows) - 1)]
            steps = len(rows) - 1
            print(f"  {tag:7} steps={steps:2}  gaps={gaps}  "
                  f"avg={round(sum(gaps) / max(1, len(gaps)), 1)}ms")
            if prof:
                print(f"    per step ({steps} steps):")
                for line in prof.report(steps):
                    print(line)
            if tick_rows:
                durs = [row[1] for row in tick_rows]
                starts = [row[0] for row in tick_rows]
                paces = [round(b - a, 1) for a, b in zip(starts, starts[1:])]
                interval = tick_rows[-1][3] if len(tick_rows[-1]) > 3 else None
                print(f"    frames: {len(starts)} at {paces} ms (interval {interval} ms) — work "
                      f"{', '.join(f'{d:.0f}' for d in durs)} ms, {sum(durs):.0f} ms of the round")
            if events and i == 0:
                print("    events (whole round):")
                for line in events.report(steps):
                    print(line)
            if args.merged and (native or prof) and i == 0:
                show_merged(native, prof.spans if prof else [], steps)
            elif native and i == 0:
                print("    native timeline (ms after the round started):")
                prev = native[0][0]
                for t, name, wparam, extra in native:
                    print(f"      {t:8.2f}  (+{t - prev:6.2f})  {name:22} {extra}")
                    prev = t
    session.timer.stop()
    sysutil.set_above(win, False)
    if rec is not None:
        stop_recorder(rec, out)
    if prof:
        prof.uninstall()
    if events:
        events.uninstall()
    if ticks:
        ticks.uninstall()

    (out / "trace.json").write_text(json.dumps({
        "mode": args.mode, "variant": variant, "desc": desc,
        "region": [session.region.x(), session.region.y(),
                   session.region.width(), session.region.height()],
        "band": session.band, "rounds": rounds, "grab": args.grab},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out / 'trace.json'}")
    undo()
    engine.stop()
    app.quit()
    return 0


# ------------------------------------------------------------------------------ the analysis
def analyze(session_dir):
    """Read a session back and print what the photographs say about the picture's place.

    What the animation promises is a window rectangle per frame; what the eye judges is where
    the picture is. Inside the window the video widget sits at a fixed inset from the window's
    left edge, so that distance is a constant — 10 px — in every frame of a smooth animation,
    whatever the window is doing; the black bar libVLC leaves on a film's narrower side is the
    next number, and it has to move one way only. A frame where either jumps the other way is
    the jerk, in pixels."""
    data = json.loads((Path(session_dir) / "trace.json").read_text(encoding="utf-8"))
    rx, ry, rw, rh = data["region"]
    print(f"{session_dir}\n  mode={data['mode']} variant={data['variant']} — {data['desc']}")
    print(f"  capture {rw}x{rh} at {rx},{ry} ({'band' if data.get('band') else 'window area'}), "
          f"grabs={data['grab']}")
    bad = 0
    for rnd in data.get("rounds", []):
        rows = rnd["rows"]
        if len(rows) < 2:
            print(f"\n  {rnd['tag']}: nothing captured")
            continue
        steps = rows[:-1]
        gaps = [round(rows[j + 1]["t"] - rows[j]["t"], 1) for j in range(len(rows) - 1)]
        print(f"\n  {rnd['tag']}: {len(steps)} steps, gaps {gaps}")
        print(f"    avg step {round(sum(gaps) / max(1, len(gaps)), 1)} ms "
              f"(a 60 Hz frame is 16.7)")
        if data["grab"] == "none":
            continue
        print("      t(ms)  win.x  win.w   video-left-inset   pic-left-inset")
        for r in steps:
            fe, pi = r.get("frame_inset"), r.get("pic_inset")
            flag = ""
            if fe is not None and abs(fe - 10) > 6:
                flag, bad = "  <- window frame not where it should be", bad + 1
            print(f"    {r['t']:7.1f} {r['got'][0]:6} {r['got'][2]:6} "
                  f"{'' if fe is None else format(fe, '14')} {'' if pi is None else format(pi, '18')}{flag}")
    print(f"\n  frames whose window frame was off by more than 6 px: {bad}")


def magenta_lines(img, x_from, x_to, rows, need=0.6):
    """The columns of the frame that are the test pattern, gathered into whole lines.

    The pattern is magenta and nothing in the application is, so a column that is magenta over
    most of the rows it is looked at belongs to a line, and neighbouring ones are the same line.
    Returns the lines as (first column, last column) pairs, in image coordinates."""
    stride, data = img.bytesPerLine(), bytes(img.constBits().asstring(img.byteCount()))
    hits = []
    want = max(1, int(len(rows) * need))
    for x in range(max(0, x_from), min(img.width(), x_to)):
        n = 0
        for y in rows:
            k = y * stride + x * 4
            if data[k] > 150 and data[k + 2] > 150 and data[k + 1] < 110:    # blue, red high
                n += 1
                if n >= want:
                    hits.append(x)
                    break
    lines, cur = [], None
    for x in hits:
        if cur and x - cur[1] <= 2:
            cur = (cur[0], x)
        else:
            if cur:
                lines.append(cur)
            cur = (x, x)
    if cur:
        lines.append(cur)
    return lines


def scan_session(session_dir, target="video", aspect=16 / 9, clip_w=1280, clip_grid=100,
                 montage=True):
    """What the recorded frames say about where the picture was, frame by frame.

    The recorder and the player are separate processes, so the two are lined up by the wall
    clock the frames carry: for every photographed frame this finds the geometry the window had
    at that moment and measures, inside the video area of that frame, the test pattern drawn on
    it — a grid every 100 px and a solid block along its left edge (see --make-clip). Both
    numbers a frame can get wrong are in that pattern: where the picture's left edge is, and how
    big it is drawn. In a frame that is right they are what the widget's shape and the film's
    shape say they must be; a frame where they are not is the picture lagging behind the window —
    libVLC reconfigures its output a moment after it is resized — and a run of frames where the
    error changes sign is exactly the left-right jerk.

    With `target="window"` the same pattern is looked for across the whole window instead, which
    is the case when the pattern is the application's own widget (--pattern) and no film plays:
    then the lines are at fixed pixels of the window and the number says whether the content was
    drawn for the window it is in, or for the one the window was a moment ago."""
    from PyQt5.QtGui import QImage
    session_dir = Path(session_dir)
    times = json.loads((session_dir / "frames" / "times.json").read_text(encoding="utf-8"))
    trace = json.loads((session_dir / "trace.json").read_text(encoding="utf-8"))
    rx, ry, rw, rh = trace["region"]
    scale = times["scale"]
    rows = []
    for rnd in trace["rounds"]:
        rows += [(r["t_wall"], r, rnd["tag"]) for r in rnd["rows"] if r.get("t_wall")]
    rows.sort()
    stamps = [r[0] for r in rows]
    frames = sorted((session_dir / "frames").glob("frame-*.jpg"))
    print(f"{session_dir.name}: {len(frames)} frames of {rw}x{rh} at {rx},{ry}, scale {scale}, "
          f"variant {trace['variant']}, mode {trace['mode']}")
    if target == "window":
        print("  the test pattern over the window: a line every 100 px from its left edge")
    else:
        print("  the test pattern in the film: a block on its left edge, a line every 100 px")
    print("       t(ms)  tag      window                widget                "
          "left edge want/seen/dev     line spacing want/seen/dev")
    devs, tables, strips = [], [], []
    for i, path in enumerate(frames):
        entry = times["times_ms"][i] if i < len(times["times_ms"]) else None
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        t_rel, wall = entry[0], entry[1]
        j = bisect.bisect_left(stamps, wall)
        best = None
        for k in (j - 1, j, j + 1):
            if 0 <= k < len(rows):
                d = abs(rows[k][0] - wall)
                if best is None or d < best[0]:
                    best = (d, rows[k][1], rows[k][2])
        if best is None or best[0] > 45:
            continue
        row, tag = best[1], best[2]
        got = row["got"]
        box = (got[0], got[1], got[2], got[3]) if target == "window" else row.get("video")
        if not box:
            continue
        bx, by, bw, bh = box
        img = QImage(str(path)).convertToFormat(QImage.Format_RGB32)
        band = [int((by - ry + bh * f) * scale) for f in (0.3, 0.4, 0.5, 0.6, 0.7)]
        band = [y for y in band if 0 <= y < img.height()]
        if not band:
            continue
        lines = magenta_lines(img, int((bx - rx) * scale), int((bx - rx + bw) * scale), band)
        if not lines:
            continue
        seen_left = lines[0][0] / scale + rx - bx
        gaps = [lines[k + 1][0] - lines[k][0] for k in range(len(lines) - 1)]
        gaps = [g for g in gaps if g > 4]
        seen_sp = None if not gaps else sorted(gaps)[len(gaps) // 2] / scale
        if target == "window":
            # the application's own pattern: fixed pixels of the window, the same in every frame
            want_left, want_sp = 20.0, 100.0
        else:
            # the film's pattern: fitted into the widget keeping the film's shape, centered
            want_w = min(bw, bh * aspect)
            want_left = (bw - want_w) / 2
            want_sp = 100 * (want_w / clip_w)
        dev_left = seen_left - want_left
        dev_sp = None if seen_sp is None else seen_sp - want_sp
        mark = "  <- jumped" if devs and abs(dev_left - devs[-1][0]) > 12 else ""
        print(f"    {t_rel:8.1f}  {tag:7} {str(got):21} {str(box):21} "
              f"{want_left:7.0f}/{seen_left:6.0f}/{dev_left:+6.0f}   "
              f"{'' if seen_sp is None else format(seen_sp, '7.1f')}/{want_sp:<7.1f}"
              f"{'' if dev_sp is None else format(dev_sp, '+7.1f')}{mark}")
        devs.append((dev_left, dev_sp, t_rel))
        tables.append((t_rel, list(got), list(box), want_left, seen_left, want_sp, seen_sp))
        if montage and i > 0:
            strips.append(img.copy(0, max(0, img.height() // 2 - 18), img.width(), 36))
    if devs:
        lefts = [d[0] for d in devs]
        jumps = sum(1 for a, b in zip(lefts, lefts[1:]) if abs(b - a) > 12)
        flips = sum(1 for a, b, c in zip(lefts, lefts[1:], lefts[2:])
                    if (b - a) * (c - b) < 0 and abs(b - a) > 8 and abs(c - b) > 8)
        sps = [d[1] for d in devs if d[1] is not None]
        sjumps = sum(1 for a, b in zip(sps, sps[1:]) if abs(b - a) > 12)
        print(f"  left edge:  worst {max(abs(v) for v in lefts):.0f} px, {jumps} jumps over "
              f"12 px, {flips} changes of direction")
        if sps:
            print(f"  line spacing: worst {max(abs(v) for v in sps):.1f} px, "
                  f"{sjumps} jumps over 12 px")
    if strips and montage:
        h = strips[0].height()
        out = QImage(strips[0].width(), h * len(strips), QImage.Format_RGB32)
        out.fill(0)
        painter = QPainter(out)
        for k, im in enumerate(strips):
            painter.drawImage(0, k * h, im)
        painter.end()
        path = session_dir / "strip.png"
        out.save(str(path))
        print(f"  strip of middle bands (one per frame, in order): {path}")
    (session_dir / "picture.json").write_text(json.dumps(tables, ensure_ascii=False),
                                              encoding="utf-8")
    return devs


CLIP = LAB / "grid.mp4"


def make_clip(path=None):
    """Write the film the measurement uses: a magenta grid with a solid block on its left.

    Nothing of a real film can be measured from outside — a dark scene has no edge to find — so
    the picture is made of what the measurement needs: lines every 100 pixels, whose spacing is
    the scale the picture is drawn at, and a block along the left edge, which is where the
    picture starts. Both are magenta, which nothing in this application is."""
    import subprocess
    path = Path(path or CLIP)
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
           "color=c=black:s=1280x720:r=30:d=90", "-vf",
           "drawbox=x=0:y=0:w=40:h=720:color=magenta@1:t=fill,"
           "drawgrid=w=100:h=100:t=3:c=magenta@1",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-crf", "18",
           str(path)]
    subprocess.run(cmd, check=True)
    print(f"test clip written: {path}")
    return path


class Pattern(QWidget):
    """The same grid the test clip carries, drawn over the window itself.

    The library page has no film in it, so the measurement of that case needs a pattern of its
    own: magenta lines every 100 pixels of the window, which the application lays out with the
    window it is in. A frame where they are not where the window says they should be is a frame
    drawn for the window of a moment ago."""

    STEP, FIRST = 100, 20

    def __init__(self, win):
        super().__init__(win)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setGeometry(win.rect())
        win.installEventFilter(self)
        self.show()
        self.raise_()

    def eventFilter(self, obj, ev):
        if obj is self.parent() and ev.type() in (QEvent.Resize, QEvent.Show):
            self.setGeometry(obj.rect())
            self.raise_()
        return False

    def paintEvent(self, e):
        p = QPainter(self)
        pen = QPen(QColor(255, 0, 255))
        pen.setWidth(2)
        p.setPen(pen)
        for x in range(self.FIRST, self.width(), self.STEP):
            p.drawLine(x, 0, x, self.height())


def compare(variants, args, quiet=True):
    """Run the whole measurement once per variant and put the numbers side by side.

    Each variant is a run of this same file in a process of its own — the one thing a variant
    does is patch the classes of the application, and two of them in one process would answer
    for each other. What comes out is the line that decides: how long the frames took, and how
    far the picture in them was from where the window geometry says it should have been."""
    import subprocess
    rows = []
    for variant in variants:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--mode", args.mode,
               "--variant", variant, "--runs", str(args.runs), "--grab", "none",
               "--record", "--scale", str(args.scale)]
        if args.mode == "video":
            cmd += ["--clip"] if args.clip else ["--film", str(args.film), "--media",
                                                str(args.media)]
        if args.pause:
            cmd.append("--pause")
        if args.pattern:
            cmd.append("--pattern")
        print(f"\n=== {variant} ===", flush=True)
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace")
        if not quiet:
            print(out.stdout)
        if out.returncode != 0:
            print(f"  ! the run failed:\n{(out.stdout or '')[-1500:]}\n{(out.stderr or '')[-800:]}")
            continue
        session = None
        for line in out.stdout.splitlines():
            if line.startswith("session: "):
                session = Path(line.split("session: ", 1)[1].strip())
            if "steps=" in line:
                print("   " + line.strip())
        if session is None:
            continue
        devs = scan_session(session, "window" if args.pattern else "video", montage=False)
        if not devs:
            print("  ! nothing measurable in the recording")
            continue
        lefts = [abs(d[0]) for d in devs]
        sps = [abs(d[1]) for d in devs if d[1] is not None]
        seq = [d[0] for d in devs]
        flips = sum(1 for a, b, c in zip(seq, seq[1:], seq[2:])
                    if (b - a) * (c - b) < 0 and abs(b - a) > 8 and abs(c - b) > 8)
        rows.append({
            "variant": variant,
            "worst left": max(lefts) if lefts else None,
            "worst scale": max(sps) if sps else None,
            "direction changes": flips,
        })
    if rows:
        print("\n" + "=" * 78)
        print(f"{'variant':22} {'worst left err':>15} {'worst scale err':>16} "
              f"{'changes of direction':>21}")
        for r in rows:
            print(f"{r['variant']:22} {r['worst left']:>15.0f} {r['worst scale']:>16.1f} "
                  f"{r['direction changes']:>21}")
        print("A frame is right when both errors are a couple of pixels: the first is where the "
              "picture\nwas against where the window says it should be, the second how much it "
              "was stretched.")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", default="video", choices=("video", "library", "blank"),
                    help="what the window shows while it is measured: a playing film, the "
                         "library page, or the video page with nothing in it")
    ap.add_argument("--pause", action="store_true",
                    help="with --mode video: load the film but keep it paused")
    ap.add_argument("--variant", default="ctrl",
                    help="what to change for this run: " + ", ".join(VARIANT_CHOICES)
                         + ", several joined with +")
    ap.add_argument("--runs", type=int, default=2, help="maximize+restore rounds")
    ap.add_argument("--film", type=Path, default=None, help="the film to play in --mode video")
    ap.add_argument("--media", type=Path, default=None, help="a folder of films")
    ap.add_argument("--lang", default="ru")
    ap.add_argument("--size", type=int, nargs=2, default=(1280, 760), help="normal window size")
    ap.add_argument("--grab", default="band", choices=("none", "band", "union"),
                    help="photograph the screen on every step: a strip across the middle of the "
                         "window (cheap), the whole area it moves through (expensive), or nothing")
    ap.add_argument("--band", type=int, default=48, help="height of the --grab band, px")
    ap.add_argument("--keep", action="store_true", help="also save the photographed strips as PNG")
    ap.add_argument("--profile", action="store_true",
                    help="time the parts of a frame (resize, floating windows, the video mask)")
    ap.add_argument("--native-trace", action="store_true",
                    help="write down every window message of the main window during a round")
    ap.add_argument("--merged", action="store_true",
                    help="print window messages and application spans as one timeline")
    ap.add_argument("--record", action="store_true",
                    help="record the screen from a process of its own for the whole run")
    ap.add_argument("--scale", type=float, default=0.5, help="recording scale")
    ap.add_argument("--video", type=Path, default=None, help="turn a session's frames into an mp4")
    ap.add_argument("--scan", type=Path, default=None,
                    help="measure where the picture was in every recorded frame")
    ap.add_argument("--scan-target", default="video", choices=("video", "window"))
    ap.add_argument("--clip", action="store_true",
                    help="with --mode video: play the test pattern clip instead of a film")
    ap.add_argument("--pattern", action="store_true",
                    help="draw the test pattern over the window (for --mode library)")
    ap.add_argument("--compare", default="", help="comma-separated variants to run and tabulate")
    ap.add_argument("--events", action="store_true",
                    help="where a frame's time goes, by event and by widget class")
    ap.add_argument("--ticks", action="store_true",
                    help="time each animation tick and compare it with the frame period")
    ap.add_argument("--slow", type=float, default=1.0, help="with --video: stretch the result")
    ap.add_argument("--poll", type=int, default=3, help="geometry poll interval, ms")
    ap.add_argument("--out", default=LAB, type=Path)
    ap.add_argument("--analyze", type=Path, default=None, help="read a session back")
    args = ap.parse_args()
    if args.analyze:
        analyze(args.analyze)
        return 0
    if args.video:
        make_video(args.video, args.slow)
        return 0
    if args.scan:
        scan_session(args.scan, args.scan_target)
        return 0
    if args.compare:
        if args.mode == "video" and args.clip is False and args.film is None:
            args.film = make_clip()
        return 0 if compare([v.strip() for v in args.compare.split(",") if v.strip()],
                            args, quiet=False) else 1
    if args.clip:
        args.film = make_clip()
    if args.media is None and args.mode == "video" and args.film is None:
        args.media = Path(r"E:\Movies\Open Movies")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
