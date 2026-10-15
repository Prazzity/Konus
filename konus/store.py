"""Persistent state: settings (per profile), library, playlists, bookmarks, presets, history.

Everything lives in one JSON file. Settings belong to a profile ("Movies", "Shows", ...),
so switching profile swaps the whole configuration including the control layout.
"""

import copy
import fnmatch
import json
import os
import re
import time
from pathlib import Path

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from . import APP_ID, prefs

_base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
_cache = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")

CONFIG_DIR = _base / APP_ID
CACHE_DIR = _cache / APP_ID

# The app answered to another name before this one, and that is where an older build left its
# settings and its cache. A rename should not look like a fresh install, so they are moved over
# the first time this one runs — the settings carry the library, the profiles and the layout.
LEGACY_ID = "vlc-redesign"


def _adopt_legacy():
    for old, new in ((_base / LEGACY_ID, CONFIG_DIR), (_cache / LEGACY_ID, CACHE_DIR)):
        if old.is_dir() and not new.exists():
            try:
                old.rename(new)
            except OSError:
                pass          # starting empty beats not starting at all


_adopt_legacy()

# The default control layout, as in the design's main Player frame.
DEFAULT_LAYOUT = {
    "title_left": ["menu", "sidebar", "divider", "folder"],
    "title_center": ["title"],
    "title_right": ["minimize", "maximize", "fullscreen", "close"],
    "rows": [["prev", "play", "next", "current_time", "seek", "duration", "volume", "audio", "subtitles",
              "settings", "queue"]],
}

# The alternative layout from the "Player - customize" frames.
ALT_LAYOUT = {
    "title_left": [],
    "title_center": [],
    "title_right": ["minimize", "maximize", "close"],
    "rows": [["current_time", "seek", "duration"],
             ["menu", "sidebar", "divider", "folder", "title", "favorite", "spacer", "prev", "play", "next", "spacer",
              "volume", "audio", "subtitles", "settings", "queue", "fullscreen"]],
}

DEFAULT_PROFILES = ["Movies", "Shows", "Music", "Work"]

STATE_DEFAULTS = {
    "version": 2,
    "onboarded": False,
    "language_chosen": False,  # the first-run language screen was answered (see konus.language)
    "profile": "Movies",
    "profiles": {name: {} for name in DEFAULT_PROFILES},
    "volume": 80,
    "geometry": "",
    "normal_geometry": "",  # the size the window goes back to when un-maximized (see MainWindow)
    "video_folders": [],
    "music_folders": [],
    "resume": {},          # path -> {time, length, updated}
    "watched": {},         # path -> epoch seconds
    "favorites": [],       # paths / urls
    "playlists": [],       # [{id, name, thumb, items: [...], created}]
    "network": {           # saved network streams
        "streams": [],     # [{name, url}]
        "lists": [],       # [{id, name, items: [{name, url}]}]
        "podcasts": [],    # [{name, url}]
        "radio": [],       # [{name, url}]
    },
    "recent": [],          # [{item, time}]  most recent first
    "recent_open": [],     # items opened through Open File/Folder/Stream (Open Recent menu)
    "pinned": [],          # [{kind: folder|playlist|item, ref, name}]
    "visits": {},          # folder -> count
    "plays": {},           # artist -> count
    "playlist_visits": {}, # playlist id -> count
    "bookmarks": {},       # path -> [{ms, name}]
    "presets": {},         # name -> {...}
    "folder_presets": {},  # folder -> preset name
    "external_subs": {},   # video path -> [subtitle paths]
    "external_audio": {},  # video path -> [audio track paths]
    "hotkeys": {},         # action -> key sequence string (overrides)
    "global_hotkeys": {},  # action -> key sequence string
    "views": {},           # page key -> {"view": grid|list, "sort": ...}
    "mini_corner": "br",
    "lib_size": "Large",
    "home_hidden": [],     # titles of the home sections switched off   # library item size: Large | Medium | Small (every page and window)
}

RESUME_LIMIT = 500
RECENT_LIMIT = 200
WATCHED_LIMIT = 2000


def _is_url(item):
    """A stream or a network path (the same rule as engine.is_url; kept here so the store does
    not have to import the engine)."""
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", item or "")) and not (item or "").startswith("file://")


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


