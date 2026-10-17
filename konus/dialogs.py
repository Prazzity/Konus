"""Dialogs in the redesign's style: #191919 card (r=14), #090909 header with the title and
a close button, an optional tool row, and Cancel / Save in the footer."""

import difflib
import json
import os
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path

from PyQt5.QtCore import QEvent, QPoint, QProcess, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QKeySequence, QPainter, QPen, QPixmap
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PyQt5.QtWidgets import (QApplication, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLineEdit, QPlainTextEdit,
                             QScrollArea, QVBoxLayout, QWidget)

from . import APP_NAME, __version__, icons, prefs, sysutil, tasks, theme
from .i18n import tr, trn, tr_opts
from .prefs import MOUSE_ROWS
from .media import match_audio_tracks, track_candidates, track_folders
from .engine import (AUDIO_EXT, MEDIA_EXT, SUB_EXT, VIDEO_EXT, display_name, ext, fmt_time, is_url, media_in_dir,
                     natural_key, parse_playlist_text)
from .theme import c
from .widgets import (Button, Dropdown, HLine, IconButton, Label, MenuItem, PopupMenu, Toggle, clamp_position, clamp_to_screen, elide, esc, paint_shadow, scroll, sep)
from .filedialog import FileDialog



def _request(url):
    req = QNetworkRequest(QUrl(url))
    req.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
    req.setRawHeader(b"User-Agent", b"VLCRedesign/0.2 (libVLC 3)")
    return req


class Dialog(QDialog):
    MARGIN = 24

    def __init__(self, parent, title, width=1048, height=None, tools=None, fill="rail", save_text="Save",
                 cancel_text="Cancel", footer=True):
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.fill = fill
        self._drag = None
        outer = QVBoxLayout(self)
        m = self.MARGIN
        outer.setContentsMargins(m, m, m, m)
        outer.setSpacing(0)
        self.head = QWidget()
        hv = QVBoxLayout(self.head)
        hv.setContentsMargins(9, 9, 9, 9)
        hv.setSpacing(12)
        top = QHBoxLayout()
        top.setContentsMargins(10, 0, 0, 0)
        self.title_label = Label(title, 14, "icon")
        self._title_text = tr(title)          # elided to the header on every resize
        top.addWidget(self.title_label)
        top.addStretch(1)
        close = IconButton("x", "Close (Esc)", box=(32, 32))
        close.clicked.connect(self.reject)
        top.addWidget(close)
        hv.addLayout(top)
        if tools is not None:
            hv.addWidget(tools)
        outer.addWidget(self.head)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(20, 22, 20, 22)
        self.body_lay.setSpacing(20)
        outer.addWidget(self.body, 1)
        self.footer = QWidget()
        fl = QHBoxLayout(self.footer)
        fl.setContentsMargins(20, 20, 20, 20)
        self.cancel = Button(cancel_text, "face", height=32)
        self.cancel.setFixedWidth(100)
        self.cancel.clicked.connect(self.reject)
        self.save = Button(save_text, "primary", height=32)
        self.save.setFixedWidth(max(100, self.save.sizeHint().width()))
        self.save.clicked.connect(self.accept_clicked)
        fl.addWidget(self.cancel)
        fl.addStretch(1)
        fl.addWidget(self.save)
        outer.addWidget(self.footer)
        self.footer.setVisible(footer)
        self.resize(width + 2 * m, (height or 600) + 2 * m)

    def accept_clicked(self):
        self.accept()

    def keyPressEvent(self, e):
        # Enter presses the dialog's main button (Save / Play / Convert…), as in any dialog
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & ~Qt.KeypadModifier):
            if not isinstance(QApplication.focusWidget(), QPlainTextEdit) and self.footer.isVisible() \
                    and self.save.isVisible() and self.save.isEnabled():
                self.accept_clicked()
            return
        super().keyPressEvent(e)

    def _first_field(self):
        """Put the cursor in the first text box (in tab order) when the dialog opens."""
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QPlainTextEdit)) and self.isAncestorOf(fw):
            return
        w = self.nextInFocusChain()
        while w is not None and w is not self:
            if isinstance(w, (QLineEdit, QPlainTextEdit)) and w.isVisible() and w.isEnabled() \
                    and not w.isReadOnly() and self.isAncestorOf(w):
                w.setFocus()
                return
            w = w.nextInFocusChain()

    def head_height(self):
        return self.head.height()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m = self.MARGIN
        r = QRectF(self.rect()).adjusted(m, m, -m, -m)
        paint_shadow(p, r, 14, spread=16, alpha=0.46, dy=8)
        p.setPen(Qt.NoPen)
        p.setBrush(c(self.fill, 0.985))
        p.drawRoundedRect(r, 14, 14)
        # header
        hr = QRectF(r.left(), r.top(), r.width(), self.head.height())
        from PyQt5.QtGui import QPainterPath
        path = QPainterPath()
        path.addRoundedRect(QRectF(r.left(), r.top(), r.width(), hr.height() + 20), 14, 14)
        clip = QPainterPath()
        clip.addRect(hr)
        p.setBrush(c("panel_head"))
        p.drawPath(path.intersected(clip))

    def resizeEvent(self, e):
        # the header has one line and no room to grow: a long title (a playlist the user named)
        # ran off under the close button instead of being elided
        if getattr(self, "_title_text", ""):
            self.title_label.setText(elide(self._title_text, theme.font(14), max(80, self.width() - 140)))
        super().resizeEvent(e)

    def showEvent(self, e):
        # A dialog is built at a size the design calls for; on a screen that cannot hold it (a
        # 1280x720 laptop against the 913 px Settings card) its footer and last rows were simply
        # off-screen. The card gives way, and the bodies that scroll keep their content reachable.
        clamp_to_screen(self)
        par = self.parentWidget()
        if par is not None:
            g = par.window().frameGeometry()
            self.move(g.center() - self.rect().center())
        clamp_position(self)
        super().showEvent(e)
        QTimer.singleShot(0, self._first_field)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.pos().y() < self.MARGIN + 50:
            self._drag = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPos() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None


def section(title, desc=None):
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(5)
    v.addWidget(Label(title, 16, "text_hi"))
    if desc:
        v.addWidget(Label(desc, 14, "text_2", wrap=True))
    return w


class Field(QLineEdit):
    """#090909 text field with a 2 px #191919 inner border (dialog inputs)."""

    def __init__(self, placeholder="", text="", height=37):
        super().__init__(text)
        self.setPlaceholderText(tr(placeholder))
        self.setFixedHeight(height)
        theme.styled(self, lambda: f"QLineEdit {{ background: {theme.hexc('field')}; "
                                   f"border: 2px solid {theme.hexc('item')}; border-radius: 6px; "
                                   f"padding: 0 12px; }}")


class ToolBox(QWidget):
    """Search / path box used in dialog tool rows (#151515 r=6, 34 px)."""

    def __init__(self, icon, text="", placeholder="", editable=True):
        super().__init__()
        self.setFixedHeight(34)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(10)
        ic = Label("")
        ic.setPixmap(icons.pixmap(icon, 24))
        h.addWidget(ic)
        self.edit = QLineEdit(tr(text))
        self.edit.setObjectName("Bare")
        self.edit.setPlaceholderText(tr(placeholder))
        self.edit.setReadOnly(not editable)
        h.addWidget(self.edit, 1)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(c("field_2"))
        p.drawRoundedRect(QRectF(self.rect()), 6, 6)


class ToolDropdown(Dropdown):
    """Dropdown in dialog tool rows: flat #151515, 34 px."""

    def __init__(self, options, value=None, width=200):
        super().__init__(options, value, height=34)
        self.setFixedWidth(width)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(c("field_2") if not self._hover else c("hover"))
        p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        p.setFont(theme.font(14))
        p.setPen(c("text"))
        p.drawText(QRectF(12, 0, self.width() - 50, self.height()), Qt.AlignVCenter, self.label())
        p.drawPixmap(self.width() - 8 - 24, (self.height() - 24) // 2,
                     icons.pixmap("chevron-up" if self._open else "chevron-down", 24))


# --- Add Subtitles -----------------------------------------------------------------------
EP_RE = re.compile(r"(?:(?<![a-z])s(\d{1,2})[ ._-]*e(\d{1,3})(?!\d)|(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)"
                   r"|(?<![a-z])e(?:p(?:isode)?)?[ ._-]?(\d{1,3})(?!\d))", re.I)


NOISE_NUMBERS = {"480", "576", "720", "1080", "2160", "264", "265", "10", "8"}


def episode_key(name):
    m = EP_RE.search(name)
    if not m:
        stem = os.path.splitext(name)[0]
        stem = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)          # [1080p] [x265] (2024)
        nums = [n for n in re.findall(r"(?<![\dA-Za-z])(\d{1,3})(?![\dA-Za-z])", stem) if n not in NOISE_NUMBERS]
        return (0, int(nums[-1])) if nums else None
    if m.group(1):
        return (int(m.group(1)), int(m.group(2)))
    if m.group(3):
        return (int(m.group(3)), int(m.group(4)))
    return (0, int(m.group(5)))


