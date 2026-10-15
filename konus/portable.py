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
    """Make the portable folders exist before the player asks for anything. Returns the data
    folder when the player is portable, and None when it is not (or cannot be)."""
    return data_dir()


def vlc_config_path():
    """Where libVLC is told to keep its own configuration, or None to leave that alone.

    Only the portable build moves it, and only because there is a `data` folder to put it in.
    libVLC's configuration is a `vlcrc` and an `vlc-qt-interface.ini`; what it writes there is
    everything the player changed at runtime, and on Windows it otherwise falls back to the
    folder of the running executable — inside the bundle, which is not where settings belong
    even when the bundle happens to be writable."""
    data = data_dir()
    if data is None:
        return None
    path = data / "vlc"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return str(path)


def log_path():
    """The log's full path in a portable build, or None when the player is not portable (the
    caller then keeps its usual place, %LOCALAPPDATA%\\konus)."""
    data = data_dir()
    return str(data / "konus-log.txt") if data is not None else None


def is_portable():
    """Whether the player is running out of its own folder right now (the settings folder is
    next to the executable rather than under AppData)."""
    return data_dir() is not None
