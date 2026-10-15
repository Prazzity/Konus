"""Optional diagnostic log for chasing a bug on a real machine.

    KONUS_DIAG_LOG=C:\\path\\to\\log.txt python main.py

Off unless the variable is set: the player then writes what the wheel sends and what it
does with it (positions, seeks, the rate limiter, the poll's decisions). Lines are flushed
at once, so the file is readable even if the window is closed by force.
"""

import os
import time

_PATH = os.environ.get("VLC_DIAG_LOG") or ""
_FILE = None


def enabled():
    return bool(_PATH)


def log(tag, **fields):
    global _FILE
    if not _PATH:
        return
    if _FILE is None:
        try:
            _FILE = open(_PATH, "a", encoding="utf-8")
        except OSError:
            return
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    try:
        _FILE.write(f"{stamp} {tag:14} {body}\n")
        _FILE.flush()
    except OSError:
        pass