def match_subtitles(videos, subs):
    """{video: subtitle} by episode number, then by name similarity."""
    out = {}
    free = list(subs)
    by_ep = {}
    for s in subs:
        k = episode_key(os.path.basename(s))
        if k:
            by_ep.setdefault(k, []).append(s)
    for v in videos:
        k = episode_key(os.path.basename(v))
        cands = by_ep.get(k) or (by_ep.get((0, k[1])) if k else None)
        if not cands and k:
            cands = [s for kk, ss in by_ep.items() if kk[1] == k[1] for s in ss]
        if cands:
            out[v] = cands[0]
            continue
        stem = os.path.splitext(os.path.basename(v))[0].lower()
        # never pair a video with a subtitle that names a different episode
        pool = [s for s in free if not (k and episode_key(os.path.basename(s)) and
                                        episode_key(os.path.basename(s)) != k)]
        best = max(pool, key=lambda s: difflib.SequenceMatcher(None, stem, os.path.basename(s).lower()).ratio(),
                   default=None)
        if best and difflib.SequenceMatcher(None, stem, os.path.basename(best).lower()).ratio() > 0.6:
            out[v] = best
    return out


class PairRow(QWidget):
    """video ─ ─ ─ ─ subtitle (click the subtitle to choose another one)."""
    pick = pyqtSignal(str)

    def __init__(self, video, sub):
        super().__init__()
        self.video, self.sub = video, sub
        self.setFixedHeight(36)
        self.setMouseTracking(True)
        self._hover_right = False

    def set_sub(self, sub):
        self.sub = sub
        self.update()

    def mouseMoveEvent(self, e):
        h = e.x() > self.width() / 2
        if h != self._hover_right:
            self._hover_right = h
            self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self._hover_right = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.x() > self.width() / 2:
            self.pick.emit(self.video)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        half = self.width() / 2
        f = theme.font(14)
        fm = QFontMetrics(f)
        p.setFont(f)
        p.drawPixmap(10, 6, icons.pixmap("video", 24))
        name = os.path.basename(self.video)
        nw = min(fm.horizontalAdvance(name), int(half - 80))
        p.setPen(c("text"))
        p.drawText(QRectF(48, 0, nw + 2, 36), Qt.AlignVCenter, elide(name, f, half - 80))
        pen = QPen(c("line").lighter(150), 1, Qt.DashLine)
        pen.setDashPattern([6, 4])
        p.setPen(pen)
        p.drawLine(int(48 + nw + 14), 18, int(half - 10), 18)
        if self._hover_right:
            p.setPen(Qt.NoPen)
            p.setBrush(c("field"))
            p.drawRoundedRect(QRectF(half + 4, 0, half - 14, 36), 6, 6)
            p.drawPixmap(int(self.width() - 40), 6, icons.pixmap("swap", 24))
        p.drawPixmap(int(half + 14), 6, icons.pixmap("subtitle", 24))
        p.setPen(c("text") if self.sub else c("text_3"))
        p.drawText(QRectF(half + 48, 0, half - 100, 36), Qt.AlignVCenter,
                   elide(os.path.basename(self.sub) if self.sub else tr("No subtitles, click to choose"), f, half - 100))


class CheckRow(QWidget):
    """A row with a tick on the right. `note` is a count or a state shown beside it ("3 tracks"),
    in the dim colour and right-aligned, so a column of them lines up."""

    toggled = pyqtSignal(bool)

    def __init__(self, text, checked=False, icon="file", note=""):
        super().__init__()
        self.text, self.checked, self.icon, self.note = text, checked, icon, note
        self.setFixedHeight(36)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        self.checked = not self.checked
        self.update()
        self.toggled.emit(self.checked)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.checked:
            p.setPen(Qt.NoPen)
            p.setBrush(c("field"))
            p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        p.drawPixmap(10, 6, icons.pixmap(self.icon, 24))
        f = theme.font(14)
        fm = QFontMetrics(f)
        p.setFont(f)
        room = self.width() - 90
        if self.note:
            w = fm.horizontalAdvance(self.note)
            p.setPen(c("text_2"))
            p.drawText(QRectF(self.width() - 46 - w, 0, w, 36), Qt.AlignVCenter, self.note)
            room = min(room, self.width() - w - 106)          # 46 left of the text, a gap, the note
        p.setPen(c("text_hi") if self.checked else c("text"))
        p.drawText(QRectF(46, 0, room, 36), Qt.AlignVCenter, elide(self.text, f, room))
        if self.checked:
            p.drawPixmap(self.width() - 34, 6, icons.pixmap("check", 24, None, "active"))


class AddSubtitlesDialog(Dialog):
    def __init__(self, win, target_path=None, scope="file", items=None):
        self.win = win
        tools = QWidget()
        th = QHBoxLayout(tools)
        th.setContentsMargins(0, 0, 0, 0)
        th.setSpacing(10)
        self.scope = ToolDropdown([("file", "To This File"), ("folder", "To This Folder"), ("playlist", "To This Playlist")],
                                  scope)
        self.origin = ToolDropdown([("computer", "From Computer"), ("opensubtitles", "From OpenSubtitles"),
                                    ("link", "From Link …")], "computer")
        self.where = ToolBox("folder", editable=False)
        self.where.setCursor(Qt.PointingHandCursor)
        self.where.mousePressEvent = lambda e: self._choose_folder()
        self.query = ToolBox("search", placeholder="Search OpenSubtitles")
        self.lang = ToolDropdown(["English", "Arabic", "Chinese", "French", "German", "Hindi", "Italian", "Japanese",
                                  "Korean", "Portuguese", "Russian", "Spanish", "Turkish", "Urdu"], "English")
        self.link = ToolBox("link", placeholder="https://… link to a .srt / .vtt / .ass file")
        for w in (self.scope, self.origin):
            th.addWidget(w)
        th.addWidget(self.where, 1)
        th.addWidget(self.query, 1)
        th.addWidget(self.lang)
        th.addWidget(self.link, 1)
        super().__init__(win, "Add Subtitles", 1048, 704, tools=tools, fill="base")
        self.target_path = target_path or win.engine.current
        self.items = items
        if scope == "file" and self.target_path and os.path.isdir(self.target_path):
            self.scope.set_value("folder")
        self.folder = None
        if self.target_path and not is_url(self.target_path):
            self.folder = self.target_path if os.path.isdir(self.target_path) else os.path.dirname(self.target_path)
        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(10, 10, 10, 10)
        self.list_lay.setSpacing(4)
        self.body_lay.setContentsMargins(20, 10, 20, 0)
        self.body_lay.addWidget(scroll(self.list_host), 1)
        self.scope.changed.connect(lambda _v: self.rebuild())
        self.origin.changed.connect(lambda _v: self.rebuild())
        self.query.edit.returnPressed.connect(self._search_online)
        self.link.edit.returnPressed.connect(self._download_link)
        self.pairs = {}
        # ticks the user made are kept across rebuilds (see _reload_ticks)
        self.checked = set()
        self._ticks_for = None
        self.net = QNetworkAccessManager(self)
        self._reload_ticks()
        self.rebuild()

    def _videos(self):
        scope = self.scope.value
        if scope == "file":
            return [self.target_path] if self.target_path and not is_url(self.target_path) else []
        if scope == "playlist":
            if self.items:
                return [i for i in self.items if not is_url(i) and ext(i) in VIDEO_EXT]
            return [i for i in self.win.engine.items if not is_url(i) and ext(i) in VIDEO_EXT]
        return media_in_dir(self.folder, VIDEO_EXT) if self.folder else []

    def _subs(self):
        if not self.folder:
            return []
        subs = media_in_dir(self.folder, SUB_EXT)
        for d in ("Subs", "subs", "Subtitles", "subtitles"):
            subs += media_in_dir(os.path.join(self.folder, d), SUB_EXT)
        return subs

    def _choose_folder(self):
        d = FileDialog.getExistingDirectory(self, "Folder with the subtitle files", self.folder or os.path.expanduser("~"),
                                             options=self.win.dialog_options())
        if d:
            self.folder = d
            self._reload_ticks()
            self.rebuild()

    def _reload_ticks(self):
        """The ticks start as the subtitles the file already has. Rebuilt only when the target
        or the folder changes: a rebuild for any other reason used to wipe what the user had
        ticked, and the subtitle just downloaded through a link with it."""
        key = (self.target_path, self.folder)
        if key == self._ticks_for:
            return
        self._ticks_for = key
        self.checked = set(self.win.store.external_subs(self.target_path)) if self.target_path else set()

    def rebuild(self):
        origin = self.origin.value
        self.where.setVisible(origin == "computer")
        self.query.setVisible(origin == "opensubtitles")
        self.lang.setVisible(origin == "opensubtitles")
        self.link.setVisible(origin == "link")
        self.where.edit.setText(pretty_path(self.folder) if self.folder else tr("Choose a folder…"))
        while self.list_lay.count():
            w = self.list_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        if origin == "opensubtitles":
            name = os.path.splitext(os.path.basename(self.target_path or ""))[0].replace("_", " ")
            if not self.query.edit.text():
                self.query.edit.setText(name)
            self.list_lay.addWidget(Label("Press Enter to search OpenSubtitles.com in your browser. Save the files you "
                                          "download into the video's folder, then switch back to “From Computer”, "
                                          "they will be matched to the episodes automatically.", 14, "text_2", wrap=True))
            self.list_lay.addStretch(1)
            return
        if origin == "link":
            self.list_lay.addWidget(Label("Paste a direct link to a subtitle file and press Enter. It is saved next to "
                                          "the video and added.", 14, "text_2", wrap=True))
            self.list_lay.addStretch(1)
            return
        subs = self._subs()
        if self.scope.value == "file":
            stem = os.path.splitext(os.path.basename(self.target_path or ""))[0].lower()
            subs.sort(key=lambda s: (not os.path.basename(s).lower().startswith(stem[:8]), natural_key(s)))
            if not subs:
                self.list_lay.addWidget(Label("No subtitle files in this folder.", 14, "text_3"))
            for s in subs:
                row = CheckRow(os.path.basename(s), s in self.checked)
                row.toggled.connect(lambda on, s=s: self.checked.add(s) if on else self.checked.discard(s))
                self.list_lay.addWidget(row)
            # something to attach, or something attached to take off
            offer = bool(subs) or bool(self.checked)
        else:
            videos = self._videos()
            current = {v: (self.win.store.external_subs(v) or [None])[0] for v in videos}
            auto = match_subtitles(videos, subs)
            self.pairs = {v: current.get(v) or auto.get(v) for v in videos}
            self._subs_cache = subs
            if not videos:
                self.list_lay.addWidget(Label("No videos to match.", 14, "text_3"))
            for v in videos:
                row = PairRow(v, self.pairs[v])
                row.pick.connect(self._pick_for)
                self.list_lay.addWidget(row)
            offer = bool(videos)
        self.list_lay.addStretch(1)
        # the button is decided on every rebuild, not only where the list came out empty: a
        # scope or folder chosen later used to leave it disabled with something to save on screen
        self.save.setEnabled(offer)

    def _pretty(self, path):
        home = os.path.expanduser("~")
        p = path.replace(home, "~", 1) if path.startswith(home) else path
        return " / ".join(x for x in p.split(os.sep) if x)

    def _pick_for(self, video):
        items = [MenuItem(os.path.basename(s), lambda s=s, v=video: self._set_pair(v, s), raw=True,
                          checked=(self.pairs.get(video) == s)) for s in self._subs_cache]
        items.append(MenuItem("No subtitles", lambda v=video: self._set_pair(v, None)))
        items.append(MenuItem("Other file…", lambda v=video: self._pick_file(v)))
        PopupMenu(items, self, width=420, row=40).popup(QCursorPos())

    def _pick_file(self, video):
        path, _ = FileDialog.getOpenFileName(self, "Subtitle file", self.folder or "", "Subtitles (*.srt *.ass *.ssa *.vtt *.sub)",
                                              options=self.win.dialog_options())
        if path:
            self._set_pair(video, path)

    def _set_pair(self, video, sub):
        self.pairs[video] = sub
        for i in range(self.list_lay.count()):
            w = self.list_lay.itemAt(i).widget()
            if isinstance(w, PairRow) and w.video == video:
                w.set_sub(sub)

    def _search_online(self):
        q = self.query.edit.text().strip()
        if q:
            lang = {"English": "en", "Arabic": "ar", "Chinese": "zh-CN", "French": "fr", "German": "de", "Hindi": "hi",
                    "Italian": "it", "Japanese": "ja", "Korean": "ko", "Portuguese": "pt-PT", "Russian": "ru",
                    "Spanish": "es", "Turkish": "tr", "Urdu": "ur"}.get(self.lang.value, "en")
            webbrowser.open(f"https://www.opensubtitles.com/{lang}/search-all/q-{QUrl.toPercentEncoding(q).data().decode()}")

    def _download_link(self):
        url = self.link.edit.text().strip()
        if not url or not self.target_path or is_url(self.target_path):
            return
        reply = self.net.get(_request(url))

        def done():
            data = bytes(reply.readAll())
            reply.deleteLater()
            name = os.path.basename(QUrl(url).path()) or "subtitles.srt"
            if ext(name) not in SUB_EXT:
                name += ".srt"
            dest = os.path.join(self.folder or os.path.dirname(self.target_path), name)
            try:
                Path(dest).write_bytes(data)
            except OSError:
                self.win.notify("Couldn't save the subtitle file")
                return
            self.checked.add(dest)
            self.origin.set_value("computer")
            self.rebuild()
        reply.finished.connect(done)

    def accept_clicked(self):
        store, engine = self.win.store, self.win.engine
        if self.scope.value == "file":
            if self.target_path and not is_url(self.target_path):
                old = set(store.external_subs(self.target_path))
                store.set_external_subs(self.target_path, sorted(self.checked))
                if engine.current == self.target_path:
                    for s in sorted(self.checked - old):
                        engine.add_subtitle(s)
        else:
            n = 0
            for v, s in self.pairs.items():
                if s:
                    store.set_external_subs(v, [s])
                    n += 1
                    if engine.current == v:
                        engine.add_subtitle(s)
            if not n:
                # closing on nothing saved used to read "saved for 0 videos"; let the user
                # pair a row instead of taking the dialog away
                self.win.notify(tr("Nothing to save: no subtitles matched"))
                return
            self.win.notify(tr("Subtitles saved for {n} video").format(n=n))
        self.accept()