class Store(QObject):
    settingChanged = pyqtSignal(str, object)
    profileChanged = pyqtSignal()
    libraryChanged = pyqtSignal(str)      # what: folders|playlists|favorites|network|recent|watched|pinned|bookmarks|presets
    layoutChanged = pyqtSignal()
    saveFailed = pyqtSignal()             # the config could not be written (locked, read-only…)

    def __init__(self):
        super().__init__()
        self.path = CONFIG_DIR / "config.json"
        self.data = copy.deepcopy(STATE_DEFAULTS)
        self._newer = 0
        self._foreign = False             # the file belongs to a newer player: do not write it
        self._save_retry = 0
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                version = saved.get("version")
                if isinstance(version, int) and version > 2:
                    # A config from a *newer* player: migrating it would rewrite the user's data
                    # in an older shape (that used to reset profiles and corrupt bookmarks).
                    # Read nothing from it and — this is the part that was missing — write
                    # nothing over it: renaming it aside and then saving v2 defaults on top
                    # still took the user's newer settings out of the place their other player
                    # looks in. While that file is there this build runs on defaults and keeps
                    # its own changes for the session only.
                    self._newer = version
                    self._foreign = True
                    saved = {}
                elif version != 2:
                    saved = self._migrate(saved)
                for k, v in saved.items():
                    if k in STATE_DEFAULTS:
                        self.data[k] = self._coerce(k, v)
        except (OSError, ValueError):
            pass
        for k, v in STATE_DEFAULTS["network"].items():
            self.data["network"].setdefault(k, copy.deepcopy(v))
        if not isinstance(self.data["profiles"], dict):
            self.data["profiles"] = copy.deepcopy(STATE_DEFAULTS["profiles"])
        if not isinstance(self.data["profile"], str) or self.data["profile"] not in self.data["profiles"]:
            self.data["profile"] = STATE_DEFAULTS["profile"]
            self.data["profiles"].setdefault(self.data["profile"], {})
        self._timer = QTimer(self, singleShot=True, interval=1200, timeout=self.save)

    @staticmethod
    def _coerce(key, value):
        """Keep a stored value only when it has the shape its default has.

        A config that is valid JSON but has wrong types inside (a list where a dict belongs, a
        string where a number does, null for a container) used to be taken as it was and blew up
        deep in the UI — 14 of 33 damaged-config cases ended in a crash. Anything that does not
        match falls back to the default for that key."""
        default = STATE_DEFAULTS[key]
        if isinstance(default, bool):
            return value if isinstance(value, bool) else default
        if isinstance(default, int):
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default
        if isinstance(default, float):
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default
        if isinstance(default, str):
            return value if isinstance(value, str) else default
        if isinstance(default, list):
            if not isinstance(value, list):
                return default
            return Store._coerce_list(key, value)
        if isinstance(default, dict):
            if not isinstance(value, dict):
                return default
            return Store._coerce_map(key, value)
        return value

    @staticmethod
    def _coerce_list(key, value):
        """The same for lists: a list of the right type can still hold the wrong records."""
        def records(items, needed):
            return [it for it in items
                    if isinstance(it, dict) and all(n in it for n in needed)]

        if key in ("video_folders", "music_folders", "favorites", "home_hidden"):
            return [v for v in value if isinstance(v, str)]
        if key in ("recent", "recent_open"):
            return records(value, ("item",))
        if key == "pinned":
            return records(value, ("kind", "ref"))
        if key == "playlists":
            return records(value, ("id", "name"))
        return value

    @staticmethod
    def _coerce_map(key, value):
        """Inner shapes as well: a container of the right type can still hold nonsense (a list
        of strings where records belong, a record without its id). Each family keeps only the
        entries that have what the rest of the code reads from them."""
        def records(items, needed):
            out = []
            for it in items:
                if isinstance(it, dict) and all(n in it for n in needed):
                    out.append(it)
            return out

        if key == "profiles":
            return {name: prof for name, prof in value.items() if isinstance(prof, dict)}
        if key == "folder_presets":
            return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}
        if key == "resume":
            return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, dict)}
        if key in ("watched", "plays", "visits", "playlist_visits"):
            return {k: v for k, v in value.items()
                    if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)}
        if key in ("bookmarks", "external_subs", "external_audio"):
            out = {}
            for k, v in value.items():
                if isinstance(k, str) and isinstance(v, list):
                    out[k] = records(v, ("ms",)) if key == "bookmarks" else [x for x in v if isinstance(x, str)]
            return out
        if key in ("hotkeys", "global_hotkeys"):
            return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}
        if key in ("presets", "views"):
            return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, dict)}
        if key == "network":
            for part, needed in (("streams", ("name", "url")), ("lists", ("id", "name")),
                                 ("podcasts", ("name", "url")), ("radio", ("name", "url"))):
                if isinstance(value.get(part), list):
                    value[part] = records(value[part], needed)
            return value
        return value

    def _migrate(self, old):
        """Carry over what the first version stored."""
        new = {"onboarded": old.get("onboarded", False), "volume": old.get("volume", 80),
               "geometry": old.get("geometry", ""), "resume": old.get("resume", {})}
        folders = old.get("media_folders") or []
        new["video_folders"] = list(folders)
        new["bookmarks"] = {p: [{"ms": ms, "name": ""} for ms in marks]
                            for p, marks in (old.get("bookmarks") or {}).items()}
        new["network"] = {"streams": [{"name": u, "url": u} for u in old.get("recent_urls", [])],
                          "lists": [], "podcasts": [], "radio": []}
        return new

    # --- raw state -----------------------------------------------------
    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save_later()

    def save_later(self):
        self._timer.start()

    def save(self):
        self._timer.stop()
        if self._foreign:
            # the settings file belongs to a newer player (see __init__): leaving it alone is
            # the whole point, so this session's changes stay in memory
            return
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
            self._save_retry = 0
        except OSError:
            # The file can be locked (another copy of the player, an editor) or read-only: the
            # changes were silently lost before. Try again shortly, and tell the user once.
            self._save_retry += 1
            if self._save_retry == 1:
                self.saveFailed.emit()
            if self._save_retry <= 5:
                self._timer.start(5000)

    # --- settings (per profile) ------------------------------------------
    @property
    def profile(self):
        return self.data["profile"]

    def _prof(self):
        return self.data["profiles"].setdefault(self.data["profile"], {})

    def get(self, key):
        prof = self._prof()
        if key in prof:
            return prof[key]
        return copy.deepcopy(prefs.DEFAULTS.get(key))

    def set(self, key, value):
        if self.get(key) == value:
            return
        self._prof()[key] = value
        self.save_later()
        self.settingChanged.emit(key, value)

    def reset_settings(self):
        prof = self._prof()
        layout = prof.get("layout")
        old = {k: self.get(k) for k in prefs.DEFAULTS}
        prof.clear()
        if layout is not None:
            prof["layout"] = layout
        self.save_later()
        for k, v in old.items():
            if self.get(k) != v:
                self.settingChanged.emit(k, self.get(k))

    def profiles(self):
        return list(self.data["profiles"])

    def newer_version(self):
        """The version of a config this player refused to touch (0 when there is none)."""
        return self._newer

    def switch_profile(self, name):
        if name == self.profile or name not in self.data["profiles"]:
            return
        old = {k: self.get(k) for k in prefs.DEFAULTS}
        self.data["profile"] = name
        self.save_later()
        for k, v in old.items():
            if self.get(k) != v:
                self.settingChanged.emit(k, self.get(k))
        self.profileChanged.emit()
        self.layoutChanged.emit()

    def add_profile(self, name, copy_current=True):
        name = name.strip()
        if not name or name in self.data["profiles"]:
            return False
        self.data["profiles"][name] = copy.deepcopy(self._prof()) if copy_current else {}
        self.save_later()
        self.switch_profile(name)
        return True

    def rename_profile(self, old, new):
        """Rename a profile, keeping its settings and the selection."""
        new = (new or "").strip()
        if not new or new == old or old not in self.data["profiles"] or new in self.data["profiles"]:
            return False
        profiles = self.data["profiles"]
        rebuilt = {new if k == old else k: v for k, v in profiles.items()}
        profiles.clear()
        profiles.update(rebuilt)
        if self.data["profile"] == old:
            self.data["profile"] = new
        self.save_later()
        self.profileChanged.emit()
        return True

    def remove_profile(self, name):
        if name not in self.data["profiles"] or len(self.data["profiles"]) < 2:
            return
        if name == self.profile:
            self.switch_profile(next(n for n in self.data["profiles"] if n != name))
        self.data["profiles"].pop(name, None)
        self.save_later()
        self.profileChanged.emit()

    # --- layout ----------------------------------------------------------
    def layout(self):
        lay = self._prof().get("layout")
        if not lay:
            return copy.deepcopy(DEFAULT_LAYOUT)
        out = copy.deepcopy(DEFAULT_LAYOUT)
        out.update(copy.deepcopy(lay))
        return out

    def set_layout(self, layout):
        self._prof()["layout"] = copy.deepcopy(layout)
        self.save_later()
        self.layoutChanged.emit()

    # --- history -----------------------------------------------------------
    def private(self, item):
        pats = [p.strip() for p in (self.get("never_remember") or "").split(",") if p.strip()]
        for p in pats:
            if any(ch in p for ch in "*?["):
                if fnmatch.fnmatch(item, p) or fnmatch.fnmatch(os.path.basename(item), p):
                    return True
            elif p.lower() in item.lower():
                return True
        return False

    def add_recent(self, item):
        if not self.get("save_recent") or self.private(item):
            return
        rec = [r for r in self.data["recent"] if r["item"] != item]
        rec.insert(0, {"item": item, "time": time.time()})
        self.data["recent"] = rec[:RECENT_LIMIT]
        self.save_later()
        self.libraryChanged.emit("recent")

    def add_recent_open(self, item):
        rec = [r for r in self.data["recent_open"] if r != item]
        self.data["recent_open"] = [item] + rec[:14]
        self.save_later()

    def clear_history(self):
        self.data["recent"] = []
        self.data["recent_open"] = []
        self.data["resume"] = {}
        self.data["visits"] = {}
        self.data["plays"] = {}
        self.data["playlist_visits"] = {}
        self.save_later()
        self.libraryChanged.emit("recent")

    def visit_folder(self, folder):
        v = self.data["visits"]
        v[folder] = v.get(folder, 0) + 1
        self.save_later()

    def count_artist(self, artist):
        if artist:
            p = self.data["plays"]
            p[artist] = p.get(artist, 0) + 1
            self.save_later()

    # --- resume points / watched -------------------------------------------
    def set_progress(self, path, time_ms, length_ms):
        if not path or length_ms <= 0 or not self.get("remember_position") or self.private(path):
            return
        resume = self.data["resume"]
        # "The last stretch means it was over": 15 s of a film, but never more than a quarter of
        # the file — a fixed 15 s marked a 20-second clip watched (and dropped its resume entry)
        # while it was still at 43 %.
        tail = min(15_000, length_ms * 0.25)
        if time_ms >= length_ms * 0.95 or length_ms - time_ms < tail:
            if resume.pop(path, None) is not None:
                self.save_later()
            self.mark_watched(path, True)
            return
        if time_ms < 10_000:
            return
        resume[path] = {"time": int(time_ms), "length": int(length_ms), "updated": time.time(),
                        "size": _file_size(path)}
        if len(resume) > RESUME_LIMIT:
            oldest = sorted(resume, key=lambda p: resume[p]["updated"])[: len(resume) - RESUME_LIMIT]
            for p in oldest:
                resume.pop(p, None)
        self.save_later()

    def progress(self, path):
        r = self.resume_point(path)
        if not r or r["length"] <= 0:
            return None
        return r["time"] / r["length"]

    def resume_point(self, path):
        """The saved position of `path`, or None when it is not for this file any more.

        The key is the path, so a file that was replaced (another episode under the same name,
        a re-encode) inherited the old position and started in the middle without a word. The
        entry carries the size and mtime it was written with: a mismatch means this is a
        different file, and the stale entry is dropped."""
        r = self.data["resume"].get(path)
        if not r:
            return None
        if "size" in r and not _is_url(path) and _file_size(path) != r["size"]:
            # size, not mtime: copying or restoring the same file gives it a new mtime, and that
            # must not throw the position away — a different file has a different size
            self.data["resume"].pop(path, None)
            self.save_later()
            return None
        return r

    def forget_progress(self, path):
        if self.data["resume"].pop(path, None) is not None:
            self.save_later()

    def continue_watching(self, limit=20):
        items = sorted(self.data["resume"].items(), key=lambda kv: kv[1]["updated"], reverse=True)
        return [p for p, r in items if os.path.exists(p) and self.resume_point(p)][:limit]

    def watched(self, path):
        return path in self.data["watched"]

    def mark_watched(self, path, value=True):
        w = self.data["watched"]
        if value:
            if path in w:
                return
            w[path] = time.time()
            if len(w) > WATCHED_LIMIT:
                oldest = sorted(w, key=w.get)[: len(w) - WATCHED_LIMIT]
                for p in oldest:
                    del w[p]
        else:
            if w.pop(path, None) is None:
                return
        self.save_later()
        self.libraryChanged.emit("watched")

    # --- favorites -----------------------------------------------------------
    def is_favorite(self, item):
        return item in self.data["favorites"]

    def toggle_favorite(self, item):
        fav = self.data["favorites"]
        if item in fav:
            fav.remove(item)
        else:
            fav.insert(0, item)
        self.save_later()
        self.libraryChanged.emit("favorites")
        return item in fav

    # --- playlists -----------------------------------------------------------
    def playlists(self):
        return self.data["playlists"]

    def playlist(self, pid):
        return next((p for p in self.data["playlists"] if p["id"] == pid), None)

    def _new_id(self, prefix):
        """An id that is unique among the ones already stored: the millisecond clock alone is
        not (several lists created in the same tick shared an id, and then lookups by id hit
        the wrong one)."""
        existing = {p["id"] for p in self.data["playlists"]} |                    {p["id"] for p in self.data["network"]["lists"]}
        n = 0
        while True:
            candidate = f"{prefix}{int(time.time() * 1000)}-{n}" if n else f"{prefix}{int(time.time() * 1000)}"
            if candidate not in existing:
                return candidate
            n += 1

    def add_playlist(self, name, items=(), thumb=""):
        pid = self._new_id("pl")
        self.data["playlists"].append({"id": pid, "name": name, "thumb": thumb, "items": list(items),
                                       "created": time.time()})
        self.save_later()
        self.libraryChanged.emit("playlists")
        return pid

    def update_playlist(self, pid, **kw):
        pl = self.playlist(pid)
        if pl:
            pl.update(kw)
            self.save_later()
            self.libraryChanged.emit("playlists")

    def add_to_playlist(self, pid, items):
        pl = self.playlist(pid)
        if pl:
            pl["items"].extend(i for i in items if i not in pl["items"])
            self.save_later()
            self.libraryChanged.emit("playlists")

    def remove_playlist(self, pid):
        self.data["playlists"] = [p for p in self.data["playlists"] if p["id"] != pid]
        self.data["pinned"] = [p for p in self.data["pinned"] if not (p["kind"] == "playlist" and p["ref"] == pid)]
        self.save_later()
        self.libraryChanged.emit("playlists")

    def visit_playlist(self, pid):
        v = self.data["playlist_visits"]
        v[pid] = v.get(pid, 0) + 1
        self.save_later()

    # --- network -------------------------------------------------------------
    def network(self):
        return self.data["network"]

    def add_stream(self, url, name=None):
        streams = [s for s in self.data["network"]["streams"] if s["url"] != url]
        streams.insert(0, {"name": name or url, "url": url})
        self.data["network"]["streams"] = streams[:100]
        self.save_later()
        self.libraryChanged.emit("network")

    def add_network_list(self, name, items):
        lid = self._new_id("nl")
        self.data["network"]["lists"].append({"id": lid, "name": name, "items": items})
        self.save_later()
        self.libraryChanged.emit("network")
        return lid

    def network_list(self, lid):
        return next((p for p in self.data["network"]["lists"] if p["id"] == lid), None)

    def remove_network(self, kind, key):
        net = self.data["network"]
        if kind == "lists":
            net["lists"] = [x for x in net["lists"] if x["id"] != key]
        else:
            net[kind] = [x for x in net[kind] if x["url"] != key]
        self.save_later()
        self.libraryChanged.emit("network")

    def add_feed(self, kind, url, name):
        lst = [x for x in self.data["network"][kind] if x["url"] != url]
        lst.append({"name": name or url, "url": url})
        self.data["network"][kind] = lst
        self.save_later()
        self.libraryChanged.emit("network")

    # --- pinned ----------------------------------------------------------------
    def pin(self, kind, ref, name):
        if any(p["kind"] == kind and p["ref"] == ref for p in self.data["pinned"]):
            return
        self.data["pinned"].append({"kind": kind, "ref": ref, "name": name})
        self.save_later()
        self.libraryChanged.emit("pinned")

    def unpin(self, kind, ref):
        self.data["pinned"] = [p for p in self.data["pinned"] if not (p["kind"] == kind and p["ref"] == ref)]
        self.save_later()
        self.libraryChanged.emit("pinned")

    def is_pinned(self, kind, ref):
        return any(p["kind"] == kind and p["ref"] == ref for p in self.data["pinned"])

    # --- folders ---------------------------------------------------------------
    def add_folder(self, kind, folder):
        key = "music_folders" if kind == "music" else "video_folders"
        folder = os.path.abspath(os.path.expanduser(folder))
        # Windows ignores the case of a path, so "D:\Movies" and "d:\movies" are one folder:
        # adding both put two tiles on the page and every file twice in the search results.
        seen = {os.path.normcase(f) for f in self.data[key]}
        if os.path.normcase(folder) not in seen:
            self.data[key].append(folder)
            self.save_later()
            self.libraryChanged.emit("folders")

    def remove_folder(self, kind, folder):
        key = "music_folders" if kind == "music" else "video_folders"
        want = os.path.normcase(os.path.abspath(os.path.expanduser(folder)))
        for f in list(self.data[key]):
            if os.path.normcase(f) == want:
                self.data[key].remove(f)
                self.save_later()
                self.libraryChanged.emit("folders")
                return

    # --- bookmarks ---------------------------------------------------------------
    def bookmarks(self, path):
        return sorted(self.data["bookmarks"].get(path, []), key=lambda b: b["ms"])

    def add_bookmark(self, path, ms, name=None):
        marks = self.data["bookmarks"].setdefault(path, [])
        if any(abs(b["ms"] - ms) < 500 for b in marks):
            return False
        marks.append({"ms": int(ms), "name": name or ""})
        self.save_later()
        self.libraryChanged.emit("bookmarks")
        return True

    def rename_bookmark(self, path, ms, name):
        for b in self.data["bookmarks"].get(path, []):
            if b["ms"] == ms:
                b["name"] = name
        self.save_later()
        self.libraryChanged.emit("bookmarks")

    def remove_bookmark(self, path, ms):
        marks = self.data["bookmarks"].get(path, [])
        self.data["bookmarks"][path] = [b for b in marks if b["ms"] != ms]
        if not self.data["bookmarks"][path]:
            self.data["bookmarks"].pop(path)
        self.save_later()
        self.libraryChanged.emit("bookmarks")

    # --- presets -------------------------------------------------------------------
    def presets(self):
        return self.data["presets"]

    def save_preset(self, name, values):
        self.data["presets"][name] = copy.deepcopy(values)
        self.save_later()
        self.libraryChanged.emit("presets")

    def remove_preset(self, name):
        self.data["presets"].pop(name, None)
        for f, p in list(self.data["folder_presets"].items()):
            if p == name:
                self.data["folder_presets"].pop(f)
        self.save_later()
        self.libraryChanged.emit("presets")

    def folder_preset(self, folder):
        return self.data["folder_presets"].get(folder)

    def set_folder_preset(self, folder, name):
        if name:
            self.data["folder_presets"][folder] = name
        else:
            self.data["folder_presets"].pop(folder, None)
        self.save_later()

    # --- external subtitles per video ------------------------------------------------
    def external_subs(self, path):
        return [s for s in self.data["external_subs"].get(path, []) if os.path.exists(s)]

    def add_external_sub(self, path, sub):
        lst = self.data["external_subs"].setdefault(path, [])
        if sub not in lst:
            lst.append(sub)
            self.save_later()

    def set_external_subs(self, path, subs):
        if subs:
            self.data["external_subs"][path] = list(subs)
        else:
            self.data["external_subs"].pop(path, None)
        self.save_later()

    # --- external audio tracks per video ---------------------------------------------
    def external_audio(self, path):
        return [a for a in self.data["external_audio"].get(path, []) if os.path.exists(a)]

    def add_external_audio(self, path, track):
        lst = self.data["external_audio"].setdefault(path, [])
        if track not in lst:
            lst.append(track)
            self.save_later()

    def set_external_audio(self, path, tracks):
        if tracks:
            self.data["external_audio"][path] = list(tracks)
        else:
            self.data["external_audio"].pop(path, None)
        self.save_later()

    # --- view preferences --------------------------------------------------------------
    def view(self, page, key, default):
        return self.data["views"].get(page, {}).get(key, default)

    def set_view(self, page, key, value):
        self.data["views"].setdefault(page, {})[key] = value
        self.save_later()
