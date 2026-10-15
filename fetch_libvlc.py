"""Fetch the official libVLC runtime for Windows, so VLC doesn't have to be installed.

Copies what an embedded libVLC needs out of VideoLAN's official win64 build into
runtime/libvlc: libvlc.dll, libvlccore.dll, plugins/, lua/ and hrtfs(). main.py picks that
folder up by itself; an installed VLC is only used when there is no runtime.

    python fetch_libvlc.py                              # download VLC 3.0.24
    python fetch_libvlc.py --version 3.0.21
    python fetch_libvlc.py --mirror <url>               # take the archive from somewhere else
    python fetch_libvlc.py --from "E:\\Programs\\VLC"       # copy an installed VLC (offline)
    python fetch_libvlc.py --lgpl-only                  # leave out the modules that are GPLv2+
    python fetch_libvlc.py --force                      # fetch again over what is there

The zip stays in runtime/, so a refetch, or --lgpl-only after a full fetch, needs no network.
The runtime is LGPL-2.1-or-later apart from the modules --lgpl-only leaves out, which are
GPLv2-or-later; COPYING.txt and AUTHORS.txt come with it.
"""

import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
DEST = RUNTIME / "libvlc"
DEFAULT_VERSION = "3.0.24"

# VideoLAN's mirrors drop the connection for clients that don't look like a browser.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) konus-setup"
TRIES = 3

# What an embedded libVLC needs. The rest of the archive is the desktop application:
# vlc.exe, axvlc/npvlc, locale/, skins/, and it is left out.
DIRS = ("plugins", "lua", "hrtfs")
FILES = ("libvlc.dll", "libvlccore.dll", "COPYING.txt", "AUTHORS.txt")

# Every module states its own license in its descriptor, so --lgpl-only can filter on the
# module itself instead of a list that goes stale: libVLC and most modules are LGPL-2.1+,
# a few (the x264/x265 encoders, rotate, dshow, the lua interpreter ...) are GPLv2+.
LGPL_MARK = b"Lesser General Public License"
GPL_MARK = b"GNU General Public License"

# The lua interpreter module is GPLv2+, so --lgpl-only drops its scripts with it.
LUA_MODULE = "plugins/lua/liblua_plugin.dll"

CHECK_SNIPPET = """
import os, sys
os.add_dll_directory(r"{dll_dir}")
sys.path.insert(0, r"{vendor}")
import vlc
inst = vlc.Instance(["--no-video", "--vout=dummy", "--aout=dummy"])
print("libVLC", str(vlc.libvlc_get_version()).strip("b'"))
print("modules", len(inst.video_filter_list_get()), "video filters,",
      len(inst.audio_filter_list_get()), "audio filters")
"""


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_urls(version):
    """Where the official win64 build lives, in the order we try. get.videolan.org only
    redirects — and answers a client it doesn't like with an HTML page — so the download
    host goes first."""
    return [f"https://download.videolan.org/vlc/{version}/win64/vlc-{version}-win64.zip",
            f"https://get.videolan.org/vlc/{version}/win64/vlc-{version}-win64.zip"]


class DownloadFailed(Exception):
    """This host isn't serving the archive — move on to the next one."""


def is_zip(path):
    with open(path, "rb") as fh:
        return fh.read(4) == b"PK\x03\x04"


