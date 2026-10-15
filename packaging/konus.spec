# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the folder build: dist\\Konus\\Konus.exe — the program with no Python
needed, run from its folder, and the thing the installer packs.

    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean packaging\\konus.spec

What goes into a bundle and what is left out of it is in konus_common.py. On top of that
shared ground, this build keeps the python-vlc binding and the licence texts *beside* the
executable rather than inside the bundle — tools\\build_installer.py copies them there, and
main.py looks for vendor\\vlc.py next to the executable first. The single-file build cannot
do that, which is the whole difference between the two specs.
"""

import sys

sys.path.insert(0, SPECPATH)
import konus_common as common

common.check_runtime()

a = Analysis(
    [str(common.ROOT / "main.py")],
    pathex=[str(common.ROOT)],
    binaries=[],
    datas=common.base_datas(),
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
    name="Konus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(common.ICON),
    version=common.write_version_resource(common.ROOT / "build" / "version_info.txt", "Konus.exe"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Konus")
