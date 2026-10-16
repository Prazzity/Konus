r"""Background media services: thumbnails, tags, durations, cover art, waveforms,
subtitle cue times and seek-preview frames. Everything is cached under the player's cache
folder — %LOCALAPPDATA%\konus, or the portable build's own data folder — and produced on a
thread pool; widgets ask, get None the first time, and repaint when the matching *Ready
signal arrives.
"""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
from collections import OrderedDict, deque
from pathlib import Path

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap

from . import sysutil
from .engine import AUDIO_EXT, SUB_EXT, VIDEO_EXT, ext, is_url, media_in_dir
from .store import cache_dir

try:
    import mutagen
except ImportError:          # tags fall back to ffprobe
    mutagen = None

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
THUMBS = cache_dir() / "thumbs"
COVERS = cache_dir() / "covers"
WAVES = cache_dir() / "waves"
PREVIEWS = cache_dir() / "previews"


def _make_cache_dirs():
    """Create the four cache folders, falling back to a temporary place if the usual one is
    unusable (a file where the folder belongs, no write rights, a read-only profile): the
    cache is a convenience and must not keep the player from starting at all."""
    global THUMBS, COVERS, WAVES, PREVIEWS
    try:
        for d in (THUMBS, COVERS, WAVES, PREVIEWS):
            d.mkdir(parents=True, exist_ok=True)
        return
    except OSError:
        pass
    base = Path(tempfile.mkdtemp(prefix="konus-cache-"))
    THUMBS, COVERS, WAVES, PREVIEWS = (base / "thumbs", base / "covers", base / "waves", base / "previews")
    for d in (THUMBS, COVERS, WAVES, PREVIEWS):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

PREVIEW_WINDOW = 8        # frames one ffmpeg run produces at most
PREVIEW_WORKERS = 2       # preview runs at once (each ffmpeg already uses several cores)
PREVIEW_QUEUE = 24        # windows waiting; a fast slide does not grow it without end


def _key(path):
    try:
        st = os.stat(path)
        raw = f"{path}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        raw = path
    return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()


def _run(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout,
                              creationflags=sysutil.quiet_process())
    except (OSError, subprocess.TimeoutExpired):
        return None