def QCursorPos():
    from PyQt5.QtGui import QCursor
    return QCursor.pos()


# --- Keyboard shortcuts --------------------------------------------------------------------
class KeyField(QWidget):
    """270×33 hotkey box with a pencil; click and press keys to change it."""
    changed = pyqtSignal(str)

    def __init__(self, seq=""):
        super().__init__()
        self.seq = seq
        self.capturing = False
        self.setFixedHeight(33)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.ClickFocus)

    def mousePressEvent(self, e):
        if e.x() > self.width() - 40 and self.seq and not self.capturing:
            pass
        self.capturing = True
        self.setFocus()
        self.update()

    def focusOutEvent(self, e):
        self.capturing = False
        self.update()

    def keyPressEvent(self, e):
        if not self.capturing:
            return super().keyPressEvent(e)
        k = e.key()
        if k in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta):
            return
        if k == Qt.Key_Escape:
            self.capturing = False
            self.clearFocus()
            self.update()
            return
        if k in (Qt.Key_Backspace, Qt.Key_Delete) and not e.modifiers():
            self.seq = ""
        else:
            mods = int(e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier))
            self.seq = QKeySequence(mods | k).toString()
        self.capturing = False
        self.clearFocus()
        self.update()
        self.changed.emit(self.seq)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(c("accent"), 1) if self.capturing else Qt.NoPen)
        p.setBrush(c("field"))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        f = theme.font(14)
        f.setFamily("DejaVu Sans Mono") if False else None
        p.setFont(f)
        p.setPen(c("text_hi") if self.seq else c("text_3"))
        text = tr("Press a key…") if self.capturing else (self.seq or "")
        p.drawText(QRectF(20, 0, self.width() - 60, self.height()), Qt.AlignCenter, text)
        p.drawPixmap(self.width() - 20 - 24 + 4, 4, icons.pixmap("edit", 24))


