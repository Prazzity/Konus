"""The main window: a frameless shell with the redesign's title bar, the player (video or
music view + control bar, with the library sidebar floating over it) and the full library
with its mini player. It also owns the floating panels, overlays, menus and shortcuts,
and is the `actions` object every bar element talks to."""

import ctypes
import json
import math
import os
import re
import time
import webbrowser
from urllib.parse import unquote, urlparse

from PyQt5.QtCore import (QAbstractAnimation, QByteArray, QEasingCurve, QElapsedTimer, QEvent,
                            QObject, QPoint, QPointF, QPropertyAnimation, QRect, QSize, Qt, QTimer,
                            QUrl, pyqtSignal)
from PyQt5.QtGui import QColor, QCursor, QIcon, QKeySequence, QPainter, QPixmap, QRegion
from PyQt5.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QInputDialog, QMenu, QShortcut,
                             QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from . import APP_NAME, diag, i18n, icons, prefs, sysutil, tasks, theme, wm
from .actions import ACTION, ACTIONS, WINDOW_WIDE
from .i18n import tr, trn
from .controls import NEEDS, SeekPreview, TitleBar
from .dialogs import (AboutDialog, AddSubtitlesDialog, CaptureDialog, ConvertDialog, DiscDialog, MessageDialog,
                      NetworkDialog, PlaylistDialog, PropertiesDialog, ShortcutsDialog, StreamDialog)
from .engine import (AUDIO_EXT, MEDIA_EXT, PLAYLIST_EXT, SUB_EXT, VIDEO_EXT, display_name, ext, fmt_time, is_audio,
                     is_url, media_in_dir)
from .library import FullLibrary, Library, SidebarLibrary
from .media import MediaService, find_subtitles, match_audio_tracks
from .miniplayer import MiniPlayer, PipWindow
from .panels import PANEL_MAX_SHARE, PANEL_W, QueuePanel, SettingsPanel, TracksPanel
from .player import OverlayWindow, PlayerArea, StatsBox
from .theme import c
from .widgets import OSD, Hint, MenuItem, PopupMenu, Toast, clamp_position, esc, sep
from .filedialog import FileDialog


def _above_safe(widget, on):
    """set_above() a moment after the change; the window may be gone by then (a language switch
    rebuilds it), and winId() on a deleted C/C++ object raises inside a timer slot, which PyQt
    answers by aborting the process."""
    try:
        sysutil.set_above(widget, on)
    except (RuntimeError, TypeError):
        pass

FRAME = 5

# Qt matches shortcuts by the character the *active layout* produces, so with a Russian
# layout every Latin binding is dead: the ] key types «ъ» and Qt reports Key_Ъ instead of
# Key_BracketRight. This table maps the physical key (the virtual key Windows puts in the
# event) to the key a US layout would produce, so bindings work whatever the layout types.
_VK_TO_QT = {}
for _i in range(26):                                   # A..Z
    _VK_TO_QT[0x41 + _i] = int(Qt.Key_A) + _i
for _i in range(10):                                   # 0..9
    _VK_TO_QT[0x30 + _i] = int(Qt.Key_0) + _i
_VK_TO_QT.update({
    0xBA: int(Qt.Key_Semicolon), 0xBB: int(Qt.Key_Equal), 0xBC: int(Qt.Key_Comma),
    0xBD: int(Qt.Key_Minus), 0xBE: int(Qt.Key_Period), 0xBF: int(Qt.Key_Slash),
    0xC0: int(Qt.Key_QuoteLeft), 0xDB: int(Qt.Key_BracketLeft), 0xDC: int(Qt.Key_Backslash),
    0xDD: int(Qt.Key_BracketRight), 0xDE: int(Qt.Key_Apostrophe),
})
# (virtual key, shift held) -> the character that key types on a US layout. Bindings like ">"
# or "<" are characters, and with a Cyrillic layout Qt never produces them, so the physical key
# has to be translated into the character the binding was written with.
_VK_TYPED = {
    (0xBE, False): ".", (0xBE, True): ">",
    (0xBC, False): ",", (0xBC, True): "<",
    (0xBA, False): ";", (0xBA, True): ":",
    (0xBB, False): "=", (0xBB, True): "+",
    (0xBD, False): "-", (0xBD, True): "_",
    (0xBF, False): "/", (0xBF, True): "?",
    (0xC0, False): "`", (0xC0, True): "~",
    (0xDB, False): "[", (0xDB, True): "{",
    (0xDD, False): "]", (0xDD, True): "}",
    (0xDC, False): "\\", (0xDC, True): "|",
    (0xDE, False): "'", (0xDE, True): '"',
}


def _needs_scan(items):
    """True when the list holds a folder (or a playlist file) that must be walked:
    expanding those can take seconds on big or network folders, so it runs in the
    background (see konus.tasks) instead of the GUI thread."""
    for i in items:
        if is_url(i):
            continue
        p = unquote(urlparse(i).path) if i.startswith("file://") else i
        if p[:1] == "/" and p[2:3] == ":":
            p = p[1:]  # file:///C:/x -> C:/x
        if os.path.isdir(p) or ext(p) in PLAYLIST_EXT:
            return True
    return False



class Shell:
    """The main window, in a form the app can replace.

    A language switch has to build the interface again, because its words enter the widgets as
    they are made (see konus.i18n) — and that means a new MainWindow. Everything around it (the
    entry point's arguments, the single-instance server) should keep talking to the window on
    screen, so it talks to this: `shell.win` is always the current one. The engine, the store
    and playback are not the window's and are untouched by a swap.
    """

    def __init__(self, engine, store):
        self.engine = engine
        self.store = store
        self.win = self.build()

    def build(self):
        win = MainWindow(self.engine, self.store)
        win.rebuild_ui = self.reload
        return win

    def reload(self, reopen=None):
        """Put a freshly built window where the old one stood, and let the old one go.

        `reopen` is a Settings category the switch was asked from: that dialog was the old
        window's and went with it, so it opens again on the new one."""
        old = self.win
        state = old.take_state()
        old.hide()                             # the new window comes up where it stood
        self.win = self.build()                # building it hands libVLC its video window
        self.win.restore_state(state)
        old.dispose()
        old.deleteLater()
        if reopen:
            self.win.open_settings(reopen)
        return self.win