def download(url, dest):
    """Fetch the archive, picking up a partial file and retrying a dropped connection."""
    print(f"Downloading {url}")
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, TRIES + 1):
        done = have = part.stat().st_size if part.is_file() else 0
        total = 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            if have:
                req.add_header("Range", f"bytes={have}-")
            with urllib.request.urlopen(req, timeout=60) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                if ctype.startswith("text/") or "html" in ctype:
                    part.unlink(missing_ok=True)
                    raise DownloadFailed(f"this host answered with {ctype} instead of the archive")
                if have and r.status != 206:    # host ignored the range: start over
                    done = have = 0
                total = have + int(r.headers.get("Content-Length") or 0)
                with open(part, "ab" if have else "wb") as fh:
                    while True:
                        chunk = r.read(1 << 18)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if total:
                            print(f"\r  {done * 100 / total:5.1f}%  {human(done)} of {human(total)}",
                                  end="")
                        else:
                            print(f"\r  {human(done)}", end="")
            print()
            if total and done < total:
                raise urllib.error.URLError(f"the connection stopped at {human(done)}")
            if not is_zip(part):
                part.unlink(missing_ok=True)
                raise DownloadFailed("what came back is not a zip archive")
            part.replace(dest)
            return
        except DownloadFailed:
            raise
        except (urllib.error.URLError, OSError) as e:
            print(f"\n  {e}")
            if isinstance(e, urllib.error.HTTPError) and e.code == 416:
                part.unlink(missing_ok=True)     # our copy made no sense to the host
            if attempt == TRIES:
                raise DownloadFailed(f"couldn't download from {url}") from e
            print(f"  Trying again ({attempt + 1} of {TRIES})...")
            time.sleep(2 * attempt)


def fetch(urls, zip_path):
    """Download from the first host that actually serves the archive."""
    last = None
    for i, url in enumerate(urls):
        try:
            download(url, zip_path)
            return url
        except DownloadFailed as e:
            last = e
            print(f"  {e}")
            if i + 1 < len(urls):
                print("  Trying another host...")
    raise SystemExit(f"Couldn't get the libVLC archive ({last}).\n"
                     "Try again later, copy an installed VLC with "
                     "--from \"C:\\Program Files\\VideoLAN\\VLC\", or pass --mirror <url>.")


def extract(zip_path, dest):
    """Unpack the parts an embedded libVLC needs, keeping the archive's own layout."""
    kept = 0
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        root = names[0].split("/")[0] + "/" if names else ""    # "vlc-3.0.24/"
        for name in names:
            rel = name[len(root):] if name.startswith(root) else name
            if not rel or name.endswith("/"):
                continue
            if rel.split("/")[0] not in DIRS and rel not in FILES:
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(out, "wb") as fh:
                shutil.copyfileobj(src, fh, 1 << 20)
            kept += 1
    return kept


def copy_from(src, dest):
    """Same layout, taken from an installed VLC instead of the archive."""
    src, kept = Path(src), 0
    if not (src / "libvlc.dll").is_file():
        raise SystemExit(f"{src} has no libvlc.dll — that is not a VLC folder.")
    for d in DIRS:
        if (src / d).is_dir():
            for f in (src / d).rglob("*"):
                if f.is_file() and f.name != "plugins.dat":    # libVLC rebuilds its cache
                    out = dest / f.relative_to(src)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, out)
                    kept += 1
    for f in FILES:
        if (src / f).is_file():
            shutil.copy2(src / f, dest / f)
            kept += 1
    return kept


def module_license(path):
    """The license a module states for itself, read out of its descriptor."""
    tail = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                return "gpl"            # states no LGPL: treat it as the rest of VLC
            blob = tail + chunk
            if LGPL_MARK in blob:
                return "lgpl"
            if GPL_MARK in blob:
                return "gpl"
            tail = blob[-64:]


def drop_gpl_modules(dest):
    """--lgpl-only: keep the modules that are LGPL-2.1+ and drop the GPLv2+ ones."""
    kept, dropped = 0, []
    for dll in sorted((dest / "plugins").rglob("*.dll")):
        if module_license(dll) == "lgpl":
            kept += 1
        else:
            dropped.append(dll.relative_to(dest).as_posix())
            dll.unlink()
    lua = dest / LUA_MODULE
    if lua.is_file():
        lua.unlink()
        dropped.append(lua.relative_to(dest).as_posix())
    shutil.rmtree(dest / "lua", ignore_errors=True)
    return kept, dropped