class ShortcutsDialog(Dialog):
    def __init__(self, win):
        from .actions import ACTIONS
        tools = QWidget()
        th = QHBoxLayout(tools)
        th.setContentsMargins(0, 0, 0, 0)
        th.setSpacing(10)
        self.search = ToolBox("search", placeholder="Search Keyboard Shortcuts...")
        self.fields = ToolDropdown(tr_opts(["All Fields", "Action", "Hotkey", "Global Hotkey"]), "All Fields")
        th.addWidget(self.search, 1)
        th.addWidget(self.fields)
        super().__init__(win, "Keyboard Shortcuts", 1048, 704, tools=tools)
        self.win = win
        self.keys = dict(win.hotkeys())
        self.globals = dict(win.store["global_hotkeys"])
        self.save.setEnabled(False)
        hdr = QHBoxLayout()
        for text, stretch in (("Action", 488), ("Hotkey", 332), ("Global Hotkey", 188)):
            l = Label(text, 14, "text_2")
            l.setFixedWidth(stretch)
            hdr.addWidget(l)
        self.body_lay.setSpacing(12)
        self.body_lay.addLayout(hdr)
        self.body_lay.addWidget(HLine())
        host = QWidget()
        self.rows_lay = QVBoxLayout(host)
        self.rows_lay.setContentsMargins(0, 0, 8, 0)
        self.rows_lay.setSpacing(14)
        self.rows = []
        cat = None
        for aid, category, label, default, _ in ACTIONS:
            if category != cat:
                if cat is not None:
                    self.rows_lay.addWidget(HLine())
                cl = Label(category, 16, "text")
                self.rows_lay.addWidget(cl)
                cat = category
                self.rows.append(("header", category, cl))
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            name = Label(tr(label), 14, "text_hi")
            h.addWidget(name, 1)
            k = KeyField(self.keys.get(aid, ""))
            k.setFixedWidth(270)
            k.changed.connect(lambda s, a=aid: self._set(a, s))
            h.addWidget(k)
            h.addSpacing(89)
            g = KeyField(self.globals.get(aid, ""))
            g.setFixedWidth(270)
            g.changed.connect(lambda s, a=aid: self._set_global(a, s))
            h.addWidget(g)
            self.rows_lay.addWidget(row)
            self.rows.append((aid, label, row, k, g, category))
        # What the mouse does on the video — the same settings as Preferences ▸ Controls ▸ Mouse,
        # put here too because "click the video to pause" is what people look for among shortcuts.
        self.rows_lay.addWidget(HLine())
        mouse_head = Label("Mouse", 16, "text")
        self.rows_lay.addWidget(mouse_head)
        self.rows.append(("header", "Mouse", mouse_head))
        self.mouse = {key: self.win.store.get(key) for key, _, _, _ in MOUSE_ROWS}
        for key, label, options, _default in MOUSE_ROWS:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            name = Label(tr(label), 14, "text_hi")
            h.addWidget(name, 1)
            if options is None:                       # a switch, e.g. the scroll direction
                box = QWidget()
                bh = QHBoxLayout(box)
                bh.setContentsMargins(0, 0, 0, 0)
                t = Toggle()
                t.setChecked(bool(self.mouse[key]))
                t.toggled.connect(lambda on, k=key: self._set_mouse(k, on))
                bh.addWidget(t)
                bh.addStretch(1)
                box.setFixedWidth(270)
                h.addWidget(box)
                control = t
            else:
                d = Dropdown(options, self.mouse[key], height=32)
                d.setFixedWidth(270)
                d.changed.connect(lambda v, k=key: self._set_mouse(k, v))
                h.addWidget(d)
                control = d
            h.addSpacing(89)
            filler = QWidget()
            filler.setFixedWidth(270)
            h.addWidget(filler)
            self.rows_lay.addWidget(row)
            self.rows.append((f"mouse:{key}", label, row, control, None, "Mouse"))
        self.rows_lay.addStretch(1)
        self.body_lay.addWidget(scroll(host), 1)
        self.search.edit.textChanged.connect(self._filter)
        self.fields.changed.connect(lambda _v: self._filter())

    def _set(self, aid, seq):
        if seq:
            for other, s in list(self.keys.items()):
                if other != aid and s == seq:
                    self.keys[other] = ""
                    for r in self.rows:
                        if r[0] == other:
                            r[3].seq = ""
                            r[3].update()
                    self.win.notify(tr("{seq} was moved from another action").format(seq=seq))
        self.keys[aid] = seq
        self.save.setEnabled(True)

    def _set_global(self, aid, seq):
        self.globals[aid] = seq
        self.save.setEnabled(True)

    def _set_mouse(self, key, value):
        self.mouse[key] = value
        self.save.setEnabled(True)

    def _filter(self):
        q = self.search.edit.text().lower().strip()
        field = self.fields.value
        for r in self.rows:
            if r[0] == "header":
                continue
            aid, label, row, k, g, cat = r
            # match both the English literal and what the row actually shows, so searching
            # «щелчок» finds the mouse rows and "click" does too
            name = f"{label} {tr(label)} {cat} {tr(cat)}"
            keys = f"{getattr(k, 'seq', '')} {getattr(g, 'seq', '')}"
            hay = {"Action": name, "Hotkey": getattr(k, "seq", ""),
                   "Global Hotkey": getattr(g, "seq", "")}.get(field, f"{name} {keys}")
            row.setVisible(not q or q in hay.lower())

    def accept_clicked(self):
        from .actions import ACTION
        overrides = {a: s for a, s in self.keys.items() if s != ACTION[a][3]}
        self.win.store["hotkeys"] = overrides
        self.win.store["global_hotkeys"] = {a: s for a, s in self.globals.items() if s}
        for key, value in getattr(self, "mouse", {}).items():
            self.win.store.set(key, value)          # applied right away: the surface reads it per click
        self.win.apply_shortcuts()
        self.accept()


# --- Open network stream ---------------------------------------------------------------------
class NetworkDialog(Dialog):
    def __init__(self, win, enqueue=False):
        super().__init__(win, "Open Network Stream", 1048, 581, save_text="Open")
        self.win = win
        self.enqueue = enqueue
        self.body_lay.addWidget(section("Network URL", "You can enter multiple links to load."))
        self.urls = QPlainTextEdit()
        self.urls.setPlaceholderText(tr("e.g., https://www.example.com/stream.avi"))
        self.urls.setFixedHeight(194)
        theme.styled(self.urls, lambda: f"QPlainTextEdit {{ background: {theme.hexc('field')}; "
                                        f"border: 2px solid {theme.hexc('item')}; border-radius: 6px; "
                                        f"padding: 8px 12px; }}")
        clip = QApplication.clipboard().text().strip()
        if re.match(r"^[a-z]+://\S+$", clip):
            self.urls.setPlainText(clip)
        self.body_lay.addWidget(self.urls)
        self.body_lay.addWidget(HLine())
        self.body_lay.addWidget(section("Playlist Name (optional)", "The playlist will be saved and displayed with this title."))
        self.name = Field("e.g., Favorite TV Channels")
        self.body_lay.addWidget(self.name)
        self.body_lay.addStretch(1)
        self.urls.textChanged.connect(self._update)
        self.urls.installEventFilter(self)
        self._update()
        self.net = QNetworkAccessManager(self)

    def eventFilter(self, obj, ev):
        # Enter opens (as in VLC); Shift+Enter starts another line for more links
        if obj is self.urls and ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and not ev.modifiers() & Qt.ShiftModifier:
            if self.save.isEnabled():
                self.accept_clicked()
            return True
        return super().eventFilter(obj, ev)

    def _update(self):
        self.save.setEnabled(bool(self.urls.toPlainText().strip()))

    def accept_clicked(self):
        urls = [u.strip() for u in self.urls.toPlainText().split() if u.strip()]
        urls = [u if "://" in u else "http://" + u for u in urls]
        name = self.name.text().strip()
        # only real playlist files: an HLS manifest (.m3u8, query string or not) is a stream
        # the player opens itself, not a list to download and parse
        playlist_like = [u for u in urls if re.search(r"\.(m3u|pls)(\?|$)", u, re.I)]
        if len(urls) == 1 and playlist_like:
            self._fetch_list(urls[0], name or display_name(urls[0]).rsplit(".", 1)[0].title())
            return
        if name or len(urls) > 1:
            lid = self.win.store.add_network_list(
                name or tr("Streams {when}").format(when=time.strftime("%Y-%m-%d %H:%M")),
                [{"name": display_name(u), "url": u} for u in urls])
            self.win.library.navigate(("network", "list", lid))
        else:
            self.win.store.add_stream(urls[0])
        self.win.store.add_recent_open(urls[0])
        if self.enqueue:
            self.win.enqueue(urls)
        else:
            self.win.play(urls)
        self.accept()

    def _fetch_list(self, url, name):
        self.save.setEnabled(False)
        self.save.setText(tr("Loading…"))
        reply = self.net.get(_request(url))

        def done():
            # a 404 or a refused connection is not a playlist: its HTML body used to be parsed
            # and saved as a "stream"; and a dialog the user closed has no business writing
            # lists or moving the library anywhere
            if reply.error() or not self.isVisible():
                reply.deleteLater()
                return
            text = bytes(reply.readAll()).decode("utf-8", "replace")
            reply.deleteLater()
            entries = parse_playlist_text(text, "")
            if not entries:
                self.win.notify("That link isn't a playlist; opening it directly")
                self.win.store.add_stream(url)
                self.win.play([url])
                self.accept()
                return
            lid = self.win.store.add_network_list(name, [{"name": n, "url": u} for n, u in entries])
            self.win.notify(tr("Saved “{name}” with {n}").format(
                name=esc(name), n=trn("stream", "streams", len(entries))))
            self.win.show_library_window(("network", "list", lid))
            self.accept()
        reply.finished.connect(done)


# --- New playlist ------------------------------------------------------------------------------
class DropZone(QWidget):
    filesAdded = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setMinimumHeight(186)
        self.setCursor(Qt.PointingHandCursor)
        self.files = []

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasFormat("application/x-konus-items"):
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasFormat("application/x-konus-items"):
            files = json.loads(bytes(md.data("application/x-konus-items")).decode())
        else:
            files = [u.toLocalFile() or u.toString() for u in md.urls()]
        self._add(files)

    def mouseReleaseEvent(self, e):
        files, _ = FileDialog.getOpenFileNames(self, "Select files", os.path.expanduser("~"),
                                                tr("Media ({exts});;All files (*)").format(
                                                    exts=" ".join(f"*.{x}" for x in sorted(MEDIA_EXT))),
                                                options=self.window().parent().dialog_options() if self.window().parent() else
                                                QFileDialog.Options())
        if files:
            self._add(files)

    def _add(self, files):
        def scan():
            out = []
            for f in files:
                if os.path.isdir(f):
                    out.extend(media_in_dir(f, recursive=True))
                else:
                    out.append(f)
            return out

        def done(out):
            if out is None:
                return
            self.files.extend(x for x in out if x not in self.files)
            self.update()
            self.filesAdded.emit(self.files)

        if any(os.path.isdir(f) for f in files):
            # a folder walk can take seconds; keep the dialog responsive
            tasks.run_bg(scan, on_done=done)
        else:
            done(files)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(c("field"))
        pen = QPen(c("text_3"), 1, Qt.DashLine)
        pen.setDashPattern([4, 4])
        p.setPen(pen)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        p.setFont(theme.font(14))
        p.setPen(c("text_2"))
        if not self.files:
            p.drawText(QRectF(0, self.height() / 2 - 40, self.width(), 30), Qt.AlignCenter,
                       tr("Drag files into the box or click here to select files."))
            p.setPen(Qt.NoPen)
            p.setBrush(c("item"))
            br = QRectF(self.width() / 2 - 42, self.height() / 2, 84, 30)
            p.drawRoundedRect(br, 4, 4)
            p.drawPixmap(int(br.center().x() - 12), int(br.center().y() - 12), icons.pixmap("add", 24))
        else:
            y = 12
            for f in self.files[:6]:
                p.drawText(QRectF(16, y, self.width() - 32, 22), Qt.AlignVCenter,
                           elide(display_name(f), theme.font(14), self.width() - 32))
                y += 24
            if len(self.files) > 6:
                p.drawText(QRectF(16, y, self.width() - 32, 22), Qt.AlignVCenter,
                           tr("… and {n} more").format(n=len(self.files) - 6))
            p.setPen(c("text_3"))
            p.drawText(QRectF(0, self.height() - 30, self.width() - 16, 22), Qt.AlignRight | Qt.AlignVCenter,
                       trn("file", "files", len(self.files)) + tr(" · click or drop to add more"))