class _GeometryDriver(QObject):
    """The window's geometry from one rectangle to another, a frame per fixed step of a clock.

    A QPropertyAnimation waits for the next frame only once the current one has been drawn, so
    the frame after a cheap one comes 16 ms later and the frame after an expensive one 16 ms
    plus whatever it cost: measured on the real window, the steps came at 19, 25, 28, 41, 44 ms.
    The geometry was right in every one of them — it is that unevenness that the eye reads as a
    jerk, and no amount of shortening the animation removes it. Here frame n is due at
    `start + n * interval` whatever the frame before it cost, so the steps come evenly and the
    motion is smooth even when the machine can only afford a coarse rate.

    Two rules keep it honest. A frame that does not fit inside its step starts the count again
    instead of pulling a burst of catch-up frames after it — the animation lasts as long as it
    was told to either way, because the geometry follows the clock rather than a frame counter.
    And the step is not guessed: it starts at 16 ms (60 frames a second, which is what a screen
    can show at all) and every frame measures what it cost, so a machine that cannot hold that
    rate lengthens the step in its very first animation rather than after a few. The step comes
    out right the first time the window is grown, and stays there.

    What the step should be is kept on the window, not on the driver: what one animation learned
    about this machine is what the next one starts from.
    """

    MIN_MS, MAX_MS, STEP_MS = 16, 50, 4
    #: What the paint that follows a frame costs, plus the compositor's own time — the part of a
    #: frame the measurement above cannot see, because it happens after the frame's work is done.
    HEADROOM_MS = 6
    #: Frames that have to fit before the step is shortened again.
    CLEAN_FRAMES = 2

    def __init__(self, window, target, ms, done):
        super().__init__(window)
        self.win = window
        self.start_rect = QRect(window.geometry())
        self.target = QRect(target)
        self.ms = max(1, ms)
        self.done = done
        self.interval = max(self.MIN_MS, min(self.MAX_MS, window._anim_interval))
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.clock = QElapsedTimer()
        self.origin = 0                       # ms already spent before the clock was restarted
        self.due_n = 0                        # which step of the clock the next frame belongs to
        self.overran = False
        self.clean = 0                        # frames in a row that fitted inside their step

    def start(self):
        self.clock.start()
        self._tick()

    def _rect(self, progress):
        v = self.win.CURVE.valueForProgress(progress)
        s, e = self.start_rect, self.target
        return QRect(int(round(s.x() + (e.x() - s.x()) * v)),
                     int(round(s.y() + (e.y() - s.y()) * v)),
                     int(round(s.width() + (e.width() - s.width()) * v)),
                     int(round(s.height() + (e.height() - s.height()) * v)))

    def _tick(self):
        began = time.perf_counter()
        elapsed = self.origin + self.clock.elapsed()
        progress = min(1.0, elapsed / self.ms)
        self.win.setGeometry(self._rect(progress))
        cost = (time.perf_counter() - began) * 1000
        if progress >= 1.0:
            self.win._learn_rate(self.overran)
            self.done()
            return
        self.due_n += 1
        due, now = self.due_n * self.interval, self.clock.elapsed()
        if due <= now:
            # this frame did not fit its step: the next one gets a whole step of its own rather
            # than the animation firing a burst of frames to catch up with the clock, which
            # would look exactly like the jerk this class exists to remove. It also means the
            # step was too short for this machine — what it costs is measured below, so there is
            # nothing to learn from the miss here beyond that.
            self.overran = True
            self.origin += now
            self.clock.restart()
            self.due_n, due, now = 0, self.interval, 0
        self._respace(cost)
        self.timer.start(max(1, due - now))

    def _respace(self, cost):
        """Set the step for the frames still to come from what this frame cost.

        The step has to hold everything a frame brings: the resize with its layout and its
        native child, the paint the system asks for afterwards, and the compositor's own time.
        The frame measures itself, so the paint that follows it is covered by HEADROOM_MS, and a
        frame that was late despite that lengthens the step at once — the animation is at the
        right rate before it is half over, on the first growth after the application started,
        rather than after a few of them.

        Shortening goes the other way: only after CLEAN_FRAMES frames that fitted, and only down
        to a floor this machine has been seen to miss. Without the floor the step would fall back
        to 16 ms as soon as a cheap frame came along and rise again on the next late one, and a
        rate that keeps changing is a rate that keeps jerking."""
        want = self._step_round(cost + self.HEADROOM_MS)
        if self.overran:
            self.win._anim_floor = max(self.win._anim_floor,
                                       min(self.MAX_MS, self.interval + self.STEP_MS))
            self.clean = 0
        if want > self.interval:
            self.interval = min(self.MAX_MS, want)
        elif self.clean >= self.CLEAN_FRAMES:
            self.interval = max(self.win._anim_floor, self.MIN_MS, want,
                                self.interval - 2 * self.STEP_MS)
            self.clean = 0
        else:
            self.clean += 1

    def _step_round(self, ms):
        """The next step at or above `ms`, in whole steps — a step is either 16 ms or 24, not 19."""
        return max(self.MIN_MS, min(self.MAX_MS,
                                    ((int(ms) + self.STEP_MS - 1) // self.STEP_MS) * self.STEP_MS))


class MainWindow(QWidget):
    def __init__(self, engine, store):
        # WindowMinimizeButtonHint draws nothing (the window is frameless) but it makes Windows
        # set WS_MINIMIZEBOX, which is what the shell checks before turning a click on the
        # taskbar button into "minimize the active window".
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowMinimizeButtonHint)
        self.engine = engine
        self.store = store
        self.media = MediaService()
        self.engine.media = self.media     # the queue reads cached durations from it
        self.store.saveFailed.connect(self._on_save_failed)
        if self.store.newer_version():
            # a config from a newer player: said once, after the window is up
            QTimer.singleShot(1500, lambda: self._on_newer_config(self.store.newer_version()))
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(icons.app_pixmap(128)))
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        QApplication.instance().installEventFilter(self)     # see eventFilter: the edge cursor
        # the control bar needs 780 px of its own (seek + transport + volume): a narrower
        # window squeezed its elements on top of each other
        self.setMinimumSize(800, 420)
        #: How long one frame of the maximize/restore animation may take, in ms, and the
        #: shortest step this machine has been seen to miss. Both are measured by the animations
        #: themselves (see _GeometryDriver) and kept here, so the next one starts from what this
        #: machine turned out to be able to draw instead of finding out again.
        self._anim_interval = _GeometryDriver.MIN_MS
        self._anim_floor = _GeometryDriver.MIN_MS
        self.mode = "player"
        self.hide_ui = False
        self.pip = None
        # Set by restore_state on a window a rebuild brought back, and None on a fresh one: what
        # that window was showing (see take_state). Two things start differently on a rebuild —
        # the interface is not hidden again over the mode it was in, and Settings is reopened.
        self._state = None
        # Set by the shell the window was built through (see konus.window.Shell): builds this
        # window again, in place, for a language switch. Without one the switch waits for the
        # next start (see apply_language).
        self.rebuild_ui = None
        self.panels = {}
        self.open_panel = None
        self._fs_bars = {}
        self._scrub_last = 0
        self._scrub_applied = None     # fraction the live scrub already seeked to
        self._geo_anim = None          # running maximize/restore animation
        self._normal_geo = None        # size to come back to (see _maximize_animated)
        self._fullscreen_from = QRect()  # the size the window went into fullscreen with
        self._was_max = False
        self._cursor_hidden = False
        self._last_move = 0
        self._play_token = 0             # invalidates in-flight background folder scans
        self._enqueue_token = 0
        self.library = Library(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(FRAME, FRAME, FRAME, FRAME)
        root.setSpacing(0)
        self.titlebar = TitleBar(self)
        root.addWidget(self.titlebar)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        # player page: the player area (the library sidebar floats over it)
        self.player_page = QWidget()
        ph = QHBoxLayout(self.player_page)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.setSpacing(0)
        # The library sidebar floats over the left of the video (like the right-hand panels),
        # so opening it never resizes the video.
        self.compact = SidebarLibrary(self.library)
        self.compact.setParent(self, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.compact.setAttribute(Qt.WA_TranslucentBackground)
        self.compact.hide()
        self.sidebar_open = False
        self.player = PlayerArea(self)
        ph.addWidget(self.player, 1)
        self.pages.addWidget(self.player_page)
        # library page
        self.library_page = QWidget()
        lv = QVBoxLayout(self.library_page)
        lv.setContentsMargins(0, 0, 0, 0)
        self.full = FullLibrary(self.library)
        lv.addWidget(self.full)
        self.pages.addWidget(self.library_page)
        self.mini = MiniPlayer(self, self.library_page)
        self.mini.hide()

        self.video = self.player.video
        self.engine.set_window(int(self.video.winId()))

        self.toast = Toast(self)
        self.toast.anchor = self._toast_anchor
        self.osd = OSD(self)
        self.osd.anchor = self._osd_anchor
        self.preview = SeekPreview()
        self.hint = Hint()              # the app's tooltip: a card that fades in
        self._prefilled = None          # the file whose preview strip is already being cut
        self._seek_pending = None       # wheel seeks waiting to be flushed as one seek
        self._seek_timer = QTimer(self, singleShot=True, timeout=self._flush_wheel_seek)
        self._seek_idle_timer = QTimer(self, singleShot=True, timeout=self._end_wheel_seek)
        self._wheel_tokens = float("inf")   # the rate limiter clamps this to its burst size
        self._wheel_tokens_at = time.monotonic()
        self.stats = StatsBox(self)
        self._floating_ready = True       # from here on _reposition_floating may run
        self._stats_timer = QTimer(self, interval=1000, timeout=self._update_stats)
        self._overlay_timer = QTimer(self, interval=80, timeout=self._overlay_tick)
        self._hide_timer = QTimer(self, singleShot=True, timeout=self._hide_overlays)
        self._cursor_timer = QTimer(self, singleShot=True, interval=2200, timeout=self._hide_cursor)
        # Repeats the video surface's cursor for a moment after a rebuild (see _on_rebuilt).
        self._cursor_settle = QTimer(self, interval=100, timeout=self._settle_cursor)
        self._cursor_settle_left = 0

        self._wire_surfaces()
        self._wire_engine()
        self.store.layoutChanged.connect(self.rebuild_bars)
        self.store.settingChanged.connect(self._on_setting)
        self.store.libraryChanged.connect(self._on_library)
        self.media.waveReady.connect(lambda p: p == self.engine.current and self._update_seek_modes())
        self.media.cuesReady.connect(lambda _k: self._update_seek_modes())
        self.media.previewReady.connect(self._preview_ready)
        self.media.thumbReady.connect(lambda p: p == self.engine.current and self._update_title())
        self.library.locationChanged.connect(self._on_location)

        self.rebuild_bars()
        self._restore_geometry()
        self._load_normal_geometry()
        self.apply_shortcuts()
        self._setup_tray()
        self._apply_window_settings()
        self.show_library_window() if not engine.items else self.show_player()
        self._update_title()
        self._sync_all()
        if store.get("compact_start"):
            QTimer.singleShot(0, self._start_hidden)

    # ------------------------------------------------------------------ wiring
    def _wire_surfaces(self):
        for surf in (self.player.video, self.player.music, self.player.blank):
            surf.clicked.connect(self._surface_click)
            surf.doubleClicked.connect(self._surface_double)
            surf.wheeled.connect(self._surface_wheel)
            surf.middleClicked.connect(self._surface_middle)
            surf.moved.connect(self._surface_moved)
            surf.contextMenu.connect(self.open_context_menu)

    def _on_save_failed(self):
        self.notify(tr("Couldn't save the settings file — is it locked or read-only?"), timeout=5000)

    def _on_newer_config(self, version):
        self.notify(tr("This settings file was written by a newer version of the player "
                       "(v{v}); it is left untouched and nothing is saved while it is there."
                       ).format(v=version), timeout=8000)

    def _wire_engine(self):
        e = self.engine
        e.stateChanged.connect(self._on_state)
        e.tick.connect(self._on_tick)
        e.mediaChanged.connect(self._on_media)
        e.metaChanged.connect(self._on_meta)
        e.videoChanged.connect(self._on_video)
        e.volumeChanged.connect(self._on_volume)
        e.modesChanged.connect(self._sync_all)
        e.message.connect(self._on_message)
        e.error.connect(self.error)
        e.resumed.connect(self._on_resumed)
        e.fxChanged.connect(self._on_fx)
        e.rebuilt.connect(self._on_rebuilt)
        e.recordingChanged.connect(self._sync_on_change)
        e.rateChanged.connect(self._sync_on_change)
        e.playlistChanged.connect(self._sync_all)

    def _sync_on_change(self, *_ignored):
        # one slot for the signals that carry a value nobody reads: bound to this window, so Qt
        # drops it when a rebuilt window replaces this one (see konus.window.Shell)
        self._sync_all()

    # ------------------------------------------------------------------ bars
    def bars(self):
        out = [self.titlebar, self.player.bar]
        out += [w.bar for w in self._fs_bars.values()]
        return out

    def rebuild_bars(self):
        layout = self.store.layout()
        if self.mode == "library":
            lib_layout = {"title_left": ["menu"], "title_center": ["title"],
                          "title_right": [e for e in layout.get("title_right", []) if e in
                                          ("minimize", "maximize", "fullscreen", "close")] or
                                         ["minimize", "maximize", "fullscreen", "close"]}
            self.titlebar.build(lib_layout)
        else:
            self.titlebar.build(layout)
        rows = self._rows_for_media(layout["rows"])
        self.player.bar.build(rows)
        for kind, ow in self._fs_bars.items():
            if kind == "top":
                ow.bar.build(layout)
            else:
                ow.bar.build(rows)
            ow.adjustSize()
        self._sync_all()
        self._update_title()
        self._update_seek_modes()
        self._layout_bars_visibility()

    def _rows_for_media(self, rows):
        """Music: back/forward 15 s around play, no subtitles button (design's music bar)."""
        cur = self.engine.current
        if not cur or not is_audio(cur) or self.engine.has_video:
            return rows
        out = []
        for row in rows:
            row = [x for x in row if x != "subtitles"]
            if "play" in row and not any(x in row for x in ("back10", "fwd10", "back15", "fwd15")):
                i = row.index("play")
                row = row[:i] + ["back15", "play", "fwd15"] + row[i + 1:]
            out.append(row)
        return out

    def _elements(self, eid):
        out = []
        for b in self.bars():
            try:
                out.extend(b.elements(eid))
            except RuntimeError:
                pass
        return out

    def _sync_all(self):
        e = self.engine
        playing = e.is_playing()
        for w in self._elements("play"):
            w.set_playing(playing)
        for w in self._elements("volume"):
            w.set_volume(e.volume, e.muted, e.max_volume())
        cur = e.current
        fav = bool(cur and self.store.is_favorite(cur))
        for w in self._elements("favorite"):
            w.set_active(fav)
        for w in self._elements("repeat"):
            w.set_active(e.repeat != "off")
            w.setToolTip(tr({"off": "Repeat: off", "all": "Repeat: all",
                             "one": "Repeat: this item"}[e.repeat]))
        for w in self._elements("shuffle"):
            w.set_active(e.shuffle)
        for w in self._elements("record"):
            w.set_active(e.is_recording())
        for w in self._elements("speed"):
            w.set_rate(e.fx["speed"])
        for w in self._elements("sidebar"):
            w.set_active(self.sidebar_open)
        for w in self._elements("pip"):
            w.set_active(self.pip is not None)
        for w in self._elements("fullscreen"):
            w.set_icon("fullscreen", "active" if self.isFullScreen() else "")
        for w in self._elements("maximize"):
            w.setToolTip(tr("Restore") if self.isMaximized() else tr("Maximize"))
        panel_state = {"audio": ("tracks", "audio"), "subtitles": ("tracks", "subtitles"), "settings": ("settings", None),
                       "queue": ("queue", None), "chapters": ("queue", "chapters"), "bookmarks": ("queue", "bookmarks")}
        for eid, (kind, tab) in panel_state.items():
            p = self.panels.get(kind)
            on = bool(p and p.isVisible() and (tab is None or p.current_tab() == tab))
            for w in self._elements(eid):
                w.set_active(on)
        folder = os.path.basename(os.path.dirname(cur)) if cur and not is_url(cur) else ""
        for w in self._elements("folder"):
            w.set_text(folder or ("Videos" if not cur else "Network"), "folder")
        for w in self._elements("folder_name"):
            w.set_text(folder)
        for w in self._elements("file_name"):
            w.set_text(display_name(cur) if cur else "")
        for w in self._elements("artist_name"):
            w.set_text(e.artist())
        pl = next((p for p in self.store.playlists() if cur in p["items"]), None) if cur else None
        for w in self._elements("playlist_name"):
            w.set_text(pl["name"] if pl else "")
        for w in self._elements("thumbnail"):
            pm = self.media.thumb(cur, 50, 28, self.devicePixelRatioF()) if cur else None
            w.set_pixmap(pm)
        t, length = e.time()
        self._update_time(t, length)
        self._update_title()
        self._update_element_states()

    def _update_element_states(self):
        """Put the elements that act on what is playing out of reach while there is nothing to act
        on (see controls.NEEDS).

        With no item open the bars are still there — in fullscreen they are both the way out and
        the way in — and every control on them used to work: the seek bar scrubbed, the volume and
        the track buttons did what they do to a film that was not open. The item asked about is the
        one that is *loaded*, not the one that is playing: a stopped film can still be seeked (the
        engine restarts it there) and a paused one has to be."""
        e = self.engine
        alive = {"item": e.current is not None, "queue": bool(e.items)}
        for eid, need in NEEDS.items():
            for w in self._elements(eid):
                # The Customize dialog builds these same bars and marks their elements as its own
                # (see konus.customize): they are dragged there, not used, and nothing is playing
                # while the layout is being arranged, so they would be dead under the pointer.
                w.setEnabled(True if getattr(w, "customizing", False) else alive[need])

    def _update_time(self, t, length):
        for w in self._elements("current_time"):
            w.set_time(t, length)
        for w in self._elements("duration"):
            w.set_time(t, length)
        if not self.engine._last or length <= 0:
            frac = 0
        else:
            frac = t / length
        for w in self._elements("seek"):
            w.seek.set_value(frac)
            w.seek.length = length

    def _update_title(self):
        e = self.engine
        show = self.store.get("title_filename")
        text = e.file_title() if (e.current and show) else APP_NAME
        for w in self._elements("title"):
            w.set_text(text)
        self.setWindowTitle(text if e.current else APP_NAME)

    def _update_seek_modes(self):
        e = self.engine
        cur = e.current
        peaks = None
        cues = None
        if cur and not is_url(cur):
            wave_on = e.fx["waveform"] or (is_audio(cur) and not e.has_video)
            if wave_on:
                peaks = self.media.waveform(cur)
            if e.fx["sub_timeline"] and e.has_video:
                ext_subs = self.store.external_subs(cur) or find_subtitles(cur)
                cues = self.media.cues(cur, ext_subs[0] if ext_subs else None)
        shift = int(e.fx["sub_delay"] * 1000)
        for w in self._elements("seek"):
            w.seek.peaks = peaks
            w.seek.cues = [(a + shift, b + shift) for a, b in cues] if cues else None
            w.seek.length = e.time()[1]
            w.set_timeline(bool(e.fx["sub_timeline"] and e.has_video))
            marks = []
            length = e.time()[1]
            if cur and length > 0:
                marks = [b["ms"] / length for b in self.store.bookmarks(cur)]
                chs = getattr(self, "_chapters", [])
                w.seek.chapters = [t / length for _, _, t in chs if t > 0]
            w.seek.marks = marks
            w.seek.update()
        if self.player.music.isVisible() or e.fx["visualizer"] == "Waveform":
            t, length = e.time()
            self.player.music.set_wave(self.media.waveform(cur) if cur and not is_url(cur) else None,
                                       t / length if length else 0, e.fx["visualizer"] == "Waveform")

    # ------------------------------------------------------------------ engine events
    def _on_state(self, state):
        self._sync_all()
        if state in ("stopped", "error", "opening", "playing"):
            self._update_surface()
        if self.store.get("icon_change"):
            self._update_icon(state)
        if state in ("stopped", "ended") and getattr(self.engine, "finished_all", False):
            self.engine.finished_all = False
            what = self.store.get("when_finished")
            if what == "Quit":
                QTimer.singleShot(300, self.quit)
            elif what == "Show the library":
                self.show_library_window()

    def _on_tick(self, t, length):
        self._update_time(t, length)
        if self.engine.fx["visualizer"] == "Waveform" and self.player.music.isVisible():
            self.player.music.set_wave(self.player.music.peaks, t / length if length else 0, True)

    def _on_media(self):
        e = self.engine
        cur = e.current
        self._chapters = []
        self._seek_pending = None            # a wheel seek must not land in the next file
        self._seek_timer.stop()
        self._seek_idle_timer.stop()
        self.rebuild_bars() if cur and is_audio(cur) != getattr(self, "_bar_audio", None) else self._sync_all()
        self._bar_audio = bool(cur and is_audio(cur))
        self._update_surface()
        self._update_seek_modes()
        self._update_title()
        if not cur:
            return
        if self.mode != "library" and e.has_video and self.store.get("start_fullscreen") and not self.isFullScreen():
            QTimer.singleShot(300, self.enter_fullscreen)
        raise_on = self.store.get("raise_on_play")
        kind = "Audio" if is_audio(cur) else "Video"
        if raise_on == "Both" or raise_on == kind:
            if self.isMinimized():
                self._unminimize()
            self.raise_()
        if self.store.get("osd_filename") and not is_audio(cur):
            QTimer.singleShot(400, self._show_filename)
        QTimer.singleShot(1500, self._load_chapters)
        self._resize_to_video_pending = True

    def _show_filename(self):
        e = self.engine
        if self.mode == "player" and e.current and e.has_video and self.pip is None:
            self.osd.show_text(e.file_title(), "", self.store.get("osd_filename_duration"), big=False)

    def _load_chapters(self):
        self._chapters = self.engine.chapters()
        self._update_seek_modes()

    def _on_meta(self):
        self._update_title()
        self._update_surface()
        self._sync_all()

    def _on_video(self, has_video):
        if self.engine.current and is_audio(self.engine.current):
            self.rebuild_bars()
        self._update_surface()
        if has_video and getattr(self, "_resize_to_video_pending", False):
            self._resize_to_video_pending = False
            QTimer.singleShot(600, self._resize_to_video)

    def _on_volume(self, v, muted):
        for w in self._elements("volume"):
            w.set_volume(v, muted, self.engine.max_volume())

    def _on_message(self, text, key=""):
        text = tr(text)
        if self.store.get("osd_feedback") and self.mode == "player" and self.isVisible():
            self.osd.show_text(text, key or getattr(self, "_last_key", ""), self.store.get("osd_duration"))
        elif self.isVisible():
            # the OSD belongs to the video surface: with the library on screen (or the OSD off)
            # a message — "Nothing playable there" for a file that is gone — was lost entirely
            self.toast.show_message(esc(text))
        self._last_key = ""

    def _on_resumed(self, ms, mode):
        if mode == "auto":
            self.toast.show_message(tr("Continued from <b>{time}</b>").format(time=fmt_time(ms)),
                                    tr("Start Over"), 6000)
            self.toast.action.connect(self.engine.restart)
        else:
            self.toast.show_message(tr("You already watched till <b>{time}</b>").format(time=fmt_time(ms)),
                                    tr("Continue Watching"), 8000)
            self.toast.action.connect(lambda: self.engine.continue_from(ms))

    def _on_fx(self, key):
        if key in ("sub_timeline", "waveform", "sub_delay", ""):
            self._update_seek_modes()
        if key in ("statistics", ""):
            self._toggle_stats(self.engine.fx["statistics"])
        if key in ("visualizer", ""):
            self._update_surface()
        if key in ("speed", ""):
            self._sync_all()

    def _on_rebuilt(self):
        self.engine.set_window(int(self.video.winId()))
        # The rebuild destroys the old libVLC video window while it sits under the pointer —
        # the click that changed a video filter lands on the picture — and the new output
        # window takes its place a moment later. Windows puts its wait cursor up each time a
        # window appears or goes under a stationary pointer, and only replaces it when the
        # pointer enters another window: it was left spinning until the pointer was moved off
        # the picture. The new window is only up after this signal (on the test clip it was
        # there some 200 ms later, and has_vout reported it later still), so the reset is
        # repeated for a moment rather than done once here.
        self._cursor_settle_left = 16               # 1.6 s at the timer's 100 ms
        self._reset_video_cursor()
        self._cursor_settle.start()

    def _settle_cursor(self):
        self._reset_video_cursor()
        self._cursor_settle_left -= 1
        if self._cursor_settle_left <= 0:
            self._cursor_settle.stop()

    def _reset_video_cursor(self):
        # Qt tells Windows about a widget's cursor only when the value it keeps changes, so
        # setting the arrow alone would be a no-op after the first time; setting it and then
        # dropping back to the inherited cursor makes each call apply again. An override
        # cursor (the pointer hidden over the fullscreen picture) still wins over this.
        self.video.setCursor(Qt.ArrowCursor)
        self.video.unsetCursor()

    def _on_library(self, what):
        if what in ("favorites", "bookmarks"):
            self._sync_all()
            self._update_seek_modes()

    def _on_location(self):
        pass

    def _on_setting(self, key, value):
        if key == "language":
            # Any path that changes the stored language rebuilds the interface: the Settings
            # dialog's own dropdown does it itself (see the reopen path below), so while that
            # dialog is on screen this only happens for the other paths — Reset All, a profile
            # switch. One rebuild, deferred: the widget that sent the change is still closing.
            want = i18n.code_for(value or "")
            if want == "system":
                want = i18n.detect_system()
            if want == i18n.language():
                return
            from .settings import SettingsDialog
            if any(isinstance(w, SettingsDialog) and w.isVisible() for w in QApplication.topLevelWidgets()):
                return
            QTimer.singleShot(0, self.apply_language)
            return
        if key in ("theme", "accent"):
            self.apply_theme()
        elif key in ("title_filename",):
            self._update_title()
        elif key in ("on_top", "window_opacity", "decorations", "tray_icon"):
            self._apply_window_settings()
        elif key in ("autohide", "fs_controls"):
            self._layout_bars_visibility()
        elif key == "max_volume":
            self._sync_all()
        elif key in ("use_library",):
            self.sidebar_open = False
            self._sync_compact()
        elif key in ("custom_aspects", "custom_crops"):
            for p in self.panels.values():
                p.deleteLater()
            self.panels = {}

    # ------------------------------------------------------------------ surfaces / video placement
    def _update_surface(self):
        e = self.engine
        cur = e.current
        show_music = bool(cur) and not e.has_video and e.fx["visualizer"] not in ("Spectometer", "Scope", "Spectrum")
        if self.pip is not None and cur:
            self.player.music.set_media(
                "", tr("Playing in picture in picture. Double-click it to come back."))
            self.player.stack.setCurrentWidget(self.player.music)
        elif show_music:
            cover = e.artwork() or (self.media.cover_path(cur) if not is_url(cur) else "")
            text = e.file_title()
            if e.artist():
                text = f"{e.file_title()} - {e.artist()}"
            self.player.music.set_media(cover, text)
            self.player.stack.setCurrentWidget(self.player.music)
        elif self.player.video.parentWidget() is self.player.stack:
            idle = not cur or e.state in ("stopped", "error")
            self.player.stack.setCurrentWidget(self.player.blank if idle else self.player.video)
        self._place_video()

    def _place_video(self):
        v = self.video
        e = self.engine
        target = "player"
        if self.pip is not None:
            target = "pip"
        elif self.mode == "library" and e.current and (e.is_active() or e.state == "paused"):
            target = "mini"
        cur_parent = v.parentWidget()
        if target == "pip":
            if cur_parent is not self.pip:
                self.mini.detach()
                self.pip.attach(v)
        elif target == "mini":
            if cur_parent is not self.mini.holder:
                self.mini.attach(v)
        else:
            if cur_parent is not self.player.stack:
                self.mini.detach()
                self.player.stack.insertWidget(0, v)
                if self.engine.has_video or not self.engine.current:
                    self.player.stack.setCurrentWidget(v)
                v.set_radius(9 if self.player.frame_lay.contentsMargins().left() else 0)
        self.place_mini()

    def place_mini(self):
        e = self.engine
        show = self.mode == "library" and bool(e.current) and self.pip is None
        self.mini.setVisible(show)
        if not show:
            return
        self.mini.refresh()
        par = self.library_page.rect()
        self.mini.fit_height(par.height())
        corner = self.store["mini_corner"]
        w, h = self.mini.width(), self.mini.height()
        # clamped into the page: a corner the card does not fit in (a small window with the
        # queue open) used to push it out of the window altogether
        x = max(0, 0 if "l" in corner else par.width() - w)
        y = max(0, 0 if "t" in corner else par.height() - h)
        self.mini.move(x, y)
        self.mini.raise_()

    def close_mini(self):
        self.engine.stop()
        self.engine.clear()
        self.place_mini()

    # ------------------------------------------------------------------ modes
    def show_player(self):
        if self.mode != "player":
            self.mode = "player"
            self.pages.setCurrentWidget(self.player_page)
            self.rebuild_bars()
        self._place_video()
        self._layout_bars_visibility()
        self._sync_compact()

    def show_library_window(self, loc=None):
        if loc:
            self.library.navigate(loc)
        if self.isFullScreen():
            self.exit_fullscreen()
        if self.mode != "library":
            self.mode = "library"
            self.pages.setCurrentWidget(self.library_page)
            self.osd.hide()
            self.stats.hide()
            self.close_panels()
            self.rebuild_bars()
        self._place_video()
        self._layout_bars_visibility()
        self._sync_compact()

    def go_home(self):
        self.show_library_window(("home",))

    def show_library_page(self, key):
        self.show_library_window((key,))

    def toggle_sidebar(self):
        if self.mode == "library":
            self.show_player()
            return
        if not self.store.get("use_library"):
            return
        self.sidebar_open = not self.sidebar_open
        self._sync_compact()
        self._sync_all()

    def _sync_compact(self):
        show = (self.sidebar_open and self.mode == "player" and not self.isFullScreen() and self.isVisible()
                and not self.isMinimized() and self.pip is None)
        if show:
            self._place_compact()
            if not self.compact.isVisible():
                self.compact.show()
            self.compact.raise_()
        elif self.compact.isVisible():
            self.compact.hide()

    def _place_compact(self):
        page = self.player_page
        top_left = page.mapToGlobal(QPoint(0, 0))
        h = page.height() - (self.player.bar.height() if self.player.bar.isVisible() else 0)
        self.compact.setGeometry(top_left.x(), top_left.y(), self.compact.width(), max(200, h))

    def open_current_folder(self):
        cur = self.engine.current
        before = self.library.loc
        if cur and not is_url(cur):
            target = self.library.show_folder(cur)
        elif cur:
            target = ("network",)
            self.library.navigate(target)
        else:
            target = ("videos",)
            self.library.navigate(target)
        # The chip is the sidebar's own switch: clicking it again while the sidebar already
        # shows this folder closes it, instead of only ever opening it.
        if self.mode == "player":
            if self.sidebar_open and before == target:
                self.toggle_sidebar()
            elif not self.sidebar_open:
                self.toggle_sidebar()

    def _layout_bars_visibility(self):
        """Windowed bars vs overlay bars, depending on fullscreen / hide UI / auto-hide."""
        overlay = self.overlay_mode()
        decorations = self.store.get("decorations")
        in_library = self.mode == "library"
        # in a fullscreen library the title bar stays: it is the visible way back out of it
        self.titlebar.setVisible(in_library or (not self.isFullScreen() and not overlay and decorations))
        self.player.bar.setVisible(not overlay and not in_library)
        if self.isFullScreen():
            self.player.set_inset(0)
        elif overlay and not in_library:
            self.player.set_inset(10, top=10)
        else:
            self.player.set_inset(10, top=1)
        if overlay and not in_library:
            self._overlay_timer.start()
            if self.isFullScreen() and (self.store.get("autohide") == "Never"
                                        or not self._player_has_picture()):
                # nothing is playing: show the bars rather than leave a black screen with no hint
                # of what to do. The pointer moving away hides them as usual.
                self._show_overlays()
        else:
            self._overlay_timer.stop()
            self._hide_overlays(force=True)
            self._show_cursor()
        QTimer.singleShot(0, self._reposition_floating)

    def overlay_mode(self):
        if self.mode != "player":
            return False
        if self.isFullScreen():
            return True
        return self.hide_ui or self.store.get("autohide") == "Always"

    # overlays ------------------------------------------------------------------
    def _ensure_overlays(self):
        for kind in ("top", "bottom"):
            if kind not in self._fs_bars:
                ow = OverlayWindow(self, kind)
                if kind == "top":
                    ow.bar.build(self.store.layout())
                else:
                    ow.bar.build(self._rows_for_media(self.store.layout()["rows"]))
                self._fs_bars[kind] = ow
        self._sync_all()
        self._update_seek_modes()

    def _overlay_geometry(self, kind):
        vr = self.player.video_rect_global()
        if self.isFullScreen():
            vr = self.windowHandle().screen().geometry() if self.windowHandle() else vr
        ow = self._fs_bars[kind]
        h = ow.bar.height() if kind == "top" else ow.bar.height()
        if kind == "top":
            return QRect(vr.left(), vr.top(), vr.width(), 40)
        return QRect(vr.left(), vr.bottom() - h + 1, vr.width(), h)

    def _show_overlays(self, which=("top", "bottom")):
        if not (self.store.get("fs_controls") or not self.isFullScreen()):
            return
        self._ensure_overlays()
        for kind in which:
            ow = self._fs_bars[kind]
            ow.setGeometry(self._overlay_geometry(kind))
            if self.isFullScreen():
                ow.setWindowFlags(ow.windowFlags() | Qt.WindowStaysOnTopHint)
            ow.show()
            ow.raise_()
        self._show_cursor()

    def _hide_overlays(self, force=False):
        if not force:
            if self.open_panel is not None or QApplication.activePopupWidget() is not None:
                self._hide_timer.start(800)
                return
            if self.store.get("autohide") == "Never" and self.isFullScreen():
                return
            pos = QCursor.pos()
            for ow in self._fs_bars.values():
                if ow.isVisible() and ow.geometry().adjusted(0, -20, 0, 20).contains(pos):
                    self._hide_timer.start(400)
                    return
        for ow in self._fs_bars.values():
            ow.hide()
        self.preview.hide()

    def _overlay_tick(self):
        if not self.isActiveWindow() and not any(o.isVisible() for o in self._fs_bars.values()) \
                and self.open_panel is None:
            return
        pos = QCursor.pos()
        vr = self.player.video_rect_global()
        if self.isFullScreen() and self.windowHandle():
            vr = self.windowHandle().screen().geometry()
        if not vr.adjusted(-2, -2, 2, 2).contains(pos):
            if any(o.isVisible() for o in self._fs_bars.values()) and not self._hide_timer.isActive():
                self._hide_timer.start(int(self.store.get("hide_after") * 1000) + 50)
            return
        zone = {0: 40, 1: 90, 2: 160}.get(int(self.store.get("fs_sensitivity")), 90)
        near_top = pos.y() - vr.top() < zone
        near_bottom = vr.bottom() - pos.y() < zone + 54
        if near_top or near_bottom:
            self._hide_timer.stop()
            self._show_overlays([k for k, near in (("top", near_top), ("bottom", near_bottom)) if near])
        elif any(o.isVisible() for o in self._fs_bars.values()):
            if not self._hide_timer.isActive():
                self._hide_timer.start(int(self.store.get("hide_after") * 1000) + 50)

    def show_overlay_bars(self):
        if self.overlay_mode():
            self._show_overlays()
            self._hide_timer.start(2500)

    def _surface_moved(self, gpos):
        if self.isFullScreen() or self.overlay_mode():
            self._show_cursor()
            self._cursor_timer.start()

    def _hide_cursor(self):
        # A menu or a dialog on screen needs the pointer: hiding it there left the user without a
        # cursor over the menu and nothing brought it back (only the video surface does that).
        if QApplication.activePopupWidget() is not None or QApplication.activeModalWidget() is not None:
            return
        if self.isFullScreen() and not any(o.isVisible() for o in self._fs_bars.values()) and self.open_panel is None:
            QApplication.setOverrideCursor(Qt.BlankCursor)
            self._cursor_hidden = True

    def _show_cursor(self):
        if self._cursor_hidden:
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False

    # fullscreen / hide UI / pip ------------------------------------------------------
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def _player_has_picture(self):
        """Whether the player has something to fill the screen with. An idle player is a plain
        black page (see BlankView), and in fullscreen its bars keep out of the way: switching to
        it with nothing to play left the whole screen black."""
        e = self.engine
        return bool(e.current) and e.state not in ("stopped", "ended", "error") and e.is_active()

    def enter_fullscreen(self):
        if self.pip is not None:
            self.toggle_pip()
        # Fullscreen fills the screen with what is on it. Going to the player when there is
        # nothing to play showed a black page instead, so the library comes along — it is the
        # screen the user was looking at, and one can browse and start something from there.
        if self.mode != "player" and self._player_has_picture():
            self.show_player()
        self._was_max = self.isMaximized()
        # the size to come back to when the window is dragged out of fullscreen (see
        # _leave_fullscreen_for_drag): Qt's own idea of "normal" is the fullscreen rectangle here
        self._fullscreen_from = QRect(self.geometry())
        target = self.store.get("fullscreen_screen")
        if target != "Current screen":
            for sc in QApplication.screens():
                if target.startswith(sc.name()) and self.windowHandle():
                    self.windowHandle().setScreen(sc)
                    self.move(sc.geometry().topLeft())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.showFullScreen()
        self._layout_bars_visibility()
        self._sync_all()

    def exit_fullscreen(self, back_to_maximized=True):
        """Leave fullscreen.

        `back_to_maximized` is the state the window comes back to when it was maximized before
        going fullscreen — what Esc and F do. A title-bar drag passes False: the drag takes the
        window out of the maximized state as well, so that the size it is put at is the size it
        keeps (see _leave_fullscreen_for_drag)."""
        self._show_cursor()
        if self._was_max and back_to_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self._layout_bars_visibility()
        self._sync_all()

    def toggle_hide_ui(self):
        self.set_hide_ui(not self.hide_ui)

    def set_hide_ui(self, on):
        """Show or hide the interface over the video. Not a toggle, so a rebuilt window can be
        put back into the mode it was in without guessing (see restore_state)."""
        if self.mode != "player":
            self.show_player()
        if on == self.hide_ui:
            return
        self.hide_ui = on
        self._layout_bars_visibility()
        self.osd.show_text("Interface hidden" if self.hide_ui else "Interface shown",
                           self.key_label("hide_ui") if self.hide_ui else "", 1.5)

    def _start_hidden(self):
        """"Hide the interface at startup" (the compact setting) — but not over a window a
        rebuild brought back to the mode it was already in (see restore_state)."""
        if self._state is None:
            self.set_hide_ui(True)

    def toggle_pip(self):
        if self.pip is None:
            if not self.engine.current or not self.engine.has_video:
                self.notify("Picture in picture needs a video")
                return
            if self.isFullScreen():
                self.exit_fullscreen()
            self.pip = PipWindow(self)
            self.pip.start_move = lambda p=self.pip: wm.start(p)
            self._place_video()
            self._update_surface()
            self.showMinimized() if self.mode == "player" else None
        else:
            pip = self.pip
            self.pip = None
            pip.detach()
            pip.deleteLater()
            if self.isMinimized():
                self._unminimize()
            # Ask for the focus back: the window that returns to the screen should be the one
            # the keyboard is on.
            self.raise_()
            self.activateWindow()
            self._place_video()
            self._update_surface()
        self._sync_all()
        self._sync_compact()

    def escape(self):
        if self.open_panel is not None:
            self.close_panels()
        elif self.isFullScreen():
            self.exit_fullscreen()
        elif self.pip is not None:
            self.toggle_pip()
        elif self.hide_ui:
            self.toggle_hide_ui()

    # ------------------------------------------------------------------ panels
    def panels_queue(self):
        return self._panel("queue")

    def _panel(self, kind):
        p = self.panels.get(kind)
        if p is None:
            p = {"tracks": TracksPanel, "settings": SettingsPanel, "queue": QueuePanel}[kind](self)
            p.closed.connect(lambda k=kind: self._panel_closed(k))
            self.panels[kind] = p
        return p

    def toggle_panel(self, kind, tab=None, anchor=None, force=False):
        p = self._panel(kind)
        if p.isVisible() and not force and (tab is None or p.current_tab() == tab):
            p.hide()
            return
        for k, other in self.panels.items():
            if other is not p and other.isVisible():
                other.hide()
        if tab:
            p.show_tab(tab)
        else:
            p.show_tab(p.current_tab())
        self.open_panel = p
        self._place_panel(p)
        if self.isFullScreen():
            p.setWindowFlags(p.windowFlags() | Qt.WindowStaysOnTopHint)
        p.show()
        p.raise_()
        if self.overlay_mode():
            self._show_overlays(["bottom"])
        self._sync_all()

    def toggle_queue(self):
        self.toggle_panel("queue", "queue")

    def toggle_right_panel(self):
        if self.open_panel is not None:
            self.close_panels()
        else:
            self.toggle_panel(getattr(self, "_last_panel", "queue"))

    def close_panels(self):
        for p in self.panels.values():
            if p.isVisible():
                p.hide()

    def _panel_closed(self, kind):
        if self.open_panel is self.panels.get(kind):
            self._last_panel = kind
            self.open_panel = None
        self._sync_all()

    def _place_panel(self, p):
        vr = self.player.video_rect_global()
        if self.isFullScreen() and self.windowHandle():
            vr = self.windowHandle().screen().geometry()
        bottom = vr.bottom()
        if self.overlay_mode() and "bottom" in self._fs_bars:
            bottom -= self._fs_bars["bottom"].bar.height()
        h = max(260, bottom - vr.top() - 40)
        # Panels ask for the width their content needs (translations are longer than the
        # English they replace); on a small window they fall back to the designed 400 px
        # and their labels elide instead.
        want = max(PANEL_W, p.content_width())
        room = max(PANEL_W, min(vr.width() - 40, int(vr.width() * PANEL_MAX_SHARE)))
        w = min(want, room)
        p.setGeometry(vr.right() - 20 - w + 1, vr.top() + 20, w, h)

    def _reposition_floating(self):
        if not getattr(self, "_floating_ready", False):
            return                       # moveEvent can fire while the window is still being built
        self._sync_compact()
        for p in self.panels.values():
            if p.isVisible():
                self._place_panel(p)
        if self.toast.isVisible():
            self.toast.reposition()
        if self.osd.isVisible():
            self.osd.reposition()
        for kind, ow in self._fs_bars.items():
            if ow.isVisible():
                ow.setGeometry(self._overlay_geometry(kind))
        if self.stats.isVisible():
            vr = self.player.video_rect_global()
            self.stats.move(vr.left() + 20, vr.top() + 20)
        self.place_mini()

    def _toast_anchor(self):
        if self.mode == "library":
            r = self.library_page.rect()
            return self.library_page.mapToGlobal(QPoint(r.center().x(), r.bottom() - 20))
        vr = self.player.video_rect_global()
        bottom = vr.bottom() + 1
        if self.overlay_mode() and any(o.isVisible() for o in self._fs_bars.values()):
            bottom -= 54
        return QPoint(vr.center().x(), bottom)

    def _osd_anchor(self):
        vr = self.player.video_rect_global()
        if self.isFullScreen() and self.windowHandle():
            vr = self.windowHandle().screen().geometry()
        return QPoint(vr.right() - 24, vr.top() + 24 + (40 if self.overlay_mode() and
                                                         self._fs_bars.get("top") and self._fs_bars["top"].isVisible() else 0))

    # stats -----------------------------------------------------------------------------
    def _toggle_stats(self, on):
        if on:
            self._update_stats()
            vr = self.player.video_rect_global()
            self.stats.move(vr.left() + 20, vr.top() + 20)
            self.stats.show()
            self._stats_timer.start()
        else:
            self._stats_timer.stop()
            self.stats.hide()

    def _update_stats(self):
        if self.mode != "player":
            self.stats.hide()
            return
        self.stats.set_rows(self.engine.stats() or {"Statistics": "Nothing playing"})
        if not self.stats.isVisible():
            self.stats.show()

    # ------------------------------------------------------------------ seek bar
    def scrub(self, frac):
        import time as _t
        now = _t.monotonic()
        length = self.engine.time()[1]
        self._update_time(int(frac * length), length)
        if now - self._scrub_last > 0.09:
            self._scrub_last = now
            self.engine.set_position(frac)
            self._scrub_applied = frac

    def seek_fraction(self, frac):
        """The release of a press on the bar. The press already seeks (that is the live scrub,
        so the picture follows the pointer), and seeking a second time to the same place made
        the position jump and then re-anchor — a plain click went to the target twice. Only the
        tail of a drag, which the throttled scrub has not landed on yet, seeks here."""
        applied, self._scrub_applied = self._scrub_applied, None
        length = self.engine.time()[1] or 0
        if applied is not None and abs(frac - applied) * length <= 300:
            return
        self.engine.set_position(frac)

    def seek_hover(self, frac, gpos, bar):
        e = self.engine
        length = e.time()[1]
        if not e.current or length <= 0 or not e.is_active():
            # nothing to show for the moment under the pointer: the card of the previous file
            # used to stay on screen after a Stop
            self.seek_hover_end()
            return
        ms = int(frac * length)
        pm = None
        if self.store.get("seek_thumbs") and e.has_video and not is_url(e.current):
            pm = self.media.preview(e.current, ms, length)
            self._preview_path, self._preview_ms, self._preview_length = e.current, ms, length
            if self._prefilled != e.current:
                # first hover on this file: cut the frames for the whole strip in the
                # background, so the card has them by the time the pointer gets there
                self._prefilled = e.current
                self.media.prefill_previews(e.current, length)
        top = bar.mapToGlobal(QPoint(0, 0)).y()
        # the card is drawn over the bar it belongs to; a bar at the top of the screen (a seek
        # dragged into the title bar) has no room there, so it flips below the bar instead
        screen = QApplication.primaryScreen()
        below = bool(screen is not None and top - screen.availableGeometry().top() < 200)
        if below:
            top = bar.mapToGlobal(QPoint(0, bar.height())).y()
        self._preview_args = (gpos.x(), top, fmt_time(ms), below)
        self.preview.show_at(gpos.x(), top, fmt_time(ms), pm, below)

    def _preview_ready(self, path, bucket):
        """A preview frame was written: show it if the pointer is still on this file."""
        if not self.preview.isVisible() or getattr(self, "_preview_path", None) != path:
            return
        length = self._preview_length
        pm = self.media.preview(path, self._preview_ms, length) if length else None
        if pm is not None:
            x, top, text, below = self._preview_args
            self.preview.show_at(x, top, text, pm, below)

    def seek_hover_end(self):
        self.preview.hide()

    def jump_cue(self, direction):
        e = self.engine
        cur = e.current
        if not cur:
            return
        subs = self.store.external_subs(cur) or find_subtitles(cur)
        cues = self.media.cues(cur, subs[0] if subs else None) or []
        shift = int(e.fx["sub_delay"] * 1000)
        t = e.time()[0]
        starts = sorted(a + shift for a, _ in cues)
        if direction > 0:
            nxt = next((s for s in starts if s > t + 300), None)
        else:
            nxt = next((s for s in reversed(starts) if s < t - 800), None)
        if nxt is not None:
            e.seek_to(max(0, nxt - 200))

    def nudge_sub(self, delta):
        e = self.engine
        e.set_fx("sub_delay", round(e.fx["sub_delay"] + delta, 3))
        self.osd.show_text(tr("Subtitle delay: {ms:+.0f} ms").format(ms=e.fx['sub_delay'] * 1000), "",
                           self.store.get("osd_duration"))

    # ------------------------------------------------------------------ surface actions
    def _surface_click(self):
        a = self.store.get("click_action")
        self._click_undo = None
        if a == "Play / Pause":
            self.engine.toggle()
            self._click_undo = self.engine.toggle      # a double click takes this back
        elif a == "Toggle fullscreen":
            self.toggle_fullscreen()
            self._click_undo = self.toggle_fullscreen
        elif a == "Show controls":
            self.show_overlay_bars()
        self.close_panels() if self.open_panel is not None and a == "Nothing" else None

    def _surface_double(self):
        if self.pip is not None:
            return                      # in picture in picture a double click just goes back (PipWindow)
        single = self.store.get("click_action")
        double = self.store.get("dblclick_action")
        undo, self._click_undo = getattr(self, "_click_undo", None), None
        if double == single:
            return                      # the first click already did it: don't undo, don't repeat
        if undo is not None:
            undo()                      # the first click fired at once, so take it back
        if double == "Toggle fullscreen":
            self.toggle_fullscreen()
        elif double == "Play / Pause":
            self.engine.toggle()

    def _surface_middle(self):
        # VLC: the middle button shows or hides the fullscreen controls
        if self.pip is None and self.overlay_mode():
            if any(o.isVisible() for o in self._fs_bars.values()):
                self._hide_overlays(force=True)
            else:
                self.show_overlay_bars()

    def video_drags_window(self):
        return not self.titlebar.isVisible()

    def _surface_wheel(self, dx, dy):
        diag.log("surface", dx=dx, dy=dy, state=self.engine.state,
                 cur=os.path.basename(self.engine.current or ""))
        rev = -1 if self.store.get("reverse_scroll") else 1
        hrev = -1 if self.store.get("hwheel_reverse") else 1     # only the horizontal axis
        for amount, action in ((dy, self.store.get("wheel_action")),
                               (dx * hrev, self.store.get("hwheel_action"))):
            if not amount:
                continue
            amount *= rev
            if action == "Volume":
                self.engine.change_volume(amount)
            elif action == "Seek":
                self._wheel_seek(amount)

    WHEEL_SEEK_FLUSH = 120                 # ms between the seeks of one continuous scroll
    WHEEL_SEEK_IDLE = 260                  # ms of silence that ends the scroll
    WHEEL_STEP_LIMIT = 5                   # notches per single event: wheels send big deltas
                                           # when spun fast, and those must not cross the film
    WHEEL_SCRUB_PER_SEC = 30               # seconds of video a held wheel moves per second of
                                           # holding, whatever the device's repeat rate is
    WHEEL_BURST_SEC = 1.0                  # a quick flick may spend this much at once
    WHEEL_INFLIGHT_S = 0.8                 # wait this long for the previous seek to land before
                                           # asking for the next one (a seek storm on a big MKV
                                           # means no seek ever completes)

    def _wheel_seek(self, steps):
        """Wheel seeks add up and reach libVLC as one seek every WHEEL_SEEK_FLUSH ms.

        One seek per event (a tilted wheel or a touchpad sends dozens a second) re-read the
        position libVLC had not moved to yet, so the picture stood still and then jumped.
        Waiting for silence instead is worse: holding the tilt button piled every repeat into
        one seek that shot past the beginning or the end of the file. The steps are also rate
        limited — a held tilt repeats as fast as the device can, and crossing a whole film in
        the three seconds of one hold reads as "it jumps to the start"."""
        steps = max(-self.WHEEL_STEP_LIMIT, min(self.WHEEL_STEP_LIMIT, steps))
        allowed = self._wheel_allowance(steps)
        diag.log("allow", want=steps, given=allowed, tokens=round(self._wheel_tokens, 2))
        steps = allowed
        if not steps:
            return
        if self._seek_pending is None:
            # A fresh scroll: the anchor is where we are (or where a seek in flight is heading)
            self._seek_pending = [self.engine.position(), 0, 0.0]
            diag.log("session", event="start", anchor=self._seek_pending[0])
        self._seek_pending[1] += steps * int(self.store.get("skip_vshort")) * 1000
        self._seek_idle_timer.stop()       # the scroll is still going
        if not self._seek_timer.isActive():
            self._seek_timer.start(self.WHEEL_SEEK_FLUSH)

    def _wheel_allowance(self, steps):
        """Rate limit: notches come out of a bucket that refills over time. A flick of the
        wheel is spent at once, a held one keeps scrubbing at WHEEL_SCRUB_PER_SEC seconds of
        video per second — the step size setting decides how many notches that is."""
        step = max(1, int(self.store.get("skip_vshort")))
        per_sec = max(1.0, self.WHEEL_SCRUB_PER_SEC / step)
        burst = max(4.0, per_sec * self.WHEEL_BURST_SEC)
        now = time.monotonic()
        self._wheel_tokens = min(burst, self._wheel_tokens + (now - self._wheel_tokens_at) * per_sec)
        self._wheel_tokens_at = now
        if abs(steps) > self._wheel_tokens:
            steps = int(math.copysign(self._wheel_tokens, steps))
        self._wheel_tokens -= abs(steps)
        return steps

    def _flush_wheel_seek(self):
        p = self._seek_pending
        if p is None or not p[1]:
            self._seek_pending = None
            return
        now = time.monotonic()
        if self.engine.seek_in_flight() and now - p[2] < self.WHEEL_INFLIGHT_S:
            # libVLC is still getting to the previous position: asking again now would abort it
            # (on a large MKV that ends with no seek completed at all). Keep adding up.
            diag.log("flush", event="wait", anchor=p[0], acc=p[1], since=round(now - p[2], 2))
            self._seek_timer.start(self.WHEEL_SEEK_FLUSH)
            return
        moved = self.engine.position() - p[0]
        if abs(moved) > 2000:
            # Someone moved the position by other means (seek bar, hotkey, chapter): our anchor
            # is stale, and scrolling on would jump back to where we were before.
            diag.log("session", event="foreign-move", anchor=p[0], acc=p[1],
                     position=self.engine.position(), last=self.engine._last[0])
            self._seek_pending = None
            return
        length = self.engine.time()[1]
        target = max(0, p[0] + p[1])
        if length > 0:
            target = min(target, length - 500)
        diag.log("flush", anchor=p[0], acc=p[1], target=target, length=length,
                 position=self.engine.position(), last=self.engine._last[0],
                 known=self.engine.time()[0], raw=self.engine.mp.get_time())
        self.engine.seek_to(target)
        self.engine.message.emit(tr("{v} s").format(v=f"{'+' if p[1] > 0 else '-'}"
                                                       f"{abs(p[1]) // 1000}"), "")
        # Move the anchor to where we just asked libVLC to go: while the wheel keeps
        # repeating, the next steps continue from there instead of from the position libVLC
        # has not reached yet. The scroll ends once the events stop (see _end_wheel_seek).
        p[0], p[1], p[2] = target, 0, now
        self._seek_idle_timer.start(self.WHEEL_SEEK_IDLE)

    def _end_wheel_seek(self):
        self._seek_pending = None
        # after a pause one notch is always spendable, so a single click right after a long
        # hold is never swallowed by the rate limiter
        self._wheel_tokens = max(self._wheel_tokens, 1.0)
        diag.log("session", event="end")

    # ------------------------------------------------------------------ simple actions
    def minimize(self):
        self.showMinimized()

    def _unminimize(self):
        """Put the window back on screen keeping the state it had.

        Qt's showNormal() is what brings a minimized window back, but for a maximized one it
        leaves it screen-sized and no longer maximized: Qt's idea of the normal size is the screen
        here (see _maximize_animated), so the restore button then has nothing to go back to."""
        self.showMaximized() if self.isMaximized() else self.showNormal()

    def toggle_maximized(self):
        if self.isFullScreen():
            return
        self.restore_animated() if self.isMaximized() else self.maximize_animated()

    # Windows animates these (the window grows into place instead of jumping); a frameless
    # window gets none of it, so the geometry is animated here. The duration is the speed and is
    # kept short — 180 ms to grow, 150 ms back — and the frames inside it are spent by the
    # driver (see _GeometryDriver), evenly, at whatever rate this machine can draw them.
    #
    # The curve is how the distance is shared between the frames: InOutSine barely moves at the
    # ends and puts its biggest step in the middle; this one is the same ease-in-out with a
    # flatter middle, so the step does not grow just where the machine is slowest.
    MAX_MS = 180
    RESTORE_MS = 150
    CURVE = QEasingCurve()
    CURVE.setType(QEasingCurve.BezierSpline)
    CURVE.addCubicBezierSegment(QPointF(0.35, 0.0), QPointF(0.65, 1.0), QPointF(1.0, 1.0))

    def _maximizing(self):
        return getattr(self, "_geo_anim", None) is not None

    @staticmethod
    def _usable_normal(r):
        """Whether `r` can be a window a user un-maximized: a real rectangle, on a screen, and
        not the whole screen at once.

        Two kinds of rectangle are turned away and both are worse than no answer at all. The
        screen itself is what Qt calls the normal geometry once the animated maximize has changed
        the state at full size (see _maximize_animated) — going back to it is the restore button
        doing nothing — and a rectangle whose middle is on no screen is one remembered from a
        monitor that has been unplugged. A window the user resized to the whole screen by hand is
        turned away too: that is one shape worth sacrificing for never landing on a fake one."""
        if r is None or not r.isValid() or r.width() < 200 or r.height() < 150:
            return False
        for screen in QApplication.screens():
            area = screen.availableGeometry()
            if area.contains(r.center()):
                return r.width() < area.width() or r.height() < area.height()
        return False

    def _normal_target(self):
        """Where the window goes when it is un-maximized — the button, or a title bar dragged off
        the top of the screen (see _restore_animated, _restore_for_drag).

        The remembered size comes first: it is what the user last left the window at, across
        restarts too (see _load_normal_geometry). Qt's own idea of the normal size is the fallback
        for a window that has none yet, and a screen with a margin is the last resort — still a
        window rather than a screen-sized one."""
        for r in (self._normal_geo, self.normalGeometry()):
            if self._usable_normal(r):
                return QRect(r)
        screen = self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry().adjusted(60, 60, -60, -60) if screen else QRect(self.geometry())

    def _anim_geometry(self, obj, target, ms, done):
        """Grow (or shrink) the window into `target`, in evenly spaced frames.

        Only the window itself goes through the fixed-cadence driver; anything else keeps Qt's
        own property animation. Which of the two is used is decided by the caller passing `self`
        as `obj`, and the driver is only good for a window of the application's own.
        """
        def finish():
            self._geo_anim = None
            try:
                done()
            except Exception as exc:
                diag.log("window", event="anim-finish-failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                self._after_geometry_animation()

        if obj is not self:
            anim = QPropertyAnimation(obj, b"geometry", self)
            anim.setDuration(ms)
            anim.setStartValue(obj.geometry())
            anim.setEndValue(target)
            anim.setEasingCurve(self.CURVE)
            self._geo_anim = anim
            anim.finished.connect(finish)
            anim.start(QAbstractAnimation.DeleteWhenStopped)
            return

        driver = _GeometryDriver(self, target, ms, finish)
        self._geo_anim = driver
        driver.start()

    def _learn_rate(self, overran):
        """Let the floor go, a step per animation, when nothing had to be lengthened.

        A floor is what keeps the step from being shortened after every cheap frame and lengthened
        again after the next late one, but a floor raised by one slow animation of one film would
        otherwise hold every animation after it coarse — the library page would grow at 24 ms
        because a film once made it miss 16. One step per animation that came out clean is slow
        enough to keep the oscillation out and quick enough that the next state of the window is
        animated at its own rate."""
        if not overran:
            self._anim_floor = max(_GeometryDriver.MIN_MS,
                                   self._anim_floor - _GeometryDriver.STEP_MS)

    def _after_geometry_animation(self):
        """Put back the one thing the animation left out, once the window has stopped moving.

        The video's rounded mask is a SetWindowRgn on a native child window, and Windows answers
        one with a WM_WINDOWPOSCHANGING/WM_WINDOWPOSCHANGED pair and a full redraw of that
        child — measured at ~1 ms of the ~8 ms a frame cost, and it paints the video area black
        on the way. Every frame is the wrong place for it: the corners are nine pixels of a
        window that is moving, and nobody sees them until it stops."""
        try:
            video = self.player.video
            video.set_radius(video.radius)
        except (RuntimeError, AttributeError):
            pass

    def maximize_animated(self):
        """Maximize with the growth Windows shows instead of an instant jump.

        Wrapped: an exception in a slot aborts the whole process under PyQt, and this runs from
        a click. If anything at all goes wrong the window is simply maximized, without the
        animation — the same result, minus the growth."""
        try:
            self._maximize_animated()
        except Exception as exc:                       # never let a slot kill the app
            diag.log("window", event="maximize-anim-failed", error=f"{type(exc).__name__}: {exc}")
            self._geo_anim = None
            self.showMaximized()

    def _maximize_animated(self):
        """The window itself is resized frame by frame — the video grows with it. The state change
        happens on the last frame and is the only thing left: Qt records the "normal" geometry
        (the size to come back to) when showMaximized() runs and would record the animated,
        full-size rect, so the real one is kept here instead of briefly resizing the window back
        to it — that resize was a visible flash of the desktop through the window."""
        if self.isFullScreen() or self.isMaximized() or self._maximizing():
            return
        normal = QRect(self.geometry())
        # The size to come back to is what is on screen now — unless that cannot be one (see
        # _usable_normal): the system's own restore leaves a maximized window at screen size, and
        # that must not overwrite the size the user actually left the window at.
        if self._usable_normal(normal):
            self._normal_geo = QRect(normal)
        screen = self.screen() or QApplication.primaryScreen()
        target = screen.availableGeometry() if screen else normal
        # The frame's margin is left alone until the state changes: taking it off here costs a
        # relayout and a full repaint of the page before the first frame, which put a 30 ms wait
        # between the click and any movement at all. It goes at the end instead, when the
        # window state changes (see changeEvent), and the five pixels the content grows by are
        # the animation's last step rather than a stall before its first.

        def done():
            self.setGeometry(target)
            self.showMaximized()

        self._anim_geometry(self, target, self.MAX_MS, done)

    def restore_animated(self):
        try:
            self._restore_animated()
        except Exception as exc:                       # see maximize_animated
            diag.log("window", event="restore-anim-failed", error=f"{type(exc).__name__}: {exc}")
            self._geo_anim = None
            self.showNormal()

    def _restore_animated(self):
        if not self.isMaximized() or self._maximizing():
            return
        target = self._normal_target()
        # Qt's idea of the normal geometry is the screen after the animated maximize above (it
        # recorded the size it was called at, and that was already the full one). Where that is
        # so, showNormal() changes nothing on screen and the state can be taken first; where Qt
        # does know the right size, the state goes last instead. Either way the window is never
        # resized to anything but the size the animation is heading for — resizing it back to a
        # stored normal size was the flash of the desktop seen through the window.
        here = QRect(self.geometry())
        qt_normal = self.normalGeometry()
        qt_wrong = (not qt_normal.isValid()
                    or abs(qt_normal.width() - here.width()) < 8
                    and abs(qt_normal.height() - here.height()) < 8)
        if qt_wrong:
            self.showNormal()

        def done():
            self.setGeometry(target)
            if not qt_wrong:
                self.showNormal()
            self._sync_all()

        self._anim_geometry(self, target, self.RESTORE_MS, done)

    def close_window(self):
        self.close()

    def quit(self):
        self.close()

    def start_move(self):
        if not self.windowHandle():
            return
        if self.isFullScreen():
            # Dragging the bar at the top of a fullscreen window brings the window back, with the
            # pointer where it is on that bar, and the drag carries on as a move — the same
            # gesture that restores a maximized window (see _restore_for_drag). Before this the
            # drag simply did nothing.
            self._leave_fullscreen_for_drag()
        elif self.isMaximized():
            self._restore_for_drag()
        wm.start(self, snap=True)          # a drag to the top edge maximizes (see wm.snap_to_top)

    def _leave_fullscreen_for_drag(self):
        """Come out of fullscreen for a drag: the window takes the size it had before it went
        fullscreen (or, when that cannot be the size of a window, the size the maximize button
        restores to), placed so the pointer stays on the bar."""
        cursor = QCursor.pos()
        screen = self.screen() or QApplication.primaryScreen()
        # The size the window had when it went fullscreen — unless that cannot be the size of a
        # window (see _usable_normal): a window that was maximized (or filled to the screen by
        # hand) went fullscreen *at the size of the screen*, and coming back to that is a
        # screen-sized window that is not maximized — nothing has shrunk, and the maximize button
        # has nothing to go back to. That size is the one the maximize button itself restores to,
        # so the drag comes back to it (see _normal_target).
        target = (QRect(self._fullscreen_from) if self._usable_normal(self._fullscreen_from)
                  else QRect(self._normal_target()))
        if target.isEmpty():
            target = QRect(screen.availableGeometry()) if screen else QRect(self.geometry())
        self._show_cursor()
        # not back_to_maximized: dragging takes a window out of the maximized state, and a window
        # put back at its normal size while the state still says maximized would keep neither
        self.exit_fullscreen(back_to_maximized=False)
        g = QRect(target)
        g.moveLeft(int(cursor.x() - min(target.width() // 2, 220)))
        g.moveTop(max(0, int(cursor.y() - min(22, g.height() // 12))))
        self.setUpdatesEnabled(False)
        self.setGeometry(g)
        self.setUpdatesEnabled(True)
        clamp_position(self)                 # a drag can start at the very edge of the screen
        self._normal_geo = QRect(self.geometry())
        self._sync_all()

    def _restore_for_drag(self):
        """Bring a maximized window back to its normal size when its title bar is dragged.

        Done here, before the system's move loop takes over, because that loop restores the
        window to the rectangle the system knows as "normal" — which for us is the animated,
        screen-sized one (see _maximize_animated), so the drag left a screen-sized window in the
        normal state: not maximized, the size of the screen, and the maximize button did nothing.
        The window is placed with the title bar under the pointer, so the drag continues from
        where the user grabbed it."""
        target = self._normal_target()
        here, cursor = QRect(self.geometry()), QCursor.pos()
        g = QRect(target)
        rel = (cursor.x() - here.x()) / max(1, here.width())     # same spot along the title bar
        g.moveLeft(int(cursor.x() - rel * g.width()))
        g.moveTop(int(cursor.y() - min(22, g.height() // 12)))
        self.setUpdatesEnabled(False)
        self.showNormal()
        self.setGeometry(g)
        self.setUpdatesEnabled(True)
        self._normal_geo = QRect(g)
        self._sync_all()

    def take_snapshot(self):
        path = self.engine.snapshot()
        if path and self.store.get("snap_preview"):
            QTimer.singleShot(300, lambda: self._snapshot_preview(path))

    def _snapshot_preview(self, path):
        pm = QPixmap(path)
        if pm.isNull():
            return
        self.toast.show_message(f"<img src='{path}' width='160'><br>{os.path.basename(path)}", "Open folder", 5000)
        self.toast.action.connect(lambda: sysutil.open_path(os.path.dirname(path)))

    def show_time(self):
        t, length = self.engine.time()
        self.osd.show_text(f"{fmt_time(t)} / {fmt_time(length)}", self.key_label("show_time"), 2.0)

    def volume_up(self):
        self.engine.change_volume(1)

    def volume_down(self):
        self.engine.change_volume(-1)

    def faster(self):
        self.engine.set_rate(self._next_rate(1))

    def slower(self):
        self.engine.set_rate(self._next_rate(-1))

    def normal_speed(self):
        self.engine.set_rate(1.0)

    def _next_rate(self, d):
        steps = [0.25, 0.5, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0]
        r = self.engine.rate()
        if d > 0:
            return next((s for s in steps if s > r + 0.01), steps[-1])
        return next((s for s in reversed(steps) if s < r - 0.01), steps[0])

    def jump(self, size, direction):
        secs = int(self.store.get({"vshort": "skip_vshort", "short": "skip_short", "medium": "skip_medium",
                                   "long": "skip_long"}[size]))
        self.engine.seek_rel(direction * secs * 1000)
        t, length = self.engine.time()
        self.engine.message.emit(tr("{v} s").format(v=f"{'+' if direction > 0 else '-'}{secs}")
                                 + f"  ({fmt_time(t)} / {fmt_time(length)})", "")

    def cycle_audio(self):
        self.engine.cycle_audio(self._last_key)

    def cycle_spu(self):
        self.engine.cycle_spu(self._last_key)

    def audio_delay(self, delta):
        e = self.engine
        e.set_fx("audio_delay", round(e.fx["audio_delay"] + float(delta), 3))
        e.message.emit(tr("Audio delay: {ms:+.0f} ms").format(ms=e.fx["audio_delay"] * 1000), "")

    def sub_delay(self, delta):
        e = self.engine
        e.set_fx("sub_delay", round(e.fx["sub_delay"] + float(delta), 3))
        e.message.emit(tr("Subtitle delay: {ms:+.0f} ms").format(ms=e.fx['sub_delay'] * 1000), "")

    def font_families(self):
        from PyQt5.QtGui import QFontDatabase
        fams = sorted(set(QFontDatabase().families()))
        return [("", "System")] + [(f, f) for f in fams]

    def aspect_options(self):
        extra = [x.strip() for x in (self.store.get("custom_aspects") or "").split(",") if x.strip()]
        return prefs.ASPECTS + [x for x in extra if x not in prefs.ASPECTS]

    def crop_options(self):
        extra = [x.strip() for x in (self.store.get("custom_crops") or "").split(",") if x.strip()]
        return prefs.CROPS + [x for x in extra if x not in prefs.CROPS]

    def _cycle(self, key, options, label):
        cur = self.engine.fx[key]
        nxt = options[(options.index(cur) + 1) % len(options)] if cur in options else options[0]
        self.engine.set_fx(key, nxt, announce=False)
        self.engine.message.emit(f"{tr(label)}: {tr(nxt)}", "")

    def cycle_aspect(self):
        self._cycle("aspect", self.aspect_options(), "Aspect ratio")

    def cycle_crop(self):
        self._cycle("crop", self.crop_options(), "Crop")

    def cycle_zoom(self):
        self._cycle("zoom", prefs.ZOOMS, "Zoom")

    def toggle_deinterlace(self):
        cur = self.store.get("deinterlace")
        nxt = "Off" if cur != "Off" else "On"
        self.store.set("deinterlace", nxt)
        self.engine.message.emit(f"{tr('Deinterlacing')}: {tr(nxt)}", "")

    def toggle_favorite_current(self):
        cur = self.engine.current
        if cur:
            on = self.store.toggle_favorite(cur)
            self.notify("Added to Favorites" if on else "Removed from Favorites")

    def add_bookmark(self):
        e = self.engine
        cur = e.current
        if not cur or not e.is_active():
            self.notify("Play something to add a bookmark")
            return
        t = e.mp.get_time()
        n = len(self.store.bookmarks(cur)) + 1
        name = f"{e.file_title().upper()} {n}"
        if self.store.add_bookmark(cur, t, name):
            e.message.emit(tr("Bookmark added: {t}").format(t=fmt_time(t)), self.key_label("bookmark"))
            self._update_seek_modes()

    def key_label(self, aid):
        return self.hotkeys().get(aid, "")

    # ------------------------------------------------------------------ playing things
    def play(self, items, index=0, names=None, shuffle=False):
        items = list(items)
        if not items:
            # An empty folder, or a list whose files are all gone: returning in silence made the
            # click look broken.
            self.notify(tr("Nothing playable there"))
            return
        if shuffle:
            import random
            random.shuffle(items)
        self._play_token += 1
        token = self._play_token
        if _needs_scan(items):
            # Walking a folder (or a playlist file) can take seconds; expand off the
            # GUI thread and start playback when it's done.
            tasks.run_bg(self.engine.expand, items,
                         on_done=lambda expanded: self._play_expanded(token, expanded, index, names))
            return
        self._play_expanded(token, items, index, names)

    def _play_expanded(self, token, items, index, names):
        if token != self._play_token:
            return                       # a newer play() superseded this scan
        if not items:
            self.notify(tr("Nothing playable there"))
            return
        if self.engine.load(items, index, names=names):
            if not self.store.get("use_library") or self.mode == "player" or not self.engine.has_video or \
                    not is_audio(self.engine.current):
                self.show_player()
            for i in items[:1]:
                self.store.add_recent_open(i) if is_url(i) else None

    def enqueue(self, items, names=None):
        items = list(items)
        if not items:
            self.notify(tr("There is nothing to add to the queue"))
            return
        self._enqueue_token += 1
        token = self._enqueue_token
        if _needs_scan(items):
            tasks.run_bg(self.engine.expand, items,
                         on_done=lambda expanded: self._enqueue_expanded(token, expanded, names))
            return
        self._enqueue_expanded(token, items, names)

    def _enqueue_expanded(self, token, items, names):
        if token != self._enqueue_token:
            return
        if not items:
            self.notify(tr("There is nothing to add to the queue"))
            return
        n = self.engine.enqueue(items, names=names)
        if n:
            self.notify(tr("Added {n} to the queue").format(n=n) if n > 1 else tr("Added to the queue"))

    def handle_args(self, items):
        items = [i for i in items if i]
        if not items:
            self.raise_()
            self.activateWindow()
            return
        subs = [i for i in items if ext(i) in SUB_EXT]
        media = [i for i in items if ext(i) not in SUB_EXT]
        if media:
            if self.store.get("enqueue_opened") and self.engine.items:
                self.enqueue(media)
            else:
                self.play(media)
        for s in subs:
            self.engine.add_subtitle(s)
        if self.isMinimized():
            self._unminimize()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ dialogs
    def dialog_options(self):
        return QFileDialog.DontUseNativeDialog if self.store.get("inapp_dialogs") else QFileDialog.Options()

    def open_dialog(self, kind="file", enqueue=False, start=None):
        start = start or (os.path.dirname(self.engine.current) if self.engine.current and not is_url(self.engine.current)
                          else os.path.expanduser("~/Videos"))
        if kind == "folder":
            folder = FileDialog.getExistingDirectory(self, "Open Folder", start, options=self.dialog_options())
            items = [folder] if folder else []
        else:
            exts = " ".join(f"*.{x}" for x in sorted(MEDIA_EXT | {"m3u", "m3u8", "pls"}))
            items, _ = FileDialog.getOpenFileNames(self, "Open File(s)", start,
                                                    tr("Media ({exts});;All files (*)").format(exts=exts),
                                                    options=self.dialog_options())
        if not items:
            return
        for i in items:
            self.store.add_recent_open(i)
        if enqueue:
            self.enqueue(items)
        else:
            self.play(items)

    def open_network(self, enqueue=False):
        NetworkDialog(self, enqueue).exec_()

    def open_disc(self):
        DiscDialog(self).exec_()

    def open_capture(self):
        CaptureDialog(self).exec_()

    def open_convert(self, items=None):
        items = items or ([self.engine.current] if self.engine.current else [])
        if not items:
            files, _ = FileDialog.getOpenFileNames(self, "Convert", os.path.expanduser("~/Videos"),
                                                    options=self.dialog_options())
            items = files
        if items:
            ConvertDialog(self, items).exec_()

    def open_stream_out(self, items=None):
        items = items or ([self.engine.current] if self.engine.current else [])
        if items:
            StreamDialog(self, items).exec_()

    def open_add_subtitles(self, path=None, scope="file", items=None):
        if path is None and items is None and not self.engine.current:
            self.notify("Play a video or choose a folder first")
            return
        if scope == "folder" and path is None:
            return
        d = AddSubtitlesDialog(self, path, scope, items)
        d.exec_()
        for p in self.panels.values():
            if hasattr(p, "refresh"):
                p.refresh()

    def add_audio_tracks(self, path=None, folder=None):
        start = folder or (os.path.dirname(path) if path and not is_url(path) else os.path.expanduser("~"))
        f, _ = FileDialog.getOpenFileName(self, "Add an audio track", start,
                                           tr("Audio ({exts})").format(
                                               exts=" ".join(f"*.{x}" for x in sorted(AUDIO_EXT))),
                                           options=self.dialog_options())
        if not f:
            return
        if path and path != self.engine.current:
            # remember it for that file and start playing: the track is attached from the
            # store while the media opens, so no timing guess is needed here
            self.store.add_external_audio(path, f)
            self.play([path])
        elif self.engine.current and not is_url(self.engine.current):
            # Adding to the file that is playing: remember it and hand it to the second player
            # (see engine.start_external_audio) — the picture and the seeking stay untouched.
            # In front of the tracks it already has: the one just chosen is the one to hear.
            self.store.add_external_audio(self.engine.current, f)
            self.engine.start_external_audio()
        else:
            self.engine.add_audio_file(f)          # streams: the only way libVLC offers
        self.auto_match_audio(path or self.engine.current, f)

    def new_playlist(self, items=None):
        d = PlaylistDialog(self, items=items)
        if d.exec_() and d.pid:
            self.library.navigate(("playlists", d.pid))

    def playlist_properties(self, pid):
        PlaylistDialog(self, pid=pid).exec_()

    def export_playlist(self, pid):
        pl = self.store.playlist(pid)
        if not pl:
            return
        path, _ = FileDialog.getSaveFileName(self, "Export Playlist", os.path.expanduser(f"~/{pl['name']}.m3u"),
                                              "M3U playlist (*.m3u)", options=self.dialog_options())
        if path:
            lines = ["#EXTM3U"]
            for i in pl["items"]:
                lines.append(f"#EXTINF:-1,{display_name(i)}")
                lines.append(i)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                self.notify(tr("Exported {name}").format(name=esc(os.path.basename(path))))
            except OSError:
                self.error("Couldn't write the playlist file")

    def playlist_submenu(self, items):
        out = [MenuItem("New Playlist…", lambda: self.new_playlist(items), icon="add", icon_variant="")]
        for pl in self.store.playlists():
            out.append(MenuItem(pl["name"], lambda pid=pl["id"]: (self.store.add_to_playlist(pid, items),
                                                                  self.notify(tr("Added to {name}").format(name=esc(self.store.playlist(pid)['name'])))),
                                icon="playlist", raw=True))
        return out

    def show_properties(self, item=None):
        item = item or self.engine.current
        if item:
            PropertiesDialog(self, item).exec_()

    def folder_info(self, target):
        if isinstance(target, tuple):
            self.notify(f"{self.library.model.title(target)}: "
                        + trn("item", "items", self.library.count(self.library.entries())))
            return
        media = self.media

        def scan():
            files = media_in_dir(target, recursive=True)
            size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
            dur = sum((media.info(f) or {}).get("duration", 0) for f in files)
            return len(files), size, dur

        def show(result):
            if result is None:
                return
            n, size, dur = result
            text = f"{trn('file', 'files', n)} · " + tr("{v} GiB").format(v=f"{size / 1073741824:.2f}")
            if dur:
                text += f" · {fmt_time(dur)}"
            MessageDialog(self, os.path.basename(target), text, target, ok="Close", cancel="").exec_()

        tasks.run_bg(scan, on_done=show)

    def show_in_folder(self, path):
        if path and not is_url(path):
            sysutil.reveal(path)

    def open_settings(self, tab=None):
        from .settings import SettingsDialog
        cat = {"playback": "playback", "video": "video", "audio": "audio", "subtitles": "subtitles"}.get(tab or "", "general")
        self.close_panels()
        dialog = SettingsDialog(self, cat)
        dialog.exec_()
        if dialog.reopen:
            # the language was switched in there: the dialog went with the old window, so the
            # switch builds the window again and this page opens on the new one
            self.apply_language(dialog.reopen)

    def open_shortcuts(self):
        ShortcutsDialog(self).exec_()

    def show_about(self):
        AboutDialog(self).exec_()

    def customize_player(self):
        from .customize import CustomizeDialog
        if self.mode != "player":
            self.show_player()
        if self.isFullScreen():
            self.exit_fullscreen()
        if self.hide_ui:
            self.toggle_hide_ui()
        CustomizeDialog(self).exec_()

    def confirm(self, title, detail=""):
        return MessageDialog(self, title, title, detail, ok="OK").exec_() == 1

    def error(self, text):
        if self.store.get("show_dialogs"):
            MessageDialog(self, "Error", text, ok="OK", cancel="").exec_()
        else:
            self.notify(text, timeout=4000)

    def notify(self, text, timeout=2200):
        self.toast.show_message(text, "", timeout)

    # ------------------------------------------------------------------ menus
    def open_main_menu(self, anchor):
        self._show_cursor()
        k = self.key_label
        recent = self.store["recent_open"]
        profiles = self.store.profiles()
        items = [
            MenuItem("Open File(s)", lambda: self.open_dialog("file"), icon="file", shortcut=k("open_file")),
            MenuItem("Open Folder", lambda: self.open_dialog("folder"), icon="folder", shortcut=k("open_folder")),
            MenuItem("Open Network Stream", self.open_network, icon="streaming", shortcut=k("open_network")),
            MenuItem("Open Disc", self.open_disc, icon="disc", shortcut=k("open_disc")),
            MenuItem("Open Capture Device", self.open_capture, icon="capture-device", shortcut=k("open_capture")),
            MenuItem("Convert", self.open_convert, icon="swap", shortcut=k("convert")),
            MenuItem("Open Recent", icon="recent", enabled=bool(recent),
                     submenu=lambda: [MenuItem(display_name(r), lambda r=r: self.play([r]), raw=True) for r in recent] +
                     [sep(), MenuItem("Clear", lambda: self.store.__setitem__("recent_open", []))]),
            sep(),
            # the profile name is a key ("Movies") shown to the user as «Фильмы» in the
            # submenu below: the title has to use the same form
            MenuItem(tr("Profile ({name})").format(name=tr(self.store.profile)), icon="profile",
                     submenu=self._profile_menu),
            MenuItem("Keyboard Shortcuts", self.open_shortcuts, icon="hotkeys", shortcut=k("shortcuts")),
            MenuItem("Customize Player", self.customize_player, icon="paint"),
            MenuItem("Settings", self.open_settings, icon="settings", shortcut=k("settings")),
            MenuItem("About Konus", self.show_about, icon="info", shortcut=k("about")),
        ]
        m = PopupMenu(items, self, width=367)
        m.popup_below(anchor, gap=6)

    def _rename_profile(self, name):
        new, ok = QInputDialog.getText(self, tr("Rename Profile"), tr("Name:"), text=name)
        if ok and self.store.rename_profile(name, new):
            self.notify(tr("Profile: {name}").format(name=esc(new.strip())))

    def _profile_label(self, name):
        """A profile's name as it is shown: the built-in ones ("Movies") are stored in English
        and appear translated, while a name the user typed is user data and stays as it is."""
        from .store import DEFAULT_PROFILES
        return tr(name) if name in DEFAULT_PROFILES else name

    def _profile_menu(self):
        items = [MenuItem(self._profile_label(p), lambda p=p: self._switch_profile(p),
                          checked=(p == self.store.profile), raw=True) for p in self.store.profiles()]
        items.append(MenuItem("New", self._new_profile, icon="add", icon_variant=""))
        items.append(MenuItem("Rename…", lambda: self._rename_profile(self.store.profile),
                              enabled=bool(self.store.profile)))
        if len(self.store.profiles()) > 1:
            items.append(MenuItem(tr("Delete “{name}”").format(name=self._profile_label(self.store.profile)),
                                  self._delete_profile))
        return items

    def _delete_profile(self):
        name = self.store.profile
        if self.confirm(tr("Delete the profile “{name}” and everything it keeps?").format(
                name=self._profile_label(name))):
            self.store.remove_profile(name)

    def _switch_profile(self, name):
        self.store.switch_profile(name)
        self.apply_theme()
        self.apply_shortcuts()
        self.notify(tr("Profile: {name}").format(name=esc(name)))

    def _new_profile(self):
        name, ok = QInputDialog.getText(self, tr("New Profile"),
                                        tr("Name (starts as a copy of the current profile):"))
        if ok and name.strip():
            if self.store.add_profile(name.strip()):
                self.notify(tr("Profile: {name}").format(name=esc(name.strip())))

    def open_context_menu(self, gpos, extra=None):
        self._show_cursor()            # the pointer comes back with the menu (see _hide_cursor)
        e = self.engine
        k = self.key_label
        active = e.is_active()
        items = [
            MenuItem("Pause" if e.is_playing() else "Play", e.toggle,
                     icon="pause" if e.is_playing() else "play", shortcut=k("play_pause")),
            MenuItem("Stop", e.stop, icon="stop", shortcut=k("stop"), enabled=active),
            MenuItem("Previous Item", e.prev, icon="previous", shortcut=k("prev")),
            MenuItem("Next Item", e.next, icon="next", shortcut=k("next")),
            MenuItem("Stop Recording" if e.is_recording() else "Record", e.toggle_record, icon="record",
                     shortcut=k("record"), enabled=active),
            MenuItem("Show UI" if self.hide_ui else "Hide UI", self.toggle_hide_ui, icon="ui", shortcut=k("hide_ui")),
            MenuItem("Playback", icon="playback", submenu=self._playback_menu),
            MenuItem("Video", icon="video", submenu=self._video_menu),
            MenuItem("Audio", icon="audio", submenu=self._audio_menu),
            MenuItem("Subtitle", icon="subtitles", submenu=self._subtitle_menu),
            MenuItem("More Settings", icon="settings", submenu=lambda: [
                MenuItem("Playback Settings", lambda: self.toggle_panel("settings", "playback", force=True)),
                MenuItem("Video Settings", lambda: self.toggle_panel("settings", "video", force=True)),
                MenuItem("Audio Settings", lambda: self.toggle_panel("settings", "audio", force=True)),
                MenuItem("Subtitle Settings", lambda: self.toggle_panel("settings", "subtitles", force=True)),
                MenuItem("Show Statistics", lambda: e.set_fx("statistics", not e.fx["statistics"]),
                         checked=e.fx["statistics"]),
                MenuItem("Properties & Metadata", self.show_properties, shortcut=k("properties"), enabled=bool(e.current)),
                sep(),
                MenuItem("Always on Top", lambda: self.store.set("on_top", not self.store.get("on_top")),
                         checked=self.store.get("on_top")),
                MenuItem("All Settings…", self.open_settings, shortcut=k("settings"))]),
            MenuItem("Playlist", icon="playlist", submenu=lambda: [
                MenuItem("Queue", lambda: self.toggle_panel("queue", "queue", force=True), shortcut=k("queue")),
                MenuItem("Bookmarks", lambda: self.toggle_panel("queue", "bookmarks", force=True)),
                MenuItem("Chapters", lambda: self.toggle_panel("queue", "chapters", force=True)),
                MenuItem("Add Bookmark", self.add_bookmark, shortcut=k("bookmark"), enabled=active),
                sep(),
                MenuItem("Save Queue as Playlist…", lambda: self.new_playlist(list(e.items)), enabled=bool(e.items)),
                MenuItem("Shuffle", e.toggle_shuffle, checked=e.shuffle),
                MenuItem("Repeat", e.cycle_repeat, checked=e.repeat != "off"),
                MenuItem("Clear Queue", e.clear, enabled=bool(e.items))]),
            MenuItem("Open Media", icon="file", submenu=lambda: [
                MenuItem("Open File(s)", lambda: self.open_dialog("file"), shortcut=k("open_file")),
                MenuItem("Open Folder", lambda: self.open_dialog("folder"), shortcut=k("open_folder")),
                MenuItem("Open Network Stream", self.open_network, shortcut=k("open_network")),
                MenuItem("Open Disc", self.open_disc, shortcut=k("open_disc")),
                MenuItem("Open Capture Device", self.open_capture, shortcut=k("open_capture")),
                MenuItem("Library", self.show_library_window, shortcut=k("home"))]),
            MenuItem("Quit", self.quit, icon="quit", shortcut=k("quit")),
        ]
        if extra:
            items += [sep()] + extra
        PopupMenu(items, self, width=292).popup(gpos)

    def _playback_menu(self):
        e = self.engine
        k = self.key_label
        speeds = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
        return [
            MenuItem("Speed", submenu=lambda: [MenuItem(f"{s:g}×", lambda s=s: e.set_rate(s),
                                                        checked=abs(e.rate() - s) < 0.01) for s in speeds]),
            MenuItem("Faster", self.faster, shortcut=k("faster")),
            MenuItem("Slower", self.slower, shortcut=k("slower")),
            MenuItem("Normal Speed", self.normal_speed, shortcut=k("normal_speed")),
            sep(),
            MenuItem("Jump Forward", lambda: self.jump("short", 1), shortcut=k("fwd_short")),
            MenuItem("Jump Backward", lambda: self.jump("short", -1), shortcut=k("back_short")),
            MenuItem("Jump to Specific Time…", self._jump_to_time),
            MenuItem("Next Frame", e.next_frame, shortcut=k("next_frame")),
            sep(),
            MenuItem("Sleep Timer", submenu=lambda: [MenuItem(o, lambda o=o: e.set_sleep(o), checked=e.fx["sleep"] == o)
                                                     for o in ("Off", "15 minutes", "30 minutes", "45 minutes", "1 hour",
                                                               "1.5 hours", "2 hours", "End of current item")]),
            MenuItem("Stop After Each Item", e.toggle_stop_after, checked=e.stop_after),
            MenuItem("Chapter", enabled=bool(getattr(self, "_chapters", [])),
                     submenu=lambda: [MenuItem(n, lambda i=i: e.set_chapter(i), raw=True) for i, n, _ in self._chapters]),
            MenuItem("Bookmarks", enabled=bool(e.current and self.store.bookmarks(e.current)),
                     submenu=lambda: [MenuItem(b["name"] or fmt_time(b["ms"]), lambda b=b: e.seek_to(b["ms"]), raw=True,
                                               shortcut=fmt_time(b["ms"])) for b in self.store.bookmarks(e.current)]),
        ]

    def _jump_to_time(self):
        t, length = self.engine.time()
        text, ok = QInputDialog.getText(self, tr("Jump to Time"), tr("Time (hh:mm:ss):"), text=fmt_time(t))
        if ok:
            nums = [float(x) for x in re.split(r"[:.]", text.strip()) if x.strip().replace(".", "").isdigit()]
            secs = 0
            for n in nums[:3]:
                secs = secs * 60 + n
            self.engine.seek_to(int(secs * 1000))

    def _video_menu(self):
        e = self.engine
        k = self.key_label
        return [
            MenuItem("Video Track", submenu=lambda: [MenuItem(n, lambda i=i: e.set_video_track(i), checked=e.video_track() == i, raw=True)
                                                     for i, n in e.video_tracks()] or [MenuItem("None", enabled=False)]),
            MenuItem("Fullscreen", self.toggle_fullscreen, shortcut=k("fullscreen"), checked=self.isFullScreen()),
            MenuItem("Picture in Picture", self.toggle_pip, shortcut=k("mini"), checked=self.pip is not None),
            MenuItem("Always on Top", lambda: self.store.set("on_top", not self.store.get("on_top")),
                     checked=self.store.get("on_top")),
            sep(),
            MenuItem("Zoom", submenu=lambda: [MenuItem(z, lambda z=z: e.set_fx("zoom", z), checked=e.fx["zoom"] == z)
                                              for z in prefs.ZOOMS]),
            MenuItem("Aspect Ratio", submenu=lambda: [MenuItem(a, lambda a=a: e.set_fx("aspect", a),
                                                               checked=e.fx["aspect"] == a) for a in self.aspect_options()]),
            MenuItem("Crop", submenu=lambda: [MenuItem(c_, lambda c_=c_: e.set_fx("crop", c_), checked=e.fx["crop"] == c_)
                                              for c_ in self.crop_options()]),
            MenuItem("Deinterlace", submenu=lambda: [MenuItem(o, lambda o=o: self.store.set("deinterlace", o),
                                                              checked=self.store.get("deinterlace") == o)
                                                     for o in ("Automatic", "On", "Off")]),
            MenuItem("Flip", submenu=lambda: [MenuItem(l, lambda v=v: e.set_fx("flip", v), checked=e.fx["flip"] == v)
                                              for v, l in (("none", "None"), ("horizontal", "Horizontal"),
                                                           ("vertical", "Vertical"))]),
            MenuItem("Rotate", submenu=lambda: [MenuItem(f"{d}°", lambda d=d: e.set_fx("rotate", d), checked=e.fx["rotate"] == d)
                                                for d in (0, 90, 180, 270)]),
            sep(),
            MenuItem("Take Snapshot", self.take_snapshot, shortcut=k("snapshot"), enabled=e.has_video and e.is_active()),
            MenuItem("Video Settings…", lambda: self.toggle_panel("settings", "video", force=True)),
        ]

    def _audio_menu(self):
        e = self.engine
        k = self.key_label
        from .engine import STEREO_LABELS, STEREO_MODES
        return [
            MenuItem("Audio Track", submenu=self._audio_tracks_menu),
            MenuItem("Audio Device", submenu=lambda: [MenuItem("System default", lambda: e.set_audio_device(None),
                                                               checked=e.audio_device() == "")] +
                     [MenuItem(desc, lambda d=d: e.set_audio_device(d), checked=e.audio_device() == d, raw=True)
                      for d, desc in e.audio_devices()]),
            MenuItem("Stereo Mode", submenu=lambda: [
                MenuItem(tr(STEREO_LABELS.get(m, m)), lambda m=m: e.set_fx("stereo_mode", m),
                         checked=e.fx["stereo_mode"] == m) for m in STEREO_MODES]),
            MenuItem("Equalizer", submenu=lambda: [MenuItem(pn, lambda pn=pn: self._eq(pn), checked=e.fx["eq_preset"] == pn)
                                                   for pn in prefs.EQ_PRESETS]),
            sep(),
            MenuItem("Volume Up", self.volume_up, shortcut=k("vol_up")),
            MenuItem("Volume Down", self.volume_down, shortcut=k("vol_down")),
            MenuItem("Mute", e.toggle_mute, shortcut=k("mute"), checked=e.muted),
            sep(),
            # a stream has no file to give a track to: the second player takes local items
            # only, so offering it there ended in "Couldn't load that audio file" every time
            MenuItem("Add Audio Track…", lambda: self.add_audio_tracks(e.current),
                     enabled=bool(e.current) and not is_url(e.current or "")),
            MenuItem("Match Tracks to the Folder…", lambda: self.match_audio_tracks(e.current),
                     enabled=bool(e.current) and not is_url(e.current or "")),
            MenuItem("Audio Settings…", lambda: self.toggle_panel("settings", "audio", force=True)),
        ]

    def _audio_tracks_menu(self):
        """The file's own tracks plus the ones added from external files, with a way to
        forget the added ones — they come back with the file on every play."""
        e = self.engine
        items = [MenuItem(n, lambda i=i: e.set_audio_track(i), checked=e.audio_track() == i, raw=True)
                 for i, n in e.audio_tracks()] or [MenuItem("None", enabled=False)]
        files = self.store.external_audio(e.current) if e.current and not is_url(e.current) else []
        if files:
            items += [sep(), MenuItem("Forget the added audio tracks", self._forget_audio_tracks,
                                      icon="remove", icon_variant="")]
        return items

    def match_audio_tracks(self, path=None):
        """Open the matcher for the current file's folder: it finds each episode's own
        track in there (including "RUS Sound" style subfolders) and remembers the pairs."""
        from .dialogs import AudioTracksDialog
        path = path or self.engine.current
        if not path or is_url(path):
            self.notify(tr("Play a video or choose a folder first"))
            return
        AudioTracksDialog(self, path).exec_()

    def match_subtitles(self, path=None):
        """Open the subtitle matcher for the current file's folder: it finds each episode's
        own subtitle files (in "Subs", one folder per language or all together) and remembers
        the pairs — every language a folder holds for an episode, not one guess."""
        from .dialogs import SubtitleTracksDialog
        path = path or self.engine.current
        if not path or is_url(path):
            self.notify(tr("Play a video or choose a folder first"))
            return
        SubtitleTracksDialog(self, path).exec_()

    def auto_match_audio(self, video, track=None):
        """After one track is added, give the other episodes of that folder theirs too:
        the scan runs in the background, the pairs land in the settings.

        The tracks come from the folder the added one lives in — in a release with several dubs
        (RUS Sound/<team>) that folder is the user's choice of dub, and it is the only folder
        where this series' episodes have tracks to find."""
        if not video or is_url(video):
            return
        folder = os.path.dirname(video)
        if not folder or not os.path.isdir(folder):
            return
        source = os.path.dirname(track) if track and not is_url(track) else folder
        want = [v for v in media_in_dir(folder, VIDEO_EXT)
                if v != video and not self.store.external_audio(v)]
        if not want:
            return
        tasks.run_bg(match_audio_tracks, want, [source],
                     on_done=lambda pairs: self._matched_audio(pairs, len(want)))

    def _matched_audio(self, pairs, looked):
        for video, tracks in pairs or []:
            self.store.set_external_audio(video, tracks)
        if pairs and self.engine.current in [v for v, _ in pairs]:
            self.engine.start_external_audio()     # the playing item switched to its new track
        n = len(pairs or [])
        if n == 1:
            self.notify(tr("Audio track matched for one more episode"))
        elif n:
            self.notify(tr("Audio tracks matched for {n} episodes").format(n=n))
        elif looked:
            self.notify(tr("No audio tracks to match in this folder"))

    def _forget_audio_tracks(self):
        e = self.engine
        if not e.current:
            return
        self.store.set_external_audio(e.current, [])
        e.stop_external_audio()
        e.message.emit(tr("Added audio tracks cleared"), "")

    def _eq(self, name):
        e = self.engine
        if name != "None":
            e.fx["eq_bands"] = e.eq_preset_bands(name)
        e.set_fx("eq_preset", name)

    def _subtitle_menu(self):
        e = self.engine
        k = self.key_label
        return [
            MenuItem("Add Subtitle File…", self._add_sub_file, enabled=bool(e.current)),
            MenuItem("Add Subtitles to Folder / Playlist…", lambda: self.open_add_subtitles(
                os.path.dirname(e.current) if e.current and not is_url(e.current) else None, "folder"),
                     enabled=bool(e.current) and not is_url(e.current or "")),
            MenuItem("Match Subtitles to the Folder…", lambda: self.match_subtitles(e.current),
                     enabled=bool(e.current) and not is_url(e.current or "")),
            MenuItem("Sub Track", submenu=lambda: [MenuItem(n, lambda i=i: e.set_spu(i), checked=e.spu() == i, raw=True)
                                                   for i, n in e.spu_tracks()] or [MenuItem("None", enabled=False)]),
            sep(),
            MenuItem("Delay −100 ms", lambda: self.sub_delay(-0.1), shortcut=k("sub_delay_down")),
            MenuItem("Delay +100 ms", lambda: self.sub_delay(0.1), shortcut=k("sub_delay_up")),
            MenuItem("Reset Delay", lambda: e.set_fx("sub_delay", 0.0)),
            MenuItem("Show Subtitle Timeline", lambda: e.set_fx("sub_timeline", not e.fx["sub_timeline"]),
                     checked=e.fx["sub_timeline"]),
            MenuItem("Subtitle Settings…", lambda: self.toggle_panel("settings", "subtitles", force=True)),
        ]

    def _add_sub_file(self):
        cur = self.engine.current
        start = os.path.dirname(cur) if cur and not is_url(cur) else ""
        path, _ = FileDialog.getOpenFileName(self, "Add subtitles", start,
                                              "Subtitles (*.srt *.ass *.ssa *.vtt *.sub *.smi *.idx);;All files (*)",
                                              options=self.dialog_options())
        if path:
            self.engine.add_subtitle(path)

    # ------------------------------------------------------------------ shortcuts
    def hotkeys(self):
        over = self.store["hotkeys"]
        return {aid: over.get(aid, default) for aid, _c, _l, default, _m in ACTIONS}

    def apply_shortcuts(self):
        for sc in getattr(self, "_shortcuts", []):
            sc.setEnabled(False)
            sc.deleteLater()
        self._shortcuts = []
        # Qt routes keys from the floating sidebar and panels (Qt.Tool windows) to these too.
        self._add_shortcuts(self)
        self._apply_global_hotkeys()

    def _add_shortcuts(self, target):
        for aid, key in self.hotkeys().items():
            if not key:
                continue
            sc = QShortcut(QKeySequence(key), target)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(lambda a=aid, k=key: self._run_action(a, k))
            self._shortcuts.append(sc)

    @staticmethod
    def _typing(widget):
        """True when the widget under the cursor takes text and should keep its own keys."""
        from PyQt5.QtWidgets import QAbstractSpinBox, QLineEdit, QPlainTextEdit, QTextEdit
        while widget is not None:
            if isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox)):
                return True
            widget = widget.parentWidget()
        return False

    def _run_action(self, aid, key=""):
        if aid not in ACTION:
            return                      # a shortcut stored by another build: nothing to run
        focus = QApplication.focusWidget()
        if focus is not None and focus is not self and self._typing(focus)                 and key in ("Up", "Down", "Left", "Right", "Space"):
            return                       # the field needs these itself
        if self.mode == "library" and key in ("Alt+Left", "Alt+Right"):
            self.library.back() if key == "Alt+Left" else self.library.forward()
            return
        method = ACTION[aid][4]
        self._last_key = key
        target, _, arg = method.partition(":")
        if target.startswith("engine."):
            getattr(self.engine, target[7:])()
        elif target == "jump":
            size, direction = arg.split(":")
            self.jump(size, int(direction))
        elif arg:
            getattr(self, target)(float(arg) if re.match(r"^-?\d", arg) else arg)
        else:
            getattr(self, target)()
        if aid in ("play_pause",):
            self.engine.message.emit("Pause" if self.engine.is_playing() else "Play", "") if False else None

    def _apply_global_hotkeys(self):
        # Only actions this build knows: a config from another version used to register its id
        # with the system, and pressing that key then raised KeyError inside a Qt slot — which
        # PyQt answers by aborting the process.
        mapping = {aid: k for aid, k in self.store["global_hotkeys"].items() if k and aid in ACTION}
        if not mapping:
            if getattr(self, "_global", None):
                self._global.stop()
                self._global = None
            return
        from .hotkeys import GlobalHotkeys
        if getattr(self, "_global", None):
            self._global.stop()
        self._global = GlobalHotkeys(mapping)
        self._global.triggered.connect(lambda aid: self._run_action(aid))
        self._global.start()

    # ------------------------------------------------------------------ theme / window settings
    def apply_theme(self):
        theme.apply(self.store.get("theme"), self.store.get("accent"))
        # the strip a growing window exposes is filled with the window class background brush,
        # so that colour has to follow the theme as well (see wm.erase_colour)
        for w in QApplication.topLevelWidgets():
            if w.isVisible():          # winId() of a hidden window would create it as a native one
                wm.erase_colour(w, c("base"))
        self.update()
        for w in self.findChildren(QWidget):
            w.update()
        for b in self.bars():
            b.update()

    def apply_language(self, reopen=None):
        """Switch the interface language, here and now.

        The words enter the interface as it is built (see konus.i18n), so there is nothing on
        screen to replace: the window is made again, and the new one takes up where this one
        was. Playback is not the window's — the engine keeps the queue, the position and the
        sound — so the switch costs a repaint, not a restart. `reopen` is a Settings category
        to come back to: the dialog this was asked from is part of what gets built again."""
        i18n.set_language(self.store.get("language"))
        if self.rebuild_ui is None:
            self.notify(tr("The language changes on the next start."))
            return
        self.rebuild_ui(reopen)

    def _apply_window_settings(self):
        s = self.store
        self.setWindowOpacity(max(0.1, s.get("window_opacity") / 100))
        self.set_above(self, s.get("on_top"))
        self._layout_bars_visibility()
        if hasattr(self, "tray") and self.tray is not None:
            self.tray.setVisible(bool(s.get("tray_icon")))

    def set_above(self, widget, on):
        QTimer.singleShot(200, lambda: _above_safe(widget, on))

    def _update_icon(self, state):
        base = icons.app_pixmap(64).copy()
        if state in ("playing", "paused"):
            p = QPainter(base)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#1b1b1b"))
            p.drawEllipse(36, 36, 28, 28)
            p.drawPixmap(38, 38, icons.pixmap("pause" if state == "playing" else "play", 24, "#ffffff", dpr=1.0))
            p.end()
        self.setWindowIcon(QIcon(base))

    def _resize_to_video(self):
        mode = self.store.get("resize_to_video")
        if mode == "Never" or self.isFullScreen() or self.isMaximized() or self.mode != "player":
            return
        if mode == "First video only" and getattr(self, "_resized_once", False):
            return
        w, h = self.engine.video_size()
        if not w or not h:
            return
        self._resized_once = True
        screen = self.screen().availableGeometry()
        chrome_w = self.width() - self.player.stack.width()
        chrome_h = self.height() - self.player.stack.height()
        scale = min(1.0, (screen.width() - chrome_w - 40) / w, (screen.height() - chrome_h - 40) / h)
        self.resize(int(w * scale) + chrome_w, int(h * scale) + chrome_h)

    def _setup_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(QIcon(icons.app_pixmap(64)), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction(tr("Play / Pause"), self.engine.toggle)
        menu.addAction(tr("Previous track"), self.engine.prev)
        menu.addAction(tr("Next track"), lambda: self.engine.next())
        menu.addSeparator()
        menu.addAction(tr("Show / Hide"), self._toggle_visible)
        menu.addAction(tr("Quit"), self.quit)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu
        self.tray.activated.connect(lambda reason: reason == QSystemTrayIcon.Trigger and self._toggle_visible())
        self.tray.setVisible(bool(self.store.get("tray_icon")))

    def _toggle_visible(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self._unminimize()
            self.raise_()
            self.activateWindow()

    # ------------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasFormat("application/x-konus-items"):
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasFormat("application/x-konus-items"):
            items = json.loads(bytes(md.data("application/x-konus-items")).decode())
        else:
            items = [u.toLocalFile() or u.toString() for u in md.urls()]
        subs = [i for i in items if ext(i) in SUB_EXT]
        media = [i for i in items if ext(i) not in SUB_EXT]
        if subs and self.engine.current:
            for s in subs:
                self.engine.add_subtitle(s)
        if media:
            if e.keyboardModifiers() & Qt.ShiftModifier or (self.engine.is_active() and
                                                            QApplication.keyboardModifiers() & Qt.ControlModifier):
                self.enqueue(media)
            else:
                self.play(media)
        e.acceptProposedAction()

    # ------------------------------------------------------------------ window chrome
    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange:
            m = 0 if (self.isMaximized() or self.isFullScreen()) else FRAME
            self.layout().setContentsMargins(m, m, m, m)
            self._sync_all()
            if self.isMinimized():
                if self.store.get("pause_minimized") and self.engine.is_playing() and self.engine.has_video:
                    self.engine.pause()
                self.close_panels()
                for ow in self._fs_bars.values():
                    ow.hide()
            QTimer.singleShot(50, self._reposition_floating)
        super().changeEvent(e)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_compact()

    def showEvent(self, e):
        super().showEvent(e)
        wm.erase_colour(self, c("base"))          # see wm.erase_colour: no white strip on growth
        QTimer.singleShot(0, self._sync_compact)

    def moveEvent(self, e):
        super().moveEvent(e)
        self._reposition_floating()

    # While the window is dragged (or its edge pulled), the system owns it: its move loop sits in
    # this thread's message queue and Qt's *queued* events — moveEvent and resizeEvent among them
    # — are not delivered until it is over (the same reason _flush_growth_strip paints by hand).
    # The floating windows are windows of their own, and the ones that live around the video are
    # the dark strips the eye is on: left to the queued events, the library sidebar, the panels,
    # the fullscreen bars and the toasts stay where the window was a moment ago, for the whole
    # drag, and then jump. These messages are handed over straight away instead, so the floating
    # windows are put in place while the drag is still going.
    WM_MOVE = 0x0003
    WM_WINDOWPOSCHANGED = 0x0047
    WM_EXITSIZEMOVE = 0x0232

    def nativeEvent(self, eventtype, message):
        if eventtype == b"windows_generic_MSG":
            try:
                # only the id is read: the MSG struct is ours only for the length of the call
                mid = ctypes.c_uint32.from_address(int(message) + 8).value
            except (TypeError, ValueError, OSError):
                mid = 0
            if mid in (self.WM_MOVE, self.WM_WINDOWPOSCHANGED):
                self._reposition_floating()
            elif mid == self.WM_EXITSIZEMOVE:
                # the drag is over: whatever the compositor was left showing, paint it again
                self._reposition_floating()
                self.update()
        return super().nativeEvent(eventtype, message)

    def resizeEvent(self, e):
        self._flush_growth_strip(e.oldSize(), e.size())
        super().resizeEvent(e)
        if self._geo_anim is not None and os.environ.get("KONUS_ANIM_SHOTS"):
            self._shot_frame()
        QTimer.singleShot(0, self._reposition_floating)

    def _flush_growth_strip(self, old, new):
        """Mark the strip this growth just opened up for painting, in the same turn as the resize.

        Left to the system, that strip is not the app's for a while: the compositor has nothing
        of its own to put there and shows either what is behind the window or the window class
        background brush (see wm.erase_colour for the colour of that one) — the pale stripes
        along the right and bottom edge. Asking for the paint here — a moment before Qt's own
        pass over the dirty region would have asked for it anyway — is what keeps the strip the
        app's own colour in the meantime, and it costs one paint of the window instead of the two
        a repaint() here plus the system's WM_PAINT would: measured on the real window, painting
        the strip by hand cost 6-8 ms of every frame, and the paint that followed it cost as much
        again for the same pixels."""
        if not old.isValid():
            return
        dw, dh = new.width() - old.width(), new.height() - old.height()
        if dw <= 0 and dh <= 0:
            return                              # a shrink opens nothing: the window leaves that area
        rects = []
        if dw > 0:
            rects.append(QRect(new.width() - dw, 0, dw, new.height()))
        if dh > 0:
            rects.append(QRect(0, new.height() - dh, new.width(), dh))
        region = QRegion(rects.pop())
        for r in rects:
            region = region.united(r)
        self.update(region)

    def _shot_frame(self):
        """Diagnostic, off unless KONUS_ANIM_SHOTS=1: save what the screen shows of this window
        during the maximize/restore animation, one file per frame.

        The frames land in the konus-anim folder of the temporary directory. They are what
        tells apart a strip of the desktop showing through the window (what is behind it)
        from something the app painted itself."""
        try:
            import tempfile
            from pathlib import Path
            d = Path(tempfile.gettempdir()) / "konus-anim"
            d.mkdir(parents=True, exist_ok=True)
            g = self.geometry()
            self._shot_n = getattr(self, "_shot_n", 0) + 1
            screen = self.screen() or QApplication.primaryScreen()
            pm = screen.grabWindow(0, g.x(), g.y(), g.width(), g.height()) if screen else None
            if pm is not None and not pm.isNull():
                pm.save(str(d / f"frame-{self._shot_n:02d}-{g.width()}x{g.height()}.png"))
        except Exception:
            pass

    def _edges(self, pos):
        edges = Qt.Edges()
        if self.isMaximized() or self.isFullScreen():
            return edges
        r, m = self.rect(), FRAME + 1
        if pos.x() <= m:
            edges |= Qt.LeftEdge
        if pos.x() >= r.width() - m:
            edges |= Qt.RightEdge
        if pos.y() <= m:
            edges |= Qt.TopEdge
        if pos.y() >= r.height() - m:
            edges |= Qt.BottomEdge
        return edges

    def mouseMoveEvent(self, e):
        edges = self._edges(e.pos())
        self._edge_cursor = bool(edges)
        if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeBDiagCursor)
        elif edges & (Qt.LeftEdge | Qt.RightEdge):
            self.setCursor(Qt.SizeHorCursor)
        elif edges & (Qt.TopEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, e):
        edges = self._edges(e.pos())
        if e.button() == Qt.LeftButton and edges and self.windowHandle():
            wm.start(self, edges)

    def keyPressEvent(self, e):
        if self._run_by_physical_key(e):
            return
        super().keyPressEvent(e)

    def _run_by_physical_key(self, e):
        """Run the action bound to the key at this physical position when the layout types
        something else for it (a Cyrillic layout turns ] into «ъ»). Qt's own shortcut
        matching compares characters, so without this the Latin bindings are dead there."""
        if e.isAutoRepeat():
            return False
        key = _VK_TO_QT.get(int(e.nativeVirtualKey()))
        if key is None or key == int(e.key()):
            return False                       # the layout produced the right key already
        mods = int(e.modifiers() & ~Qt.KeypadModifier)
        # Two forms can be bound: the key itself ("Shift+.") and the character it types on a US
        # layout (">"), which is how the default bindings are written.
        forms = [QKeySequence(key | mods).toString()]
        typed = _VK_TYPED.get((int(e.nativeVirtualKey()), bool(mods & int(Qt.ShiftModifier))))
        if typed:
            forms.append(QKeySequence(typed).toString() if mods & int(Qt.ControlModifier) or
                         mods & int(Qt.AltModifier) else typed)
            shifted = QKeySequence(mods & ~int(Qt.ShiftModifier))
            if shifted.isEmpty():
                forms.append(typed)
            else:
                forms.append(f"{shifted.toString()}+{typed}")
        for aid, bound in self.hotkeys().items():
            if bound and bound in forms:
                self._run_action(aid, bound)
                return True
        return False

    def leaveEvent(self, e):
        self._edge_cursor = False
        self.unsetCursor()

    def eventFilter(self, obj, ev):
        # The bars carry the app's own hint card instead of Qt's tooltip: it fades in, shows
        # up sooner (see the style hints in main.py) and, for the fullscreen bars, sits above
        # them — a hint below would be covered by the bar along the screen edge.
        if ev.type() == QEvent.ToolTip and isinstance(obj, QWidget):
            if obj.toolTip():
                self.hint.show_for(obj)
            else:
                self.hint.hide_soon()
            return True
        if ev.type() in (QEvent.MouseButtonPress, QEvent.Wheel, QEvent.KeyPress):
            self.hint.hide_soon()
            # The mouse's side buttons walk the file browser's history, as they walk
            # Explorer's: back and forward, wherever the pointer is on it.
            if ev.type() == QEvent.MouseButtonPress and ev.button() in (Qt.BackButton, Qt.ForwardButton):
                if self._browser_on_screen(obj):
                    self.library.back() if ev.button() == Qt.BackButton else self.library.forward()
                    return True
        elif ev.type() == QEvent.Leave:
            self._hint_may_leave(obj)
        # Every window the app puts on screen gets the app's colour as its class background
        # brush. The brush belongs to the class, so this covers each kind of window — dialogs
        # and panels included — the first time one of them is shown (see wm.erase_colour).
        if ev.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow() \
                and not obj.testAttribute(Qt.WA_TranslucentBackground):
            wm.erase_colour(obj, c("base"))
        # The resize cursor is set while the pointer is on our edge. Moving on into the window
        # only sends events to the widget it enters, so drop the cursor there; otherwise every
        # child kept showing it until a click.
        if ev.type() == QEvent.Enter and getattr(self, "_edge_cursor", False) and obj is not self \
                and isinstance(obj, QWidget) and self.isAncestorOf(obj):
            self._edge_cursor = False
            self.unsetCursor()
        return False

    def _browser_on_screen(self, obj):
        """Whether a mouse event under `obj` belongs to the file browser that is on screen —
        the full library page or the sidebar beside the video — rather than to a dialog, a
        panel or a popup opened above it."""
        if not isinstance(obj, QWidget):
            return False
        win = obj.window()
        if win is self.compact:
            return self.compact.isVisible()
        if win is not self:
            return False
        return self.mode == "library" or self.compact.isVisible()

    def _hint_may_leave(self, obj):
        """The hint card is put up under the pointer, so the surface that was being hovered
        sends a Leave the moment it appears. Reading every Leave as "the pointer is gone" is
        what made a hint vanish right after coming up, and — since moving to the neighbour
        icon crosses the card on the way — the hint of the icon being left. The pointer has
        really gone when it left the window the hint belongs to, or a window that is not one
        of these cards (the bars' popups, a dialog)."""
        if not isinstance(obj, QWidget):
            return
        for w in (self.hint, getattr(self, "toast", None)):
            if w is not None and (obj is w or w.isAncestorOf(obj)):
                return
        if obj is self:
            self.hint.hide_soon()
            return
        top = obj.window()
        if top is not self and top.isActiveWindow():
            # A popup or dialog of its own — Preferences, a menu: the pointer leaving it is
            # the pointer leaving the hint's world too, and the next hint will come with the
            # next tooltip.
            self.hint.hide_soon()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), c("bar"))
        if not (self.isMaximized() or self.isFullScreen()):
            p.setPen(c("line"))
            p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def _load_normal_geometry(self):
        """The size this window returns to when it is un-maximized, from the last session (see
        closeEvent). The setting that remembers where the window comes up covers this size too,
        so nothing is loaded when the user asked for it not to be."""
        if not self.store.get("remember_geometry"):
            return
        try:
            x, y, w, h = (int(v) for v in str(self.store["normal_geometry"] or "").split(","))
        except ValueError:
            return
        r = QRect(x, y, w, h)
        if self._usable_normal(r):
            self._normal_geo = r

    def _restore_geometry(self):
        geo = self.store["geometry"]
        if not (self.store.get("remember_geometry") and geo and
                self.restoreGeometry(QByteArray.fromBase64(geo.encode()))):
            screen = QApplication.primaryScreen().availableGeometry()
            w, h = min(1280, screen.width() - 80), min(800, screen.height() - 80)
            self.setGeometry(screen.x() + (screen.width() - w) // 2, screen.y() + (screen.height() - h) // 2, w, h)

    # ------------------------------------------------------------------ being rebuilt
    def take_state(self):
        """What a window built in this one's place has to come back to: where it stood, what it
        was showing, and the screen modes. Playback is the engine's and is not in here."""
        return {
            "geometry": QRect(self.geometry()),
            "normal_geometry": QRect(self._normal_geo) if self._normal_geo else None,
            "visible": self.isVisible(),
            "maximized": self.isMaximized(),
            "minimized": self.isMinimized(),
            "fullscreen": self.isFullScreen(),
            "pip": self.pip is not None,
            "hide_ui": self.hide_ui,
            "mode": self.mode,
            "location": tuple(self.library.loc),
            "sidebar": self.sidebar_open,
        }

    def restore_state(self, state):
        """Come back as the window that was replaced (see take_state)."""
        self._state = state
        self.setGeometry(state["geometry"])
        # the size a restore goes back to travels with the window: the rebuild is not a restart,
        # and the new window's own Qt normal geometry is this screen-sized rectangle
        if state.get("normal_geometry") is not None:
            self._normal_geo = state["normal_geometry"]
        if state["mode"] == "library":
            self.library.navigate(state["location"])
            self.show_library_window()
        else:
            self.show_player()
        # the mode is put back without the notice a toggle shows: this is not the user hiding the
        # interface, it is the window it was in
        self.hide_ui = bool(state["hide_ui"])
        self._layout_bars_visibility()
        if state["sidebar"] and state["mode"] == "player":
            self.sidebar_open = True
            self._sync_compact()
        if state["visible"]:
            if state["fullscreen"]:
                if state["maximized"]:
                    self.showMaximized()     # where leaving fullscreen comes back to
                self.enter_fullscreen()
            elif state["maximized"]:
                self.showMaximized()
            elif state["minimized"]:
                self.showMinimized()
            else:
                self.show()
        if state["pip"] and self.engine.current and self.engine.has_video:
            self.toggle_pip()

    def dispose(self):
        """Let go of everything this window holds. Called on the window a rebuilt one replaces
        (see konus.window.Shell): the engine, the store and the library's own state are not this
        window's and are left alone.

        What this window hung on the *engine* comes off by itself — those are bound slots of the
        widgets that are going away, and Qt drops them with them (see the notes in
        konus.miniplayer and konus.panels). The media service is this window's own, so its
        signals are dropped whole: a job that was already running can still deliver its result,
        and it must not arrive at a widget that is gone."""
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if getattr(self, "_global", None):
            self._global.stop()
            try:
                self._global.triggered.disconnect()
            except TypeError:
                pass
            self._global = None
        for signal in (self.media.thumbReady, self.media.infoReady, self.media.waveReady,
                       self.media.cuesReady, self.media.previewReady):
            try:
                signal.disconnect()
            except TypeError:
                pass
        if self.pip is not None:                 # its own top-level window: it would outlive us
            self.pip.hide()
            self.pip.deleteLater()
            self.pip = None
        if self.tray is not None:
            self.tray.hide()
        for w in (self.compact, self.toast, self.osd, self.preview, self.hint, self.stats,
                  *self.panels.values(), *self._fs_bars.values()):
            w.hide()
        self.media.shutdown()                    # the new window brought its own service
        self.hide()

    def closeEvent(self, e):
        if self.isFullScreen():
            self.exit_fullscreen()
        if self.pip is not None:
            self.toggle_pip()
        self.store["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        # the size the window comes back to when it is un-maximized: Qt records its own only
        # when the state changes while the window is at that size, and the animated maximize
        # changes it at full size, so Qt's copy would be the screen. While the window is
        # maximized the kept size is that one; otherwise what is on screen now is the size the
        # next session has to come back to, a resize since the last maximize included.
        g = (self._normal_geo or self.normalGeometry()) if self.isMaximized() else self.geometry()
        self.store["normal_geometry"] = f"{g.x()},{g.y()},{g.width()},{g.height()}"
        for w in (self.toast, self.osd, self.preview, self.stats, *self.panels.values(), *self._fs_bars.values()):
            w.hide()
        if getattr(self, "_global", None):
            self._global.stop()
        if self.tray is not None:
            self.tray.hide()
        self.engine.shutdown()
        self.media.shutdown()
        super().closeEvent(e)
        QApplication.quit()
