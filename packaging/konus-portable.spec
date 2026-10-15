# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the single-file build: dist\\release\\Konus-<version>-portable.exe —
one file to attach to a release, nothing to install, no folder beside it.

    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean --distpath dist\\release ^
        packaging\\konus-portable.spec

Everything it runs on is inside it, the python-vlc binding and the licence texts included,
because there is no folder next to it to hold them. That is also what it costs: single-file
mode unpacks the whole bundle into a fresh %TEMP% folder at every start and deletes it again
on exit, so the window takes a few seconds longer to appear than the folder build's, and a
killed process leaves that folder behind until Windows clears it. Windows caches the file, so
the second start is quicker than the first.

For anybody who has a folder to keep the program in, the installer (Konus-<version>-setup.exe)
is the better of the two: same program, but it unpacks once and starts at once afterwards.
"""

import sys

sys.path.insert(0, SPECPATH)
import konus_common as common

common.check_runtime()

a = Analysis(
    [str(common.ROOT / "main.py")],
    pathex=[str(common.ROOT)],
    binaries=[],
    datas=common.portable_datas(),
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
    a.binaries,
    a.datas,
    [],
    name=f"Konus-{common.VERSION}-portable",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(common.ICON),
    version=common.write_version_resource(common.ROOT / "build" / "version_info-portable.txt",
                                          f"Konus-{common.VERSION}-portable.exe"),
)
