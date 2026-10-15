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
    python fetch_libvlc.py --cache-only                 # just (re)build the plugin cache

The zip stays in runtime/, so a refetch, or --lgpl-only after a full fetch, needs no network.
The runtime is LGPL-2.1-or-later apart from the modules --lgpl-only leaves out, which are
GPLv2-or-later; COPYING.txt and AUTHORS.txt come with it.

The last step is `build_cache`: plugins/plugins.dat, the list of modules libVLC reads at
startup. libVLC only ever *reads* that file — the desktop build writes it when it installs,
which is the one job vlc-cache-gen.exe has, and it travels in the archive for it. It matters
more than it looks: without the file libVLC walks the plugins folder at every single start and
opens all 360 modules to read their descriptors, and on the first start after an install that
walk is what makes the player take a minute to come up (Windows inspects every one of those
DLLs the first time it sees them). The cache lists the plugins that are actually in the folder,
so it is built after --lgpl-only has had its say.
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

# vlc-cache-gen.exe is in the archive too, but it is a build tool rather than part of the
# runtime: it is unpacked only long enough to write the cache and then dropped again, so no
# Konus install carries it. Where the archive is not at hand (--from), an installed VLC has
# its own copy next to vlc.exe.
CACHE_GEN = "vlc-cache-gen.exe"
PLUGINS_CACHE = "plugins/plugins.dat"

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
                if f.is_file() and f.name != "plugins.dat":
                    # The source's cache is left behind and built again below: --lgpl-only
                    # may drop modules from what was just copied, and a cache that lists a
                    # module the folder no longer holds is worse than no cache at all.
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


def find_cache_gen(dest, src=None, zip_path=None):
    """vlc-cache-gen.exe, from wherever it can be had: already unpacked, an installed VLC, or
    the archive. Returns (path, borrowed) — a borrowed one is deleted again after the run."""
    gen = dest / CACHE_GEN
    if gen.is_file():
        return gen, False
    if src and (Path(src) / CACHE_GEN).is_file():
        shutil.copy2(Path(src) / CACHE_GEN, gen)
        return gen, True
    archives = [zip_path] if zip_path else sorted(RUNTIME.glob("vlc-*-win64.zip"))
    for archive in archives:
        if not (archive and Path(archive).is_file()):
            continue
        with zipfile.ZipFile(archive) as z:
            name = next((n for n in z.namelist() if n.endswith("/" + CACHE_GEN)), None)
            if name is None:
                continue
            with z.open(name) as source, open(gen, "wb") as fh:
                shutil.copyfileobj(source, fh, 1 << 20)
            return gen, True
    return None, False


def build_cache(dest, src=None, zip_path=None):
    """Write plugins/plugins.dat — the module list libVLC reads when it starts.

    libVLC cannot write this file itself (only vlc-cache-gen.exe can), so a runtime that
    ships without it costs every start a walk of the plugins folder: 360 DLLs opened one by
    one to read their descriptors. On a machine that has just seen those files for the first
    time that walk is slow — Windows inspects each DLL as it is loaded — and it is the
    difference between the window appearing at once and a minute of nothing.

    Returns True when the cache is in place.
    """
    plugins = dest / "plugins"
    if not plugins.is_dir():
        print(f"No {plugins} to build a cache from.")
        return False
    gen, borrowed = find_cache_gen(dest, src, zip_path)
    if gen is None:
        print("vlc-cache-gen.exe was not found, so no plugin cache could be built: libVLC "
              "will walk its plugins at every start (see the top of this file).")
        return False
    cache = plugins / "plugins.dat"
    cache.unlink(missing_ok=True)
    try:
        # The plugins folder has to be an absolute path. Given a relative one the tool exits
        # 0 and writes an empty cache, and an empty cache is worse than none: libVLC would
        # trust it and come up with no modules at all.
        subprocess.run([str(gen), str(plugins.resolve())], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"vlc-cache-gen.exe could not be run ({e}).")
    finally:
        if borrowed:
            gen.unlink(missing_ok=True)
    if cache.is_file() and cache.stat().st_size > 1024:
        print(f"Built {PLUGINS_CACHE} ({human(cache.stat().st_size)}): libVLC reads the "
              f"module list instead of walking the plugins folder at every start.")
        return True
    cache.unlink(missing_ok=True)
    print("No usable plugin cache came out of it — starting will be slow.")
    return False


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
    ap.add_argument("--cache-only", action="store_true",
                    help="only (re)build plugins/plugins.dat in an existing runtime")
    ap.add_argument("--no-check", action="store_true", help="skip the load test")
    args = ap.parse_args()

    dest = Path(args.dest).resolve()
    if args.cache_only:
        if not (dest / "plugins").is_dir():
            raise SystemExit(f"{dest} has no plugins folder — nothing to build a cache from. "
                             f"Run without --cache-only to unpack the runtime first.")
        return 0 if build_cache(dest) else 1
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

    version, digest, zip_path = None, None, None
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

    dropped = []
    if args.lgpl_only:
        kept, dropped = drop_gpl_modules(dest)
        print(f"Left out {len(dropped)} modules that are GPLv2+ (see {dest / 'NOTICE.md'})")

    # After the filtering, so the cache lists the modules the folder actually holds.
    build_cache(dest, src=args.src, zip_path=zip_path)

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