class ThumbPicker(QWidget):
    def __init__(self, path=""):
        super().__init__()
        self.path = path
        self.setFixedSize(112, 112)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tr("Choose a thumbnail image"))

    def mouseReleaseEvent(self, e):
        path, _ = FileDialog.getOpenFileName(self, "Thumbnail", os.path.expanduser("~/Pictures"),
                                              "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            self.path = path
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(c("item"), 2))
        p.setBrush(c("field"))
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.drawRoundedRect(r, 6, 6)
        pm = QPixmap(self.path) if self.path else QPixmap()
        if not pm.isNull():
            from PyQt5.QtGui import QPainterPath
            path = QPainterPath()
            path.addRoundedRect(r.adjusted(2, 2, -2, -2), 5, 5)
            p.setClipPath(path)
            p.drawPixmap(r.toRect(), pm.scaled(110, 110, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(c("field_2"))
            p.drawRoundedRect(QRectF(41, 41, 30, 30), 5, 5)
            p.drawPixmap(44, 44, icons.pixmap("add", 24))


class PlaylistDialog(Dialog):
    def __init__(self, win, pid=None, items=None):
        pl = win.store.playlist(pid) if pid else None
        super().__init__(win, "Playlist Properties" if pl else "New Playlist", 713, 517)
        self.win, self.pid = win, pid
        top = QHBoxLayout()
        top.setSpacing(20)
        left = QVBoxLayout()
        left.setSpacing(20)
        left.addWidget(section("Playlist Details", "The playlist will be saved and displayed with this title and thumbnail."))
        self.name = Field("e.g., Favorite Videos", pl["name"] if pl else "")
        left.addWidget(self.name)
        top.addLayout(left, 1)
        self.thumb = ThumbPicker(pl.get("thumb", "") if pl else "")
        top.addWidget(self.thumb, 0, Qt.AlignBottom)
        self.body_lay.addLayout(top)
        self.body_lay.addWidget(HLine())
        self.body_lay.addWidget(section("Select Files", "These files will be added to the playlist." if not pl else
                                        trn("item", "items", len(pl['items'])) + ". "
                                        + tr("Files you add here are appended.")))
        self.drop = DropZone()
        if items:
            self.drop._add(items)
        self.body_lay.addWidget(self.drop, 1)
        self.name.textChanged.connect(lambda t: self.save.setEnabled(bool(t.strip())))
        self.save.setEnabled(bool(self.name.text().strip()))

    def accept_clicked(self):
        name = self.name.text().strip()
        if not name:
            return
        if self.pid:
            self.win.store.update_playlist(self.pid, name=name, thumb=self.thumb.path)
            if self.drop.files:
                self.win.store.add_to_playlist(self.pid, self.drop.files)
        else:
            self.pid = self.win.store.add_playlist(name, self.drop.files, self.thumb.path)
            self.win.notify(tr("Created “{name}”").format(name=esc(name)))
        self.accept()


# --- Open disc / capture -------------------------------------------------------------------------
class DiscDialog(Dialog):
    def __init__(self, win):
        super().__init__(win, "Open Disc", 640, 380, save_text="Play")
        self.win = win
        saved = win.store.get("optical_drive")
        drives = sysutil.optical_drives() or ([saved] if saved else [])
        self.kind = Dropdown([("dvd", "DVD"), ("bluray", "Blu-ray"), ("cdda", "Audio CD"), ("vcd", "Video CD")], "dvd",
                             height=44)
        self.drive = Dropdown(drives, saved if saved in drives else (drives[0] if drives else None), height=44)
        self.menus = Toggle()
        self.menus.setChecked(True)
        grid = QGridLayout()
        grid.setVerticalSpacing(14)
        for row, (label, w) in enumerate((("Disc type", self.kind), ("Drive", self.drive), ("Disc menus", self.menus))):
            grid.addWidget(Label(label, 14, "text_2"), row, 0)
            grid.addWidget(w, row, 1, Qt.AlignRight if isinstance(w, Toggle) else Qt.Alignment())
        grid.setColumnStretch(1, 1)
        self.body_lay.addLayout(grid)
        if not drives:
            # an empty drive list left Play offering "dvd://" — an action that cannot work
            self.body_lay.addWidget(Label("No optical drive found on this computer.", 14, "text_3"))
            self.save.setEnabled(False)
        self.body_lay.addStretch(1)

    def accept_clicked(self):
        kind, drive = self.kind.value, self.drive.value
        if not drive:
            return
        if kind == "dvd":
            mrl = f"{'dvd' if self.menus.isChecked() else 'dvdsimple'}://{drive}"
        elif kind == "bluray":
            mrl = f"bluray://{drive}"
        elif kind == "vcd":
            mrl = f"vcd://{drive}"
        else:
            mrl = f"cdda://{drive}"
        self.win.play([mrl], names={mrl: f"{self.kind.label()} ({drive})"})
        self.accept()


class CaptureDialog(Dialog):
    def __init__(self, win):
        super().__init__(win, "Open Capture Device", 640, 400, save_text="Play")
        self.win = win
        vids = ["(default camera)"]
        self.mode = Dropdown([("dshow", "Camera / capture card"), ("screen", "Desktop (screen)")], "dshow",
                             height=44)
        self.device = Dropdown(vids, vids[0], height=44)
        # dshow names the audio devices itself; this dialog only offers "no audio" separately
        sources = [("", "No audio")]
        self.audio = Dropdown(sources, "", height=44)
        self.fps = Dropdown(["10", "15", "24", "30", "60"], "30", height=44)
        grid = QGridLayout()
        grid.setVerticalSpacing(14)
        for row, (label, w) in enumerate((("Capture", self.mode), ("Video device", self.device),
                                          ("Audio", self.audio), ("Frame rate (screen)", self.fps))):
            grid.addWidget(Label(label, 14, "text_2"), row, 0)
            grid.addWidget(w, row, 1)
        grid.setColumnStretch(1, 1)
        self.body_lay.addLayout(grid)
        self.body_lay.addStretch(1)

    def accept_clicked(self):
        if self.mode.value == "screen":
            mrl = "screen://"
            opts = f":screen-fps={self.fps.value}"
            name = tr("Desktop")
        else:
            mrl = "dshow://"
            opts = ""
            name = tr("Camera")
        self.win.engine.capture_options = [opts] if opts else []
        self.win.play([mrl], names={mrl: name})
        self.accept()


# --- Convert / stream ----------------------------------------------------------------------------
PROFILES = [
    ("mp4", "Video – H.264 + AAC (MP4)", ["-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac", "-b:a", "160k"], "mp4"),
    ("mkv", "Video – H.265 + AAC (MKV)", ["-c:v", "libx265", "-crf", "24", "-c:a", "aac", "-b:a", "160k"], "mkv"),
    ("webm", "Video – VP9 + Opus (WebM)", ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-c:a", "libopus"], "webm"),
    ("copy", "Remux only – keep streams (MKV)", ["-map", "0", "-c", "copy"], "mkv"),
    ("mp3", "Audio – MP3", ["-vn", "-c:a", "libmp3lame", "-q:a", "2"], "mp3"),
    ("flac", "Audio – FLAC", ["-vn", "-c:a", "flac"], "flac"),
    ("opus", "Audio – Opus (OGG)", ["-vn", "-c:a", "libopus", "-b:a", "128k"], "opus"),
]


class ConvertDialog(Dialog):
    def __init__(self, win, items):
        super().__init__(win, "Convert", 760, 440, save_text="Start")
        self._cancelled = False           # a user's Stop is not a conversion failure
        self.win = win
        self.items = [i for i in items if not is_url(i)] or []
        self.body_lay.addWidget(section("Files", "\n".join(display_name(i) for i in self.items[:4]) +
                                        (f"\n… and {len(self.items) - 4} more" if len(self.items) > 4 else "") or
                                        "No local files selected."))
        self.profile = Dropdown([(k, label) for k, label, _, _ in PROFILES], "mp4", height=44)
        self.dest = Field("Same folder as each file")
        browse = Button("Browse", "dark", height=37)
        browse.setFixedWidth(100)
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.dest, 1)
        row.addWidget(browse)
        self.body_lay.addWidget(Label("Profile", 14, "text_2"))
        self.body_lay.addWidget(self.profile)
        self.body_lay.addWidget(Label("Save to", 14, "text_2"))
        self.body_lay.addLayout(row)
        self.status = Label("", 14, "text_2")
        self.body_lay.addWidget(self.status)
        self.body_lay.addStretch(1)
        self.proc = None
        self._out = ""
        self.queue = []
        self.save.setEnabled(bool(self.items) and bool(shutil.which("ffmpeg")))
        if not shutil.which("ffmpeg"):
            hint = "winget install ffmpeg"
            self.status.setText(tr("Converting needs ffmpeg ({why}).").format(why=hint))

    def _browse(self):
        d = FileDialog.getExistingDirectory(self, "Save converted files to", os.path.expanduser("~/Videos"),
                                             options=self.win.dialog_options())
        if d:
            self.dest.setText(d)

    def accept_clicked(self):
        prof = next(p for p in PROFILES if p[0] == self.profile.value)
        self.queue = list(self.items)
        self._failed = 0
        self._total = len(self.queue)
        self._written = set()              # names this run produced (two sources may share one)
        self._cancelled = False
        self.args, self.extn = prof[2], prof[3]
        self.save.setEnabled(False)
        self.cancel.setText(tr("Stop"))
        self._next()

    def _next(self):
        if not self.queue:
            self.cancel.setText(tr("Close"))
            if self._cancelled:
                self.status.setText(tr("Stopped."))    # the user's own Stop: neither "done"
                return                                 # nor "failed"
            if self._failed:
                self.status.setText(tr("Finished with errors: {n} of {total} failed.")
                                    .format(n=self._failed, total=self._total))
                self.win.notify(tr("Some files could not be converted."))
            else:
                self.status.setText(tr("Done."))
                self.win.notify(tr("Conversion finished"))
            return
        src = self.queue.pop(0)
        folder = self.dest.text().strip() or os.path.dirname(src)
        if not os.path.isdir(folder):
            # a missing (or file) destination used to be handed to ffmpeg and then reported
            # as a success, because the finished handler wrote "Failed." and _next() wiped it
            self._failed += 1
            self._out = ""
            self._next()
            return
        out = os.path.join(folder, os.path.splitext(os.path.basename(src))[0] + "." + self.extn)
        if os.path.abspath(out) == os.path.abspath(src):
            out = os.path.splitext(out)[0] + "-converted." + self.extn
        base, suffix = os.path.splitext(out)
        n = 2
        while out in self._written:        # two sources with the same name used to overwrite
            out = f"{base}-{n}{suffix}"    # each other, and the loss was never mentioned
            n += 1
        self._written.add(out)
        self._out = out                     # remembered so a cancelled run can be cleaned up
        self._status = tr("Converting…")
        self.status.setText(self._status + " " + display_name(src))
        self.proc = QProcess(self)
        def done(code, _status):
            if code and not self._cancelled:     # the user's own Stop is not a failure
                self._failed += 1
                self.status.setText(tr("Failed."))
            self._next()

        self.proc.finished.connect(done)
        self.proc.readyReadStandardError.connect(self._progress)
        self.duration = (self.win.media.info_now(src) or {}).get("duration", 0)
        self.proc.start("ffmpeg", ["-nostdin", "-y", "-i", src] + self.args + [out])

    def _progress(self):
        text = bytes(self.proc.readAllStandardError()).decode(errors="replace")
        m = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", text)
        if m and self.duration:
            h, mi, s = m[-1]
            t = (int(h) * 3600 + int(mi) * 60 + float(s)) * 1000
            self.status.setText(f"{self._status} ({min(100, t / self.duration * 100):.0f}%)")

    def reject(self):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self._cancelled = True
            self.queue = []
            self.proc.kill()
            self.proc.waitForFinished(2000)
            # a half-written file was left behind on cancel; remove what this run produced
            try:
                if getattr(self, "_out", "") and os.path.exists(self._out):
                    os.remove(self._out)
            except OSError:
                pass
        super().reject()


class StreamDialog(Dialog):
    """Stream out over HTTP with libVLC's stream output (a separate player)."""

    def __init__(self, win, items):
        super().__init__(win, "Stream", 760, 420, save_text="Start Streaming")
        self.win = win
        self.items = items
        s = win.store.get
        self.body_lay.addWidget(section("Stream over HTTP", "Other devices on your network can open the address below "
                                        "in VLC or a browser."))
        self.port = Field("8080", "8080")
        self.path = Field("/stream", "/stream")
        grid = QGridLayout()
        grid.addWidget(Label("Port", 14, "text_2"), 0, 0)
        grid.addWidget(self.port, 0, 1)
        grid.addWidget(Label("Path", 14, "text_2"), 1, 0)
        grid.addWidget(self.path, 1, 1)
        self.body_lay.addLayout(grid)
        self.address = Label("", 14, "accent")
        self.address.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body_lay.addWidget(self.address)
        self.body_lay.addStretch(1)
        self.player = None

    def accept_clicked(self):
        import socket
        import vlc
        s = self.win.store.get
        try:
            port = int(str(self.port.text()).strip() or 8080)
        except ValueError:
            port = 0
        if not (1024 <= port <= 65535):
            # the address line is this dialog's own place for messages; a bad port used to
            # abort the app with a ValueError from inside the slot
            self.address.setText(tr("Port must be a number between 1024 and 65535."))
            return
        path = (self.path.text() or "/stream").strip()
        if not path.startswith("/"):
            path = "/" + path
        import socket as _socket
        try:                                   # a taken port: libVLC logs "socket bind error"
            probe = _socket.socket()           # and serves nothing, so say it here rather than
            probe.bind(("0.0.0.0", port))      # announcing a stream that never came up
            probe.close()
        except OSError:
            self.address.setText(tr("Port {port} is already in use.").format(port=port))
            return
        venc = f"vcodec=h264,venc=x264{{preset={s('stream_preset').lower()}" + \
            (f",tune={s('stream_tune').lower().replace(' ', '')}" if s("stream_tune") != "None" else "") + "}," \
            if s("stream_video") else "vcodec=none,"
        aenc = "acodec=mp4a,ab=160,channels=2," if s("stream_audio") else "acodec=none,"
        senc = "scodec=dvbs,soverlay," if s("stream_subs") else ""
        chain = f"#transcode{{{venc}{aenc}{senc}}}:http{{mux=ts,dst=:{port}{path}}}"
        if s("stream_local"):
            chain = f"#transcode{{{venc}{aenc}{senc}}}:duplicate{{dst=http{{mux=ts,dst=:{port}{path}}},dst=display}}"
        self.inst = vlc.Instance(["--no-video-title-show"])
        self.player = self.inst.media_list_player_new()
        ml = self.inst.media_list_new()
        for i in self.items:
            m = self.inst.media_new(i) if is_url(i) else self.inst.media_new_path(i)
            m.add_option(":sout=" + chain)
            m.add_option(":sout-keep")
            ml.add_media(m)
            m.release()
        self.player.set_media_list(ml)
        self.player.play()
        try:
            host = socket.gethostbyname(socket.gethostname())
        except OSError:
            host = "this-computer"
        self.address.setText(f"http://{host}:{port}{path}")
        self.save.setEnabled(False)
        self.cancel.setText(tr("Stop"))
        self.win.notify("Streaming started")

    def reject(self):
        if self.player:
            self.player.stop()
            self.player.release()
            self.inst.release()
            self.player = None
        super().reject()


# --- Properties & metadata --------------------------------------------------------------------------
class PropertiesDialog(Dialog):
    FIELDS = [("Title", "Title"), ("Artist", "Artist"), ("Album", "Album"), ("Genre", "Genre"), ("Date", "Date"),
              ("TrackNumber", "Track number"), ("Description", "Comment")]

    def __init__(self, win, item):
        super().__init__(win, "Properties & Metadata", 760, 640)
        import vlc
        self.win, self.item = win, item
        self.media = None
        self.edits = {}
        grid = QGridLayout()
        grid.setVerticalSpacing(10)
        grid.setHorizontalSpacing(16)
        editable = not is_url(item)
        if editable:
            inst = win.engine.instance
            self.media = inst.media_new_path(item)
            self.media.parse_with_options(vlc.MediaParseFlag.local, 3000)
            t0 = time.time()
            while self.media.get_parsed_status() == 0 and time.time() - t0 < 3:
                QApplication.processEvents()
                time.sleep(0.02)
        self.original = {}
        for row, (key, label) in enumerate(self.FIELDS):
            val = ""
            if self.media is not None:
                v = self.media.get_meta(getattr(vlc.Meta, key))
                val = v.decode() if isinstance(v, bytes) else (v or "")
            self.original[key] = val
            f = Field("", val, height=34)
            f.setReadOnly(not editable)
            grid.addWidget(Label(label, 14, "text_2"), row, 0)
            grid.addWidget(f, row, 1)
            self.edits[key] = f
        self.body_lay.addLayout(grid)
        self.body_lay.addWidget(HLine())
        info = self._technical()
        text = "\n".join(f"{tr(k)}:  {v}" for k, v in info.items() if v)
        lab = Label(text, 13, "text_2", wrap=True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body_lay.addWidget(scroll(lab), 1)
        self.save.setEnabled(editable)

    def _technical(self):
        out = {"Location": self.item}
        if not is_url(self.item):
            try:
                out["Size"] = tr("{v} MiB").format(v=f"{os.path.getsize(self.item) / 1048576:.1f}")
            except OSError:
                pass
            if shutil.which("ffprobe"):
                try:                            # a probe slower than its timeout used to raise
                    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                    "format=duration,bit_rate:stream=codec_type,codec_long_name,width,height,"
                                    "r_frame_rate,channels,sample_rate:stream_tags=language,title", "-of", "json",
                                    self.item], capture_output=True, timeout=10,
                                       creationflags=sysutil.quiet_process())
                except (OSError, subprocess.SubprocessError):
                    r = None                    # no technical block, but the dialog still opens
                try:
                    data = json.loads(r.stdout or b"{}") if r is not None else {}
                    fmt = data.get("format", {})
                    if fmt.get("duration"):
                        out["Duration"] = fmt_time(float(fmt["duration"]) * 1000)
                    if fmt.get("bit_rate"):
                        out["Bitrate"] = f"{int(fmt['bit_rate']) // 1000} kb/s"
                    for n, st in enumerate(data.get("streams", [])):
                        t = st.get("codec_type", "")
                        desc = st.get("codec_long_name", "")
                        if t == "video":
                            fr = st.get("r_frame_rate", "0/1").split("/")
                            fps = float(fr[0]) / max(1, float(fr[1])) if len(fr) == 2 else 0
                            desc += tr(", {w}×{h}, {v} fps").format(
                                w=st.get("width"), h=st.get("height"), v=f"{fps:.3g}")
                        elif t == "audio":
                            desc += tr(", {ch} ch, {hz} Hz").format(
                                ch=st.get("channels"), hz=st.get("sample_rate"))
                        lang = (st.get("tags") or {}).get("language")
                        if lang:
                            desc += f" [{lang}]"
                        out[tr("Stream {n} ({kind})").format(n=n, kind=tr(t))] = desc
                except ValueError:
                    pass
        if self.item == self.win.engine.current and self.win.store.get("developer_mode"):
            out.update(self.win.engine.stats())
        return out

    def accept_clicked(self):
        import vlc
        if self.media is not None:
            wrote = False
            for key, f in self.edits.items():
                value = f.text().strip()
                # only what the user actually typed: libVLC falls back to the file name for an
                # empty Title, and writing every field back baked that name in as a real tag
                if value != self.original.get(key, "") and (value or self.original.get(key)):
                    self.media.set_meta(getattr(vlc.Meta, key), value)
                    wrote = True
            if not wrote:
                self.accept()
                return
            before = _file_stamp(self.item)
            self.media.save_meta()
            # libVLC answers "saved" for a read-only file — and for a folder — while writing
            # nothing, so believe the file itself
            if _file_stamp(self.item) == before:
                self.win.notify(tr("Couldn't write the tags to this file"))
            else:
                self.win.notify(tr("Metadata saved"))
            from .media import _key
            self.win.media._info.pop(_key(self.item), None)
        self.accept()

    def done(self, r):
        if self.media is not None:
            self.media.release()
            self.media = None
        super().done(r)


def _file_stamp(path):
    """(size, mtime) of a file, or None when there is none: used to tell whether a metadata
    write really happened, since libVLC's own answer is not reliable."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime)
    except OSError:
        return None


def pretty_path(path):
    """A path with the home folder shortened and the separators spaced out for a tool box."""
    home = os.path.expanduser("~")
    p = path.replace(home, "~", 1) if path.startswith(home) else path
    return " / ".join(x for x in p.split(os.sep) if x)


class PickRow(QWidget):
    """video — "3 matching tracks, click to choose one": a row the matcher left open.

    Reads like the rows around it but is a button: it opens the tracks that could belong to this
    episode (three identically named dubs, say), which is where the user answers what the matcher
    refused to guess."""

    clicked = pyqtSignal()

    def __init__(self, video, note):
        super().__init__()
        self.video, self.note = video, note
        self.setFixedHeight(36)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.pos()):
            self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(c("field"))
            p.drawRoundedRect(QRectF(self.rect()), 6, 6)
            p.drawPixmap(self.width() - 34, 6, icons.pixmap("swap", 24))
        p.drawPixmap(10, 6, icons.pixmap("audio", 24))
        f = theme.font(14)
        fm = QFontMetrics(f)
        p.setFont(f)
        half = int(self.width() * 0.55)
        name = elide(os.path.basename(self.video), f, half - 50)
        p.setPen(c("text"))
        p.drawText(QRectF(46, 0, half - 50, 36), Qt.AlignVCenter, name)
        x = 46 + fm.horizontalAdvance(name) + 14
        p.setPen(c("text_2"))
        p.drawText(QRectF(x, 0, self.width() - x - 40, 36), Qt.AlignVCenter,
                   elide(self.note, f, self.width() - x - 40))


class AudioTracksDialog(Dialog):
    """Match the audio files in a folder to its episodes by name and remember the pairs.

    The tracks of a release live in folders of their own ("RUS Sound", and under it one folder
    per dub), so those folders are listed with their track counts and the user ticks the ones to
    take tracks from — that is what tells three identically named dubs apart, and the row shows
    the folder its track came from. Every ticked folder that has the episode's own track gives
    it one, so ticking three dubs leaves three tracks on that episode: the row shows how many
    there are, the first folder listed is the one that plays, and the others are a Sound menu
    pick away while it runs. Opening the dialog again shows the folders the episodes' tracks came
    from last time, so it opens on the user's own choice. The switch by the Add button hands the
    tracks to the episode that is playing right now instead of waiting for it to start again."""

    def __init__(self, win, target_path=None, items=None):
        self.win = win
        tools = QWidget()
        th = QHBoxLayout(tools)
        th.setContentsMargins(0, 0, 0, 0)
        th.setSpacing(10)
        self.where = ToolBox("folder", editable=False)
        self.where.setCursor(Qt.PointingHandCursor)
        self.where.mousePressEvent = lambda e: self._choose_folder()
        th.addWidget(self.where, 1)
        super().__init__(win, "Match Audio Tracks", 900, 640, tools=tools, fill="base",
                         save_text="Add")
        act = QWidget()
        ah = QHBoxLayout(act)
        ah.setContentsMargins(0, 0, 14, 0)
        ah.setSpacing(10)
        ah.addWidget(Label("Activate the track", 14, "text_2"))
        self.activate = Toggle()
        self.activate.setChecked(win.store.get("activate_matched_audio") is not False)
        ah.addWidget(self.activate)
        act.setToolTip(tr("Give the matched track to the episode playing right now, instead of "
                          "applying it when that episode starts again."))
        self.footer.layout().insertWidget(2, act)      # beside Add: it is what Add does
        self.items = items
        self.target_path = target_path or win.engine.current
        self.folder = None
        if self.target_path and not is_url(self.target_path):
            self.folder = self.target_path if os.path.isdir(self.target_path)                 else os.path.dirname(self.target_path)
        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(10, 10, 10, 10)
        self.list_lay.setSpacing(4)
        self.body_lay.setContentsMargins(20, 10, 20, 0)
        self.body_lay.addWidget(scroll(self.list_host), 1)
        self.rows = {}                  # video -> (CheckRow, its tracks — the first one plays)
        self.picks = {}                 # video -> the tracks the matcher would not choose between
        self.chosen = {}                # video -> the track the user picked by hand
        self.skipped = set()            # videos the user left without a track
        self.found = []                 # [(folder, tracks)] where tracks were found
        self.checked = None             # folders to take tracks from (None: the first build)
        self.rebuild()

    def _videos(self):
        if self.items:
            return [i for i in self.items if not is_url(i) and ext(i) in VIDEO_EXT]
        return media_in_dir(self.folder, VIDEO_EXT) if self.folder else []

    def _choose_folder(self):
        d = FileDialog.getExistingDirectory(self, "Folder with the episodes and their tracks",
                                            self.folder or os.path.expanduser("~"),
                                            options=self.win.dialog_options())
        if d:
            self.folder = d
            self.checked = None         # another folder: its own folders, and what its episodes use
            self.chosen.clear()
            self.skipped.clear()
            self.rebuild()

    def _folders(self):
        """The ticked folders, in the order they are listed: that order is what decides between
        two equal tracks of different folders, so it has to be the one the user sees."""
        return [f for f, _ in self.found if f in (self.checked or ())]

    def _tracks(self):
        out = []
        for folder in self._folders():
            out += media_in_dir(folder, AUDIO_EXT)
        return out

    def _folder_toggled(self, folder, on):
        if on:
            self.checked.add(folder)
        else:
            self.checked.discard(folder)
        self.rebuild()

    def rebuild(self):
        self.where.edit.setText(pretty_path(self.folder) if self.folder else tr("Choose a folder…"))
        while self.list_lay.count():
            w = self.list_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.rows = {}
        self.picks = {}
        videos = self._videos()
        if not videos:
            self.list_lay.addWidget(Label("No videos in this folder.", 14, "text_3"))
            self.list_lay.addStretch(1)
            self.save.setEnabled(False)          # nothing to match: Save must not offer itself
            self.count_note(0)                   # the count was left over from the previous folder
            return
        self.found = track_folders(self.folder) if self.folder else []
        here = {f for f, _ in self.found}
        if self.checked is None:
            # The folders the episodes' own tracks come from: opening the dialog again shows the
            # choice made last time (which dub, or which "RUS Sound") rather than every folder
            # that happens to hold audio.
            self.checked = self._stored_folders(videos, here) or set(here)
        else:
            self.checked &= here
        if self.found:
            self.list_lay.addWidget(Label("Folders with tracks", 14, "text_2"))
            for folder, n in self.found:
                row = CheckRow(f"{self._folder_name(folder)}   —   {trn('track', 'tracks', n)}",
                               folder in self.checked, icon="folder")
                row.setToolTip(folder)
                row.toggled.connect(lambda on, f=folder: self._folder_toggled(f, on))
                self.list_lay.addWidget(row)
            # What the ticks add up to, said once here rather than in every row: the count
            # beside an episode is this many tracks, and the menu is where the rest of them are.
            self.list_lay.addWidget(Label(
                "Every ticked folder gives each episode its own track — the first one plays, "
                "the rest are under Audio → Audio Track.", 14, "text_3", wrap=True))
            self.list_lay.addSpacing(8)
        else:
            self.list_lay.addWidget(Label("No audio files in this folder.", 14, "text_3"))
        self._episodes(videos)
        self.save.setEnabled(bool(self.rows) or bool(self.skipped))
        self.count_note(len(videos))
        self.list_lay.addStretch(1)

    def _stored_folders(self, videos, here):
        """The folders of `here` the episodes' remembered tracks live in."""
        want = set()
        for v in videos:
            for track in self.win.store.external_audio(v):
                folder = os.path.dirname(track)
                if folder in here:
                    want.add(folder)
        return want

    def _episodes(self, videos):
        """The episode rows, with the tracks each one gets: matched — one per ticked folder, the
        count shown on the row and the first of them the one that plays — left to the user to
        pick (several tracks of the ticked folders look equally right), or none at all.

        An episode that already uses a track of a ticked folder keeps it in front: the dialog
        opens on what the episodes use, so pressing Add again changes nothing. Untick that
        folder and the episode is matched afresh, which is how a season moves to another dub."""
        tracks = self._tracks()
        pairs = dict(match_audio_tracks(videos, self._folders())) if tracks else {}
        for v in videos:
            cands = track_candidates(v, tracks)
            if v in self.skipped:
                got = []
            elif v in self.chosen:
                got = [self.chosen[v]]
            else:
                got = self._episode_tracks(v, pairs.get(v) or [], cands)
            if got:
                row = CheckRow(f"{os.path.basename(v)}   —   {self._track_name(got[0])}", True,
                               icon="audio", note=self._tracks_note(got))
                row.setToolTip("\n".join(self._track_name(t) for t in got))
                self.rows[v] = (row, got)
                self.list_lay.addWidget(row)
                continue
            if cands:
                note = (tr("no track") if v in self.skipped
                        else trn("matching track", "matching tracks", len(cands))) \
                    + ", " + tr("click to choose")
                row = PickRow(v, note)
                row.setToolTip("\n".join(self._track_name(t) for t in cands))
                row.clicked.connect(lambda v=v: self._choose(v))
                self.picks[v] = cands
                self.list_lay.addWidget(row)
            else:
                self.list_lay.addWidget(Label(f"{os.path.basename(v)}   —   " + tr("no matching track found"),
                                              14, "text_3"))

    def _episode_tracks(self, video, matched, cands):
        """What this episode gets from the ticked folders: every track the matcher found, with
        the one the episode already uses in front — that is the one that plays, and the others
        are a Sound → Audio Track pick away. An episode whose tracks the matcher would not
        choose between (none found, but the folders hold candidates) keeps what it has, so a
        pick made by hand survives the next open."""
        stored = [t for t in self.win.store.external_audio(video) if t in cands]
        if not matched:
            return stored
        kept = [t for t in stored if t in matched]
        return kept + [t for t in matched if t not in kept]

    def _tracks_note(self, tracks):
        """The count beside an episode that got more than one track — the folders it is ticked
        for all had one for it, and the row says so instead of quietly showing the first."""
        return trn("track", "tracks", len(tracks)) if len(tracks) > 1 else ""

    def _folder_name(self, folder):
        """A path the user knows from Explorer — "RUS Sound/AniLibria", or the folder being
        scanned itself — rather than a full one running off the edge of the list."""
        rel = os.path.relpath(folder, self.folder) if self.folder else folder
        return tr("This folder") if rel == "." else rel

    def _track_name(self, track):
        """The track's own folder relative to the one being scanned, when it is in a subfolder."""
        if self.folder and os.path.dirname(track) != self.folder:
            return os.path.relpath(track, self.folder)
        return os.path.basename(track)

    def _choose(self, video):
        """The row the matcher left open: pick this episode's track by hand."""
        items = [MenuItem(self._track_name(t), lambda t=t, v=video: self._pick(v, t), raw=True,
                          checked=self.chosen.get(video) == t) for t in self.picks.get(video, [])]
        items += [sep(), MenuItem("No track", lambda v=video: self._pick(v, None),
                                  icon="remove", icon_variant="")]
        PopupMenu(items, self, width=560, row=40).popup(QCursorPos())

    def _pick(self, video, track):
        if track is None:
            self.chosen.pop(video, None)
            self.skipped.add(video)
        else:
            self.skipped.discard(video)
            self.chosen[video] = track
        self.rebuild()

    def count_note(self, total):
        self.title_label.setText(tr("Match Audio Tracks") +
                                 (f" — {trn('episode', 'episodes', total)}" if total else ""))

    def accept_clicked(self):
        store, engine = self.win.store, self.win.engine
        store.set("activate_matched_audio", self.activate.isChecked())
        saved, touched = 0, False
        for video, (row, tracks) in self.rows.items():
            # a file that disappeared between the scan and Add is not remembered as this
            # episode's track (the store filters those out on read, so it was a dead entry)
            want = [t for t in tracks if os.path.exists(t)] if row.checked else []
            saved += 1 if want else 0
            if store.external_audio(video) != want:
                store.set_external_audio(video, want)
                touched = touched or video == engine.current
        for video in self.skipped:
            if store.external_audio(video):
                store.set_external_audio(video, [])
                touched = touched or video == engine.current
        self.win.notify(trn("audio track remembered", "audio tracks remembered", saved) if saved
                        else tr("Added audio tracks cleared"))
        if touched and self.activate.isChecked() and engine.is_active():
            # The episode playing right now: its new tracks take over at once (the others get
            # theirs when they start, see engine._start_item)
            if store.external_audio(engine.current):
                engine.start_external_audio()
            else:
                engine.stop_external_audio()
        self.accept()


class AboutDialog(Dialog):
    def __init__(self, win):
        super().__init__(win, "About Konus", 560, 430, footer=False)
        v = self.body_lay
        v.setSpacing(10)
        logo = Label("")
        logo.setPixmap(icons.pixmap("logo", 72))
        logo.setAlignment(Qt.AlignCenter)
        v.addWidget(logo)
        t = Label(f"{APP_NAME} {__version__}", 20, "text_hi")
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        import vlc
        ver = vlc.libvlc_get_version()
        ver = ver.decode() if isinstance(ver, bytes) else ver
        for text in (tr("Plays everything through libVLC {ver}.").format(ver=ver),
                     "Interface design: “VLC Redesign” by Juxtopposed, CC BY 4.0.",
                     "Icons from the same design, apart from the app's own mark. Font: Inter (SIL Open Font License).",
                     "Not affiliated with VideoLAN: VLC and the cone are their trademarks, and this player uses neither.",
                     "Own code MIT; with PyQt5 (GPLv3) the whole player is GPLv3."):
            l = Label(text, 14, "text_2", wrap=True)
            l.setAlignment(Qt.AlignCenter)
            v.addWidget(l)
        row = QHBoxLayout()
        row.addStretch(1)
        for label, url in (("The redesign video", "https://youtu.be/ERFeOgqk324"),
                           ("Figma file", "https://www.figma.com/community/file/1691465999134370980"),
                           ("Donate to VideoLAN", "https://www.videolan.org/contribute.html")):
            b = Button(label, "dark", height=32, bold=False)
            b.clicked.connect(lambda _=False, u=url: webbrowser.open(u))
            row.addWidget(b)
        row.addStretch(1)
        v.addStretch(1)
        v.addLayout(row)


class HomeDialog(Dialog):
    """Customize Home: switch the home page's sections on and off (applies right away)."""

    def __init__(self, lib):
        super().__init__(lib.win, "Customize Home", 520, 120 + 52 * 5, save_text="Done")
        from .library import HOME_SECTIONS
        self.cancel.hide()
        self.body_lay.addWidget(section("Sections", "With every section off, Home shows just the player's own mark."))
        grid = QGridLayout()
        grid.setVerticalSpacing(14)
        hidden = set(lib.store["home_hidden"])
        for row, title in enumerate(HOME_SECTIONS):
            t = Toggle()
            t.setChecked(title not in hidden)
            t.toggled.connect(lambda on, title=title: lib.set_home_section(title, on))
            grid.addWidget(Label(title, 14, "text_hi"), row, 0)
            grid.addWidget(t, row, 1, Qt.AlignRight)
        grid.setColumnStretch(0, 1)
        self.body_lay.addLayout(grid)
        self.body_lay.addStretch(1)


class MessageDialog(Dialog):
    def __init__(self, win, title, text, detail="", ok="OK", cancel="Cancel"):
        super().__init__(win, title, 520, 220, save_text=ok, cancel_text=cancel)
        # plain text: the messages carry user data (a playlist's name, a path), and QLabel would
        # otherwise read it as markup
        # A long message (an error with a path in it, a playlist named by the user) used to be
        # cut off mid-word with no way to read or copy the rest: the body scrolls instead.
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        for value, size, colour in ((text, 16, "text_hi"), (detail, 14, "text_2")):
            if not value:
                continue
            line = Label(value, size, colour, wrap=True)
            line.setTextFormat(Qt.PlainText)
            lay.addWidget(line)
        lay.addStretch(1)
        self.body_lay.addWidget(scroll(body), 1)
        if not cancel:
            self.cancel.hide()
