"""What the two release builds share: the paths, what goes into a bundle, what is left out,
and the version resource Konus.exe carries.

`konus.spec` (the folder build, which the installer packs) and `konus-portable.spec` (the
single-file build) are then only about the shape of their output. Keeping the rules here is
what keeps the two in step: something new the app needs is one edit, not two.
"""

import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.search(r'^__version__ = "([^"]+)"',
                    (ROOT / "konus" / "__init__.py").read_text(encoding="utf-8"), re.M).group(1)
ICON = ROOT / "konus" / "assets" / "konus.ico"


def check_runtime():
    if not (ROOT / "runtime" / "libvlc" / "libvlc.dll").is_file():
        raise SystemExit("runtime\\libvlc is missing: run `python fetch_libvlc.py` first "
                         "(or `--from <an installed VLC>` to copy one).")
    # libVLC reads this list instead of walking the plugins folder, and that folder holds 360
    # DLLs: a build without it comes up with the walk, which is the minute the first start used
    # to take (see fetch_libvlc.py, whose last step writes the file).
    if not (ROOT / "runtime" / "libvlc" / "plugins" / "plugins.dat").is_file():
        raise SystemExit("runtime\\libvlc\\plugins\\plugins.dat is missing: without it libVLC "
                         "walks all 360 plugins at every start — that is what made the player "
                         "take about a minute to come up. Run `python fetch_libvlc.py "
                         "--cache-only`, then build again.")


def base_datas():
    """What every build carries: the assets (fonts, icons, the .ico) and the libVLC runtime."""
    return [
        (str(ROOT / "konus" / "assets"), "konus/assets"),
        (str(ROOT / "runtime" / "libvlc"), "runtime/libvlc"),
    ]


def portable_datas():
    """And what only the single-file build carries *inside* itself, because it has no folder
    beside it to hold these: the python-vlc binding — which stays a plain file main.py loads,
    so a build keeps it replaceable — and the licence texts a build is obliged to travel with.
    The folder build gets the same files from tools\\build_installer.py, put next to the
    executable where they can be read without unpacking anything."""
    return base_datas() + [
        (str(ROOT / "vendor" / "vlc.py"), "vendor"),
        (str(ROOT / "vendor" / "COPYING.python-vlc"), "vendor"),
        (str(ROOT / "LICENSE"), "."),
        (str(ROOT / "README.md"), "."),
        (str(ROOT / "README.ru.md"), "."),
        (str(ROOT / "licenses" / "GPL-3.0.txt"), "licenses"),
    ]


def translation_modules():
    """`konus.i18n_<code>`, one per translation file beside konus/i18n.py.

    konus.i18n imports them by name while the app runs — which table to read is settled by the
    language setting — and a bundler cannot see an import it can only resolve at run time.
    Without this the Russian table was not in the build at all: the interface stayed English
    whatever the setting said, a restart included (the file was missing, not the choice)."""
    return [f"konus.{p.stem}" for p in sorted((ROOT / "konus").glob("i18n_*.py"))]


HIDDENIMPORTS = collect_submodules("mutagen") + translation_modules() + [
    # vendor/vlc.py is loaded from its file, so PyInstaller cannot see what it imports, and
    # this one is not asked for anywhere else in the app.
    "ctypes.util",
]

EXCLUDES = [
    "tkinter", "unittest", "pydoc", "doctest", "distutils",
    # python-vlc is a file beside the executable instead (see portable_datas).
    "vlc",
    # Qt modules the player never imports.
    "PyQt5.QtBluetooth", "PyQt5.QtDBus", "PyQt5.QtDesigner", "PyQt5.QtHelp",
    "PyQt5.QtLocation", "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets", "PyQt5.QtNfc",
    "PyQt5.QtOpenGL", "PyQt5.QtPositioning", "PyQt5.QtPrintSupport", "PyQt5.QtQml",
    "PyQt5.QtQuick", "PyQt5.QtQuick3D", "PyQt5.QtQuickWidgets", "PyQt5.QtSensors",
    "PyQt5.QtSerialPort", "PyQt5.QtSql", "PyQt5.QtTest", "PyQt5.QtWebChannel",
    "PyQt5.QtWebSockets", "PyQt5.QtXml", "PyQt5.QtXmlPatterns",
]

# Qt ships these and the player never opens them: the interface is a raster QWidget one, and
# the video goes through libVLC into the window handle rather than through Qt. About 33 MB of
# dead weight — Qt's Quick/QML stack, ANGLE and the software OpenGL fallback, D-Bus.
_DROP_FILES = {
    "PyQt5/Qt5/bin/opengl32sw.dll",
    "PyQt5/Qt5/bin/d3dcompiler_47.dll",
    "PyQt5/Qt5/bin/libEGL.dll",
    "PyQt5/Qt5/bin/libGLESv2.dll",
    "PyQt5/Qt5/bin/Qt5Qml.dll",
    "PyQt5/Qt5/bin/Qt5QmlModels.dll",
    "PyQt5/Qt5/bin/Qt5Quick.dll",
    "PyQt5/Qt5/bin/Qt5DBus.dll",
    "PyQt5/Qt5/bin/Qt5WebSockets.dll",
}
_DROP_DIRS = (
    "PyQt5/Qt5/plugins/bearer/",          # QtNetwork's bearer management
    "PyQt5/Qt5/plugins/generic/",         # input contexts other than Windows'
)
_TRANSLATIONS = "PyQt5/Qt5/translations/"


def keep(entries):
    """Filter an Analysis TOC (binaries or datas) down to what the app actually uses, on the
    rules above. Qt's own translations stay only for the languages the interface speaks."""
    kept = []
    for entry in entries:
        name = entry[0].replace("\\", "/")
        if name in _DROP_FILES or name.startswith(_DROP_DIRS):
            continue
        if name.startswith(_TRANSLATIONS) and not name.endswith(("_ru.qm", "_en.qm")):
            continue
        kept.append(entry)
    return kept


def write_version_resource(path, original_name):
    """The file properties of the built executable: name, version, who made it, and what it
    is licensed under. One place, so the About box, the exe and the installer agree."""
    v = tuple(int(p) for p in (VERSION.split(".") + ["0", "0", "0"])[:4])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Prazzity'),
      StringStruct('FileDescription', 'Konus - a player built on libVLC, in the VLC redesign interface'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'Konus'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 nagasab221 and the Konus contributors. GPLv3: see the licences shipped with this build.'),
      StringStruct('OriginalFilename', '{original_name}'),
      StringStruct('ProductName', 'Konus'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n""", encoding="utf-8")
    return str(path)
