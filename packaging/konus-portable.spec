# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the portable build: dist\\Konus-portable\\ — the program and
everything it needs, and a `data` folder for everything it writes, in one folder that can be
moved, copied to a stick, and carried to another machine as it stands.

    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean --distpath dist ^
        packaging\\konus-portable.spec

Two things make it the portable one rather than a second name for the folder build:

* `konus/_portable.py`, a one-line module this spec writes into the bundle. Its presence is
  the flag: from it on the player keeps its settings, library, cache and log in `data` beside
  the executable instead of under %APPDATA%/%LOCALAPPDATA% (see konus.portable), and libVLC is
  told not to keep a configuration of its own at all.

* the old flat layout (`contents_directory='.'`): the runtime, the assets and the Qt libraries
  land in the folder itself, beside the executable, rather than in an `_internal` subfolder.
  That is a nuisance in an installed program and exactly right for a folder whose whole promise
  is "everything is here, nothing is anywhere else". It also matters for speed: the bundled
  runtime now sits at one fixed path, so libVLC's plugins cache stays valid between runs
  instead of being rebuilt at every start the way a single-file build's is.

The `data` folder is created as part of the build — an empty folder is not something a zip
carries, and a portable folder that arrives without it reads as a broken one, even though the
player would create it on its first run.
"""

import sys
from pathlib import Path

sys.path.insert(0, SPECPATH)
import konus_common as common

common.check_runtime()

# The flag the player reads (see konus.portable). Written here rather than kept in the source
# tree, because it is true of exactly one build.
_MARKER = common.ROOT / "build" / "portable-marker" / "konus" / "_portable.py"
_MARKER.parent.mkdir(parents=True, exist_ok=True)
_MARKER.write_text('"""Written by packaging/konus-portable.spec: this is the portable build."""\n'
                   "\nPORTABLE = True\n", encoding="utf-8")

a = Analysis(
    [str(common.ROOT / "main.py")],
    pathex=[str(common.ROOT)],
    binaries=[],
    # `portable_datas` and not `base_datas`: this build has no folder beside it that the
    # installer fills, so the binding and the licence texts it has to travel with go in here
    # (they are the program's files). The `data` folder is not in the bundle — it is the
    # *user's*, it travels as it stands, and editing the settings must not mean unpacking the
    # program.
    datas=[(str(_MARKER), "konus")] + common.portable_datas(),
    hiddenimports=common.HIDDENIMPORTS,
    hookspath=[],
    runtime_hooks=[],
    excludes=common.EXCLUDES,
    noarchive=False,
)
a.binaries = common.keep(a.binaries)
a.datas = common.keep(a.datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    # Not the plain Konus.exe of the other builds: a person who drops this file next to an
    # installed Konus should still be able to tell the two apart, both on disk and in the
    # window's taskbar entry.
    name="Konus-portable",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(common.ICON),
    version=common.write_version_resource(common.ROOT / "build" / "version_info-portable.txt",
                                          "Konus-portable.exe"),
    # The flat layout described above, which is also why this build starts as fast as the
    # installed one: nothing is unpacked at run time.
    contents_directory=".",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Konus-portable")

# The plugin cache has to be newer than the plugins beside it or libVLC ignores it (see
# konus_common.stamp_plugin_cache): this build keeps the runtime at the folder root, and a
# cache that is "the same age" as the DLLs is a cache libVLC walks past.
common.stamp_plugin_cache(Path(coll.name))

# The data folder, there from the start so the unpacked folder reads as one whole thing.
_data = Path(coll.name) / "data"
_data.mkdir(parents=True, exist_ok=True)
(_data / "README.txt").write_text(
    """This folder belongs to the player and travels with it.

It holds:
  config\\config.json   settings, library, playlists, bookmarks, resume points, presets
  cache\\               thumbnails, waveforms, seek previews, stored tags, the log

Delete it and Konus comes up fresh; copy the whole folder to another machine and the player
is there with the same library and the same settings. Nothing is written outside this
folder.\n""", encoding="utf-8")