class _Job(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args
        self.setAutoDelete(True)

    def run(self):
        try:
            self.fn(*self.args)
        except Exception:
            pass


class MediaService(QObject):
    thumbReady = pyqtSignal(str)
    infoReady = pyqtSignal(str)
    waveReady = pyqtSignal(str)
    cuesReady = pyqtSignal(str)
    previewReady = pyqtSignal(str, int)

    def __init__(self):
        super().__init__()
        _make_cache_dirs()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.preview_pool = QThreadPool(self)
        self.preview_pool.setMaxThreadCount(PREVIEW_WORKERS)
        self._lock = threading.Lock()
        self._pending = set()
        self._pix = OrderedDict()          # (path, w, h) -> QPixmap
        self._info_path = cache_dir() / "info.json"
        try:
            self._info = json.loads(self._info_path.read_text())
        except (OSError, ValueError):
            self._info = {}
        self._info_dirty = False
        self._save_timer = QTimer(self, singleShot=True, interval=3000, timeout=self._save_info)
        self._waves = {}
        self._cues = {}
        self._preview_cache = OrderedDict()
        self._preview_queue = deque()
        self._preview_queued = set()
        self._preview_done = set()
        self._preview_running = 0
        self._prefill = None                 # [path, buckets, length, next window, span]
        self.thumbReady.connect(self._drop_pix)
        self.infoReady.connect(lambda _p: self._save_timer.start())

    # --- info (tags, duration) -----------------------------------------------
    def info(self, path):
        """{"duration": ms, "title", "artist", "album", "genre", "track", "disc", "date", "cover": bool} or None."""
        if is_url(path):
            return {}
        k = _key(path)
        rec = self._info.get(k)
        if rec is not None:
            return rec
        self._queue(("info", path), self._make_info, path, k)
        return None

    def info_now(self, path):
        """Blocking variant (used by background scans)."""
        if is_url(path):
            return {}
        k = _key(path)
        rec = self._info.get(k)
        if rec is None:
            self._make_info(path, k, emit=False)
            rec = self._info.get(k, {})
        return rec

    def _make_info(self, path, k, emit=True):
        rec = {"duration": 0, "title": "", "artist": "", "album": "", "genre": "", "track": 0, "disc": 0,
               "date": "", "cover": False}
        if mutagen is not None and ext(path) in AUDIO_EXT:
            try:
                f = mutagen.File(path, easy=True)
                if f is not None:
                    rec["duration"] = int((getattr(f.info, "length", 0) or 0) * 1000)
                    tags = f.tags or {}

                    def first(name):
                        v = tags.get(name)
                        return str(v[0]) if v else ""
                    rec["title"] = first("title")
                    rec["artist"] = first("artist") or first("albumartist")
                    rec["album"] = first("album")
                    rec["genre"] = first("genre")
                    rec["date"] = first("date")[:4]
                    for name, key in (("tracknumber", "track"), ("discnumber", "disc")):
                        m = re.match(r"(\d+)", first(name))
                        rec[key] = int(m.group(1)) if m else 0
                rec["cover"] = self._extract_cover(path, k)
            except Exception:
                pass
        if not rec["duration"] and FFPROBE:
            r = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration:format_tags=title,artist,album,genre",
                      "-of", "json", path], timeout=15)
            if r and r.returncode == 0:
                try:
                    fmt = json.loads(r.stdout or b"{}").get("format", {})
                    rec["duration"] = int(float(fmt.get("duration", 0)) * 1000)
                    tags = {k2.lower(): v for k2, v in (fmt.get("tags") or {}).items()}
                    for t in ("title", "artist", "album", "genre"):
                        rec[t] = rec[t] or tags.get(t, "")
                except (ValueError, TypeError):
                    pass
        with self._lock:
            self._info[k] = rec
            self._info_dirty = True
            self._pending.discard(("info", path))
        if emit:
            self.infoReady.emit(path)

    def _extract_cover(self, path, k):
        out = COVERS / f"{k}.jpg"
        if out.exists():
            return True
        data = None
        try:
            f = mutagen.File(path)
            if f is None:
                return False
            tags = f.tags
            if hasattr(f, "pictures") and f.pictures:            # FLAC
                data = f.pictures[0].data
            elif tags is not None:
                for key in list(tags.keys()):
                    if key.startswith("APIC"):                   # ID3
                        data = tags[key].data
                        break
                    if key == "covr":                            # MP4
                        data = bytes(tags["covr"][0])
                        break
                    if key.lower() == "metadata_block_picture":  # Ogg
                        import base64
                        from mutagen.flac import Picture
                        data = Picture(base64.b64decode(tags[key][0])).data
                        break
        except Exception:
            data = None
        if not data:
            return False
        img = QImage.fromData(data)
        if img.isNull():
            return False
        img = img.scaled(600, 600, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return img.save(str(out), "JPG", 88)

    def cover_path(self, path):
        p = COVERS / f"{_key(path)}.jpg"
        return str(p) if p.exists() else ""

    def _save_info(self):
        if not self._info_dirty:
            return
        with self._lock:
            data = dict(self._info)
            self._info_dirty = False
        try:
            tmp = self._info_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self._info_path)
        except OSError:
            pass

    # --- thumbnails ------------------------------------------------------------
    def thumb(self, path, w, h, dpr=1.0):
        """Centre-cropped thumbnail pixmap, or None while it is being made."""
        if is_url(path):
            return None
        key = (path, w, h, dpr)
        pm = self._pix.get(key)
        if pm is not None:
            self._pix.move_to_end(key)
            return pm
        k = _key(path)
        src = None
        if ext(path) in AUDIO_EXT:
            cp = COVERS / f"{k}.jpg"
            if cp.exists():
                src = str(cp)
            elif self._info.get(k) is None:
                self.info(path)
                return None
            else:
                return None
        else:
            tp = THUMBS / f"{k}.jpg"
            if tp.exists():
                src = str(tp)
            else:
                if FFMPEG and ext(path) in VIDEO_EXT:
                    self._queue(("thumb", path), self._make_thumb, path, str(tp))
                return None
        img = QImage(src)
        if img.isNull():
            return None
        pm = QPixmap.fromImage(_crop(img, int(w * dpr), int(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        self._pix[key] = pm
        if len(self._pix) > 600:
            self._pix.popitem(last=False)
        return pm

    def _drop_pix(self, path):
        for key in [k for k in self._pix if k[0] == path]:
            self._pix.pop(key, None)

    def _make_thumb(self, path, out):
        dur = 0
        info = self._info.get(_key(path))
        if info:
            dur = info.get("duration", 0) / 1000
        if not dur and FFPROBE:
            r = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], 15)
            try:
                dur = float(r.stdout.strip()) if r and r.returncode == 0 else 0
            except ValueError:
                dur = 0
        at = max(0.0, min(dur * 0.2, dur - 1)) if dur > 2 else 0
        r = _run([FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", path, "-frames:v", "1",
                  "-vf", "scale=480:-2", "-q:v", "4", out], 40)
        with self._lock:
            self._pending.discard(("thumb", path))
        if r and r.returncode == 0 and os.path.exists(out):
            self.thumbReady.emit(path)

    # --- waveform ----------------------------------------------------------------
    def waveform(self, path, bins=600):
        if is_url(path) or not FFMPEG:
            return None
        if path in self._waves:
            return self._waves[path]
        f = WAVES / f"{_key(path)}.json"
        if f.exists():
            try:
                self._waves[path] = json.loads(f.read_text())
                return self._waves[path]
            except (OSError, ValueError):
                pass
        self._queue(("wave", path), self._make_wave, path, str(f), bins)
        return None

    def _make_wave(self, path, out, bins):
        try:
            proc = subprocess.Popen([FFMPEG, "-nostdin", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "2000",
                                     "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    creationflags=sysutil.quiet_process())
            raw = proc.stdout.read()
            proc.wait(timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            raw = b""
        n = len(raw) // 2
        peaks = []
        if n:
            samples = struct.unpack(f"<{n}h", raw[: n * 2])
            per = max(1, n // bins)
            for i in range(0, n, per):
                chunk = samples[i:i + per]
                peaks.append(max(abs(min(chunk)), abs(max(chunk))) / 32768)
            top = max(peaks) or 1
            peaks = [round(min(1.0, (v / top) ** 0.8), 3) for v in peaks]
        with self._lock:
            self._pending.discard(("wave", path))
        if peaks:
            self._waves[path] = peaks
            try:
                open(out, "w").write(json.dumps(peaks))
            except OSError:
                pass
            self.waveReady.emit(path)

    # --- subtitle cues ----------------------------------------------------------
    def cues(self, path, subfile=None):
        """[(start_ms, end_ms)] for the given subtitle file, or the first embedded track."""
        key = subfile or path
        if key in self._cues:
            return self._cues[key]
        if subfile:
            self._cues[key] = parse_cues(subfile)
            return self._cues[key]
        if is_url(path) or not FFMPEG:
            return None
        self._queue(("cues", key), self._make_cues, path, key)
        return None

    def _make_cues(self, path, key):
        r = _run([FFMPEG, "-nostdin", "-v", "error", "-i", path, "-map", "0:s:0", "-f", "srt", "-"], 60)
        cues = parse_srt(r.stdout.decode("utf-8", "replace")) if r and r.returncode == 0 else []
        self._cues[key] = cues
        with self._lock:
            self._pending.discard(("cues", key))
        self.cuesReady.emit(key)

    # --- seek previews -----------------------------------------------------------
    def _preview_file(self, path, b):
        return PREVIEWS / f"{_key(path)}-{b}.jpg"

    def _preview_pixmap(self, path, b):
        key = (path, b)
        pm = self._preview_cache.get(key)
        if pm is not None:
            return pm
        f = self._preview_file(path, b)
        if f.exists():
            pm = QPixmap(str(f))
            if not pm.isNull():
                self._preview_cache[key] = pm
                if len(self._preview_cache) > 300:
                    self._preview_cache.popitem(last=False)
                return pm
        return None

    def preview(self, path, ms, length, buckets=240):
        """A small frame near ms for the seek-bar hover card, or None while loading."""
        if is_url(path) or not FFMPEG or length <= 0:
            return None
        b = int(ms / length * buckets)
        pm = self._preview_pixmap(path, b)
        if pm is not None:
            return pm
        self._want_preview(path, b - b % self._preview_span(length, buckets), buckets, length, urgent=True)
        return None

    def prefill_previews(self, path, length, buckets=240):
        """Fill the whole strip in the background the first time the bar is hovered, so the
        frames are there before the pointer gets to them. The queue is bounded, so the
        windows are fed in as it drains rather than all at once."""
        if is_url(path) or not FFMPEG or length <= 0:
            return
        self._prefill = [path, buckets, length, 0, self._preview_span(length, buckets)]
        self._prefill_more()

    def _prefill_more(self):
        job = self._prefill
        if not job:
            return
        path, buckets, length, b0, span = job
        while b0 < buckets and len(self._preview_queue) < PREVIEW_QUEUE:
            self._want_preview(path, b0, buckets, length)
            b0 += span
        job[3] = b0
        if b0 >= buckets:
            self._prefill = None

    def _preview_span(self, length, buckets):
        """How many frames one ffmpeg run should produce. More frames per run saves the
        process start and the seek, but they have to stay within a few seconds of video —
        decoding a minute of film to get one small picture is not worth it."""
        if length <= 0:
            return 1
        per_nothing = 10000                                  # 10 s of video at most
        return max(1, min(PREVIEW_WINDOW, int(per_nothing * buckets / length)))

    def _want_preview(self, path, b0, buckets, length, urgent=False):
        key = (path, b0)
        with self._lock:
            if key in self._preview_done or key in self._preview_queued:
                return
            if len(self._preview_queue) >= PREVIEW_QUEUE:
                if not urgent:
                    return
                self._preview_queue.pop()                    # the hovered one goes first
            self._preview_queued.add(key)
            item = (path, b0, buckets, length, self._preview_span(length, buckets))
            if urgent:
                self._preview_queue.appendleft(item)
            else:
                self._preview_queue.append(item)
        self._pump_previews()

    def _pump_previews(self):
        while True:
            with self._lock:
                if self._preview_running >= PREVIEW_WORKERS or not self._preview_queue:
                    return
                item = self._preview_queue.popleft()
                self._preview_running += 1
            self.preview_pool.start(_Job(self._make_preview, *item))

    def _make_preview(self, path, b0, buckets, length, span):
        """One ffmpeg run writes `span` frames, numbered by bucket, so each lands in the
        cache under its own name."""
        try:
            if not self._preview_file(path, b0).exists():
                at = b0 / buckets * length / 1000
                if span <= 1:
                    _run([FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", path,
                          "-frames:v", "1", "-vf", "scale=256:-2", "-q:v", "5",
                          str(self._preview_file(path, b0))], 15)
                else:
                    fps = buckets / (length / 1000)          # one frame per bucket
                    dur = span / buckets * length / 1000
                    _run([FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", path,
                          "-t", f"{dur:.3f}", "-vf", f"fps={fps:.6f},scale=256:-2", "-q:v", "5",
                          "-f", "image2", "-start_number", str(b0),
                          str(PREVIEWS / f"{_key(path)}-%d.jpg")], 25)
        finally:
            with self._lock:
                self._preview_running = max(0, self._preview_running - 1)
                self._preview_queued.discard((path, b0))
                if self._preview_file(path, b0).exists():
                    self._preview_done.add((path, b0))   # produced: don't ask again
                # a window that produced nothing stays retryable (the very end of a file
                # sometimes has no frame at that exact spot)
            self._pump_previews()
            self._prefill_more()
        self.previewReady.emit(path, b0)

    # --- internals ----------------------------------------------------------------
    def _queue(self, key, fn, *args):
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
        self.pool.start(_Job(fn, *args))

    def shutdown(self):
        self._save_info()
        self.pool.clear()
        self.preview_pool.clear()


def _crop(img, w, h):
    scaled = img.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x = (scaled.width() - w) // 2
    y = (scaled.height() - h) // 2
    return scaled.copy(x, y, w, h)


def _ts(text):
    text = text.strip().replace(",", ".")
    parts = text.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
        elif len(parts) == 2:
            h, (m, s) = 0, parts
        else:
            return 0
        return int((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)
    except ValueError:
        return 0


def parse_srt(text):
    cues = []
    for m in re.finditer(r"(\d+:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d+:\d{2}:\d{2}[.,]\d{1,3})", text):
        cues.append((_ts(m.group(1)), _ts(m.group(2))))
    return cues


def parse_cues(path):
    try:
        raw = open(path, "rb").read()
    except OSError:
        return []
    text = raw.decode("utf-8", "replace")
    e = ext(path)
    if e in ("ass", "ssa"):
        cues = []
        for line in text.splitlines():
            if line.startswith("Dialogue:"):
                parts = line.split(",", 3)
                if len(parts) >= 3:
                    cues.append((_ts(parts[1]), _ts(parts[2])))
        return sorted(cues)
    if e == "vtt":
        text = re.sub(r"(\n|^)(\d{2}:\d{2}\.\d{3})", r"\g<1>00:\2", text)
    return parse_srt(text)


# --- matching external audio tracks to episodes ----------------------------------
_LANG_TAGS = ("rus", "ru", "russian", "eng", "en", "english", "jpn", "jp", "japanese", "ukr",
              "dub", "dual", "audio", "sound", "track", "commentary", "mka")
# Episode numbers, from the most explicit form to the loosest. Digits that belong to a
# resolution, a codec, a year or a channel pair ("1080p", "x265", "2021", "5.1") must not be
# mistaken for an episode — the lookarounds in the bare case are what keeps them out.
_EP_SEASON = re.compile(r"\bs(\d{1,2})\s*[ex]\s*(\d{1,3})\b")             # s02e05, S2x05
_EP_SHORT = re.compile(r"\b(\d{1,2})\s*x\s*(\d{1,3})\b")                  # 2x05
_EP_MARK = re.compile(r"\b(?:ep?|ser|series)\s*[.\-]?\s*(\d{1,3})\b")     # ep05, e05, ep.05
_EP_BARE = re.compile(r"(?<![A-Za-z0-9.])(\d{1,3})(?![A-Za-z0-9.])")     # a lone "05"


def name_key(path):
    """A file name as words of letters and digits, so the boundaries survive:
    "[anti-raws]ep.01[BDRemux]" -> "anti raws ep 01 bdremux"."""
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in stem).split())


def episode_number(key):
    return episode_key(key)[1]


def episode_key(key):
    """(season, episode) of a file name key, either of them None when the name doesn't say.

    "s02e05" -> (2, 5); "2x05" -> (2, 5); "ep.05" -> (None, 5); a bare "… 05 …" -> (None, 5);
    "Show [1080p] x265 2021.mkv" -> (None, None) — those digits are not an episode."""
    m = _EP_SEASON.search(key)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _EP_SHORT.search(key)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _EP_MARK.search(key)
    if m:
        return None, int(m.group(1))
    last = None
    for m in _EP_BARE.finditer(key):
        last = m                          # "Show 2 - 05": the episode is the last lone number
    return (None, int(last.group(1))) if last else (None, None)


def _without_lang(key):
    """Drop the trailing language marker an audio track usually carries: "… ep 01 rus" -> "… ep 01"."""
    words = key.split()
    while words and words[-1] in _LANG_TAGS:
        words.pop()
    return " ".join(words)


def same_episode(video_key, audio_key):
    """Whether an audio file belongs to this video.

    The episode number decides: releases name their files differently ("ep.01", "01", "S02E05"),
    but 5 is 5. A season is only compared when both names carry one, so "S02E05" matches
    "…ep.05…" and no longer matches "S03E05". Only when neither name has a number do the names
    themselves have to line up."""
    a, b = _without_lang(audio_key).replace(" ", ""), video_key.replace(" ", "")
    if a == b:
        return True
    if not a or not b:
        return False
    vs, ve = episode_key(video_key)
    as_, ae = episode_key(audio_key)
    if ve is not None and ae is not None:
        return ve == ae and (vs is None or as_ is None or vs == as_)
    if ve is not None or ae is not None:
        return False                      # one names an episode, the other does not
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 8 and longer.startswith(shorter)


def shared_words(video_key, audio_key):
    """Words that mean something in both names ("stone"), to prefer one of two tracks that
    happen to carry the same episode number."""
    def words(key):
        return {w for w in _without_lang(key).split()
                if len(w) >= 3 and w.isalpha() and w not in _LANG_TAGS}
    return words(video_key) & words(audio_key)


def match_audio_tracks(videos, folders):
    """[(video, track)]: the audio file from `folders` (and their first-level subfolders,
    where "RUS Sound" style folders live) that belongs to each video."""
    tracks = []
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        tracks += media_in_dir(folder, AUDIO_EXT)
        try:
            subs = [e.path for e in os.scandir(folder) if e.is_dir()]
        except OSError:
            subs = []
        for sub in subs:
            tracks += media_in_dir(sub, AUDIO_EXT)
    if not tracks:
        return []
    keys = [(t, name_key(t)) for t in tracks]
    used = set()
    out = []
    for video in videos:
        vkey = name_key(video)
        # Score every candidate, take the best, and pair nothing when two look equally right:
        # a file's own episode number is a strong hint, but a folder can hold two series that
        # both have an "05", and a wrong pair is worse than none.
        best, best_score, tie = None, 0, False
        for track, tkey in keys:
            if track in used or not same_episode(vkey, tkey):
                continue
            if tkey == vkey:
                score = 3                     # the names are the same, bar the language tag
            elif shared_words(vkey, tkey):
                score = 2                     # same episode number and a shared title word
            else:
                score = 1                     # only the number lines up
            if score > best_score:
                best, best_score, tie = track, score, False
            elif score == best_score and track != best:
                tie = True                    # two tracks look equally right: don't choose
        if best is not None and not tie:
            used.add(best)
            out.append((video, best))
    return out


def find_subtitles(video):
    """Subtitle files next to a video whose name starts with the video's name."""
    folder = os.path.dirname(video)
    stem = os.path.splitext(os.path.basename(video))[0].lower()
    out = []
    try:
        for e in os.scandir(folder):
            if e.is_file() and ext(e.name) in SUB_EXT and e.name.lower().startswith(stem):
                out.append(e.path)
    except OSError:
        pass
    return sorted(out)
