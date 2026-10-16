"""Where the player keeps what it writes — and, in the portable build, why it keeps it there.

A portable build has a `konus/_portable.py` in it (the flag `packaging/konus-portable.spec`
puts in the bundle). With that flag the player is a folder that belongs to the person carrying
it: the settings, the library, the cache and the log go into a `data` folder next to the
executable and travel with it, and nothing is read from or written to the places an installed
player uses (%APPDATA%\\konus and %LOCALAPPDATA%\\konus). Move the folder and everything moves
with it; copy it to a stick and the other machine sees the same library.

Without the flag — the source tree, the folder build, the installer — nothing changes: the
folders under AppData stay where they were, and so does the settings migration from the older
name (see store._adopt_legacy).

Two things can still send an app to AppData even when the flag is there: `KONUS_DATA_DIR` in
the environment (the tests and the screenshot tool set it), and a `data` folder that cannot be
written to — a read-only stick, or a folder dropped where this user has no rights. The second
one is decided once, at the first call, by trying: a player that cannot save its settings is
worse than a player that saves them in the usual place, and only the second calls itself
portable with a straight face.
"""

import os
import sys
import tempfile
from pathlib import Path

#: The marker file the portable bundle carries; importing it is how the frozen build finds out
#: that it is the portable one. Its *absence* in the source tree is normal.
try:
    from ._portable import PORTABLE  # noqa: F401
except ImportError:
    PORTABLE = False


def target_root():
    """The folder the player runs from: the one holding the .exe, the .py in the source tree.

    A frozen build passes this through sys.executable, which is the executable on disk even
    when the bundle itself has been unpacked somewhere else."""
    if not getattr(sys, "frozen", False):
        return Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
    return Path(os.path.abspath(sys.executable)).parent


def _is_portable():
    """The flag in the bundle, or a .exe renamed to Konus-portable.exe (the way the folder is
    found again after the folder has been moved by hand)."""
    if PORTABLE:
        return True
    exe = Path(os.path.abspath(sys.executable)).stem.lower() if getattr(sys, "frozen", False) else ""
    return exe.endswith("-portable")


_data = None


def _writable(path):
    """Whether `path` can be written to. Created if it is not there yet; checked by writing,
    because that is the question being asked (a read-only stick fails here and nowhere else)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".write-test-", delete=True):
            return True
    except OSError:
        return False


def data_dir():
    """The folder the player keeps its settings and its cache in.

    Set up once, at the first call — which main() makes happen before anything else can read a
    path — and remembered after that. None means "the usual AppData folders" (see store)."""
    global _data
    if _data is not None:
        return _data or None
    override = (os.environ.get("KONUS_DATA_DIR") or "").strip()
    if override:
        want = Path(override)
        _data = want if _writable(want) else False
        return _data or None
    if not _is_portable():
        _data = False
        return None
    want = target_root() / "data"
    _data = want if _writable(want) else False
    return _data or None


def setup():
    """Make the portable folders exist before the player asks for anything, and date the plugin
    cache so libVLC reads it. Returns the data folder when the player is portable, and None when
    it is not (or cannot be)."""
    data = data_dir()
    if data is not None:
        stamp_plugin_cache()
    return data


def log_path():
    """The log's full path in a portable build, or None when the player is not portable (the
    caller then keeps its usual place, %LOCALAPPDATA%\\konus)."""
    data = data_dir()
    return str(data / "konus-log.txt") if data is not None else None


# The last date a file can carry before some tools refuse it: 2099-12-31 23:59:58.
_NEVER = 4102358398.0


def stamp_plugin_cache():
    """Date the bundled plugin cache past every one of its plugins.

    libVLC reads `plugins/plugins.dat` only while that file is at least as new as the plugins
    beside it; one module with a later timestamp and it drops the list and opens all 360 DLLs
    instead — the minute of a cold start (see fetch_libvlc.py and
    packaging/konus_common.stamp_plugin_cache).

    Nothing a build or an archive can do survives the last step of the way: unpacking rewrites
    the dates, and Explorer, 7-Zip and Python's own zipfile each stamp the files with the
    moment they were written — the plugin that comes out last ends up newer than the cache,
    whichever order it was in the archive. So the player sets it here, once, on the first start
    of a folder it has not stamped yet, with a date no later write can beat."""
    root = target_root()
    cache = root / "runtime" / "libvlc" / "plugins" / "plugins.dat"
    if not cache.is_file():
        return
    try:
        if cache.stat().st_mtime >= _NEVER:
            return                       # stamped already (this is the cheap path: one stat)
        newest = 0.0
        for f in cache.parent.rglob("*"):
            if f.is_file() and f != cache:
                try:
                    newest = max(newest, f.stat().st_mtime)
                except OSError:
                    pass
        if cache.stat().st_mtime > newest:
            return                       # newer than everything already: nothing to do
        os.utime(cache, (_NEVER, _NEVER))
    except OSError:
        pass                             # read-only media: libVLC walks the folder, it still plays


def is_portable():
    """Whether the player is running out of its own folder right now (the settings folder is
    next to the executable rather than under AppData)."""
    return data_dir() is not None