def write_notice(dest, version, source, digest, kept, dropped):
    gpl = "\n".join(f"- {m}" for m in dropped) if dropped else "- none"
    libs = f"https://get.videolan.org/vlc/{version}/" if version else "https://get.videolan.org/vlc/"
    (dest / "NOTICE.md").write_text(f"""# Bundled libVLC runtime

This folder is VideoLAN's own Windows build of libVLC, unpacked here so that VLC does not
have to be installed. The files are as published, no patches.

- Source: {source}
- SHA-256 of the archive: {digest or "n/a (copied from an installed VLC)"}
- Fetched: {datetime.date.today().isoformat()}
- Files unpacked: {kept}
- VLC source code: {libs}

libVLC and its modules are LGPL-2.1-or-later except the ones listed below, which are
GPLv2-or-later (VLC as a whole is GPLv2+). `COPYING.txt` is VLC's own license file,
`AUTHORS.txt` its credits.

Modules under GPLv2+{"" if dropped else " (present in this runtime)"}:
{gpl}

Everything here is a plain DLL in a plain folder, so this runtime can be replaced with any
other VLC 3.0 build: delete this folder and run `python fetch_libvlc.py` again, with
`--from <VLC folder>` to copy an installed one or `--version` to pick another release.
""")


def check(dest):
    """Load the runtime in a subprocess: creating a libVLC instance finds the plugins."""
    env = dict(os.environ)
    env["PYTHON_VLC_LIB_PATH"] = str(dest / "libvlc.dll")
    env["PYTHON_VLC_MODULE_PATH"] = str(dest / "plugins")
    code = CHECK_SNIPPET.format(dll_dir=dest, vendor=HERE / "vendor")
    try:
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                             text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"Could not test the runtime: {e}")
        return False
    if out.returncode != 0:
        print("The runtime did not load:")
        print((out.stderr or out.stdout).strip()[-2000:])
        return False
    for line in out.stdout.strip().splitlines():
        print("  " + line)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", default=DEFAULT_VERSION, help="VLC release to fetch (default %(default)s)")
    ap.add_argument("--mirror", metavar="URL", help="take the archive from this URL instead of get.videolan.org")
    ap.add_argument("--from", dest="src", metavar="VLC_DIR",
                    help="copy an installed VLC instead of downloading")
    ap.add_argument("--dest", default=str(DEST), help="where to unpack (default %(default)s)")
    ap.add_argument("--lgpl-only", action="store_true",
                    help="leave out the modules that are GPLv2+ and the lua scripts")
    ap.add_argument("--force", action="store_true", help="fetch again over an existing runtime")
    ap.add_argument("--no-check", action="store_true", help="skip the load test")
    args = ap.parse_args()

    dest = Path(args.dest).resolve()
    if (dest / "libvlc.dll").is_file() and not args.force:
        print(f"{dest} is already there — nothing to do (use --force to fetch again).")
        return 0

    try:
        shutil.rmtree(dest)
    except FileNotFoundError:
        pass
    except OSError as e:
        raise SystemExit(f"Couldn't clear {dest} ({e}). Close Konus and try again.")
    dest.mkdir(parents=True, exist_ok=True)

    version, digest = None, None
    if args.src:
        print(f"Copying libVLC out of {args.src}")
        kept = copy_from(args.src, dest)
        source = f"installed VLC in {args.src}"
        version = ""
    else:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        zip_path = RUNTIME / f"vlc-{args.version}-win64.zip"
        version = args.version
        if zip_path.is_file():
            print(f"Using {zip_path} (already downloaded)")
            source = args.mirror or archive_urls(args.version)[0]
        else:
            urls = [args.mirror] if args.mirror else archive_urls(args.version)
            source = fetch(urls, zip_path)
        digest = sha256(zip_path)
        print(f"Unpacking into {dest}")
        kept = extract(zip_path, dest)

    (dest / "plugins" / "plugins.dat").unlink(missing_ok=True)  # libVLC writes its own
    dropped = []
    if args.lgpl_only:
        kept, dropped = drop_gpl_modules(dest)
        print(f"Left out {len(dropped)} modules that are GPLv2+ (see {dest / 'NOTICE.md'})")

    write_notice(dest, version, source, digest, kept, dropped)
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    print(f"{kept} files, {human(size)} in {dest}")

    ok = True if args.no_check else check(dest)
    print()
    if ok:
        print("The runtime is ready. VLC does not have to be installed; if it is, it is left alone.")
    else:
        print("The runtime was unpacked but did not load. Run setup.bat and let it find an "
              "installed VLC instead, or delete runtime/libvlc and try again.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
