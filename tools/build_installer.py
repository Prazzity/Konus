"""Build what people download: the folder build, the portable folder, and the installer that
packs the first of them.

    .venv\\Scripts\\python.exe tools\\build_installer.py

Each step writes into dist\\, and the last two put their results in dist\\release\\ — the files
that go into a release:

1. PyInstaller reads packaging\\konus.spec and writes dist\\Konus: Konus.exe plus everything it
   runs on (PyQt5, the assets, the libVLC runtime from runtime\\libvlc — run
   `python fetch_libvlc.py` once if that folder is not there).
2. PyInstaller reads packaging\\konus-portable.spec and writes dist\\Konus-portable: the same
   program with a `data` folder beside it, which is where it keeps everything it writes — the
   settings, the library, the cache, the log. Zipped into
   dist\\release\\Konus-<version>-portable.zip, this is the one to carry on a stick: unzip it
   anywhere, run Konus-portable.exe, take the folder with you when you go.
3. What the folder build has to carry *beside* the executable goes next to it: the binding as
   the plain file main.py loads (LGPL — it stays replaceable), and the licence texts.
4. Inno Setup compiles packaging\\konus.iss out of dist\\Konus into
   dist\\release\\Konus-<version>-setup.exe. The version comes from konus\\__init__.py, so it
   is said in one place and appears in the About box, in both executables' file properties and
   in the installer's name.

Needs PyInstaller where it runs (pip install pyinstaller) and Inno Setup 6 for the last step
(winget install JRSoftware.InnoSetup). The switches skip a part of it: --without-installer,
--without-portable, --installer-only.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "Konus"
PORTABLE = ROOT / "dist" / "Konus-portable"
RELEASE = ROOT / "dist" / "release"

# What is copied beside the executable by step 3: source -> where it lands in dist\Konus.
SIDE_FILES = [
    (ROOT / "vendor" / "vlc.py", "vendor"),
    (ROOT / "vendor" / "COPYING.python-vlc", "vendor"),
    (ROOT / "LICENSE", "."),
    (ROOT / "README.md", "."),
    (ROOT / "README.ru.md", "."),
    (ROOT / "licenses" / "GPL-3.0.txt", "licenses"),
]


def app_version():
    text = (ROOT / "konus" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)


def run(cmd, **kwargs):
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    result = subprocess.run([str(c) for c in cmd], cwd=str(ROOT), **kwargs)
    if result.returncode:
        raise SystemExit(f"failed with exit code {result.returncode}")


def pyinstaller(spec, distpath):
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", distpath, "--workpath", ROOT / "build", ROOT / "packaging" / spec])


def build_folder():
    runtime = ROOT / "runtime" / "libvlc" / "libvlc.dll"
    if not runtime.is_file():
        raise SystemExit("runtime\\libvlc is missing — run `python fetch_libvlc.py` first "
                         "(or `--from <an installed VLC>` to copy one).")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit("PyInstaller is not installed here: run this with the project's "
                         "interpreter (.venv\\Scripts\\python.exe) or pip install pyinstaller.")

    pyinstaller("konus.spec", ROOT / "dist")

    for src, dest in SIDE_FILES:
        target = DIST / dest
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target / src.name)
        print(f"copied {src.relative_to(ROOT)} -> dist/Konus/{dest}")


def build_portable(version):
    """The portable folder, and the zip of it that goes into a release.

    The folder is built as dist\\Konus-portable and then carries the version in its name, so an
    unzipped one next to an older one reads as what it is. The zip holds the folder itself (not
    its contents loose), which is what keeps unzipping from mixing a new build into an old
    folder and taking the settings and the library in `data` along with it."""
    pyinstaller("konus-portable.spec", ROOT / "dist")
    if not (PORTABLE / "Konus-portable.exe").is_file():
        raise SystemExit("the portable build did not come out where it was expected: "
                         f"{(PORTABLE / 'Konus-portable.exe')}")
    named = PORTABLE.with_name(f"Konus-{version}-portable")
    if named.exists():
        shutil.rmtree(named)
    PORTABLE.rename(named)
    release_zip(named, RELEASE / f"Konus-{version}-portable.zip")


def release_zip(folder, target):
    """Zip a built folder for the release page, print what it is made of. DEFLATE at level 6:
    the bundle's DLLs compress by half and the whole thing is written once, so the tar-time is
    not worth a level 9 nobody unpacks twice."""
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(folder.parent))
                total += path.stat().st_size
    print(f"zipped {folder.name}  ({total / 1e6:.0f} MB -> {target.stat().st_size / 1e6:.0f} MB)")


def find_iscc():
    cands = [os.environ.get("INNO_SETUP_ISCC"), shutil.which("ISCC"), shutil.which("iscc")]
    for base in (os.environ.get("LOCALAPPDATA") and Path(os.environ["LOCALAPPDATA"]) / "Programs",
                 os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            cands.append(Path(base) / "Inno Setup 6" / "ISCC.exe")
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    raise SystemExit("ISCC.exe (Inno Setup 6) was not found. Install it — winget install "
                     "JRSoftware.InnoSetup — or point INNO_SETUP_ISCC at it. For everything "
                     "but the installer, run with --without-installer.")


def build_installer(version):
    if not (DIST / "Konus.exe").is_file():
        raise SystemExit("dist\\Konus\\Konus.exe is not there — run this without "
                         "--installer-only first.")
    run([find_iscc(), f"/DAppVersion={version}", ROOT / "packaging" / "konus.iss"])


def report(version):
    print()
    if (DIST / "Konus.exe").is_file():
        size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
        print(f"folder:    dist\\Konus\\Konus.exe  ({size / 1e6:.0f} MB, runs as it is)")
    folder = ROOT / "dist" / f"Konus-{version}-portable"
    if (folder / "Konus-portable.exe").is_file():
        size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        print(f"portable:  dist\\{folder.name}\\Konus-portable.exe  ({size / 1e6:.0f} MB, "
              "settings and library beside it in data\\)")
    for name in (f"Konus-{version}-portable.zip", f"Konus-{version}-setup.exe"):
        path = RELEASE / name
        if path.is_file():
            kind = "release:  " if name.endswith(".zip") else "installer:"
            print(f"{kind:11} dist\\release\\{name}  ({path.stat().st_size / 1e6:.0f} MB)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--without-installer", action="store_true",
                    help="the folder and the portable build, no Inno Setup")
    ap.add_argument("--without-portable", action="store_true",
                    help="skip the portable build and its zip (it is the slow half)")
    ap.add_argument("--installer-only", action="store_true",
                    help="only compile the installer from the dist\\Konus already there")
    args = ap.parse_args()

    version = app_version()
    print(f"Konus {version}\n")

    if args.installer_only:
        build_installer(version)
    else:
        build_folder()
        if not args.without_portable:
            build_portable(version)
        if not args.without_installer:
            build_installer(version)
    report(version)


if __name__ == "__main__":
    main()
