"""Run a function on a worker thread and deliver its result on the GUI thread.

Used for filesystem walks (expanding folders into playlists, folder info, Convert):
they can take seconds on big or network folders, and Qt must not run them in the
event loop.

The function itself must be pure Python plus filesystem access — never touch widgets
or libVLC from it. The result comes back through a QObject bridge, so the callback
runs on the thread that created the bridge (the GUI thread). A None result means the
function raised; it reaches on_error (or is dropped when there is none).
"""

import traceback

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

_pool = None
_pending = set()        # bridges of running jobs: kept until their signal was delivered


def _pool_():
    global _pool
    if _pool is None:
        _pool = QThreadPool()
        _pool.setMaxThreadCount(2)
    return _pool


class _Bridge(QObject):
    done = pyqtSignal(object)


class _Job(QRunnable):
    def __init__(self, fn, args, bridge):
        super().__init__()
        self.fn, self.args, self.bridge = fn, args, bridge
        self.setAutoDelete(True)

    def run(self):
        try:
            result = self.fn(*self.args)
        except Exception:       # a broken scan must not kill the worker
            result = None
        self.bridge.done.emit(result)


def run_bg(fn, *args, on_done=None, on_error=None):
    """fn(*args) runs on a worker thread; on_done(result) on the GUI thread."""
    bridge = _Bridge()
    _pending.add(bridge)

    def deliver(result):
        try:
            if result is None:
                if on_error is not None:
                    on_error()
            elif on_done is not None:
                on_done(result)
        except Exception:                       # a broken callback must not kill the app
            traceback.print_exc()
        finally:
            _pending.discard(bridge)

    bridge.done.connect(deliver)
    _pool_().start(_Job(fn, args, bridge))
