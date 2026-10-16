"""How long a start takes, told in one file, for when a player takes too long to come up.

`KONUS_STARTUP_LOG=<a file>` makes the app write a line for each step of its own startup —
the data folder, the settings, libVLC, the window — relative to the moment the interpreter
started, and to exit on its own after the window is up when `KONUS_STARTUP_EXIT` is set.
Nothing is written without the variable: this is a switch for measuring, not a log.

It exists because "the window takes a minute to appear" has been true twice for two different
reasons (the plugin walk, see fetch_libvlc.py, and the cache that was silently ignored, see
packaging/konus_common.stamp_plugin_cache), and neither was visible from the outside.
"""

import os
import sys
import time

_BOOT = time.perf_counter()

#: The file to write to, or None (the normal case).
path = (os.environ.get("KONUS_STARTUP_LOG") or "").strip() or None

#: Exit once the window has come up, instead of staying open (for repeated measurements).
exit_after_window = (os.environ.get("KONUS_STARTUP_EXIT") or "").strip() not in ("", "0")

if path:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"startup marks, seconds since the interpreter started\n")
            f.write(f"{0.0:7.3f}  python up "
                    f"({'frozen' if getattr(sys, 'frozen', False) else 'source'})\n")
    except OSError:
        path = None


def mark(what):
    """One step of the startup, timed from the interpreter's own start."""
    if not path:
        return 0.0
    at = time.perf_counter() - _BOOT
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{at:7.3f}  {what}\n")
    except OSError:
        pass
    return at


def watch_window(widget, on_done=None):
    """Mark when `widget` is painted for the first time, and with KONUS_STARTUP_EXIT call
    `on_done` when that has happened.

    Install this *before* show(): the paint is what a start is really measured to, and it can
    come and go in the same event-loop turn as the show. A short fallback timer covers the
    window that is up but never reports an Expose (headless sessions, a window shown behind
    another)."""
    if not path:
        return

    from PyQt5.QtCore import QEvent, QObject, QTimer

    done = [False]

    def finish():
        if done[0]:
            return
        done[0] = True
        mark("window first painted (this is the one to look at)")
        if exit_after_window and on_done:
            QTimer.singleShot(0, on_done)

    class _Watcher(QObject):
        def eventFilter(self, obj, event):
            if event.type() in (QEvent.Expose, QEvent.Paint):
                finish()
                obj.removeEventFilter(self)
            return False

    watcher = _Watcher(widget)
    widget.installEventFilter(watcher)
    widget._startup_watcher = watcher          # keep it alive as long as the window
    QTimer.singleShot(2000, finish)            # fallback: still a start that came up
