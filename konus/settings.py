"""The Settings dialog (1048×865): searchable categories on the left, rows built from
prefs.CATEGORIES on the right, every row with an (i) explanation, "Advanced Options ›"
sections folded away."""

import glob
import os
import shutil
import subprocess
from pathlib import Path

from PyQt5.QtCore import QPoint, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFontDatabase, QFontMetrics, QPainter
from PyQt5.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QLineEdit, QScrollArea, QVBoxLayout,
                             QWidget)

from . import icons, prefs, sysutil, theme
from . import i18n
from .i18n import tr, tr_opts
from .store import config_dir
from .theme import ACCENTS, c
from .widgets import (Button, ColorField, Dropdown, Expander, HLine, IconButton, InfoIcon, Label, NumberField, Segmented, Slider, StartVolume, Toggle, VSlider, clamp_position, clamp_to_screen, elide, paint_shadow)
from .filedialog import FileDialog


class AccentPicker(QWidget):
    changed = pyqtSignal(str)

    def __init__(self, value):
        super().__init__()
        self.value = value
        self.setFixedSize(318, 36)
        self.setCursor(Qt.PointingHandCursor)

    def set_value(self, v):
        self.value = v
        self.update()

    def _rects(self):
        w = (self.width() - 4) / len(ACCENTS)
        return [QRectF(2 + i * w, 2, w, self.height() - 4) for i in range(len(ACCENTS))]

    def mousePressEvent(self, e):
        for col, r in zip(ACCENTS, self._rects()):
            if r.contains(e.pos()):
                self.value = col
                self.update()
                self.changed.emit(col)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(c("field"))
        p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        for col, r in zip(ACCENTS, self._rects()):
            sel = col.lower() == (self.value or "").lower()
            if sel:
                p.setBrush(theme.mix(col, theme.T.p["field"], 0.78))
                p.drawRoundedRect(r, 4, 4)
            sw = QRectF(r.center().x() - 10, r.center().y() - 10, 20, 20)
            q = QColor(col)
            if not sel:
                q.setAlphaF(0.7)
            p.setBrush(q)
            p.drawRoundedRect(sw, 1, 1)


class PathRow(QWidget):
    changed = pyqtSignal(str)

    def __init__(self, value, mode="dir", parent_win=None):
        super().__init__()
        self.mode = mode
        self.win = parent_win
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        self.edit = QLineEdit(value or "")
        # 36 like the other fields: the design draws the input's frame at the height of a
        # dropdown's, with the Browse button beside it 32 tall, centred
        self.edit.setFixedHeight(36)
        self.edit.setFixedWidth(206)
        self.edit.editingFinished.connect(lambda: self.changed.emit(self.edit.text().strip()))
        h.addWidget(self.edit)
        b = Button("Browse", "face", height=32)
        b.setFixedWidth(100)
        b.clicked.connect(self._browse)
        h.addWidget(b)

    def _browse(self):
        start = os.path.expanduser(self.edit.text() or "~")
        opts = self.win.dialog_options() if self.win else QFileDialog.Options()
        if self.mode == "dir":
            p = FileDialog.getExistingDirectory(self, "Choose a folder", start, options=opts)
        elif self.mode == "save":
            p, _ = FileDialog.getSaveFileName(self, "Choose a file", start, options=opts)
        else:
            p, _ = FileDialog.getOpenFileName(self, "Choose a file", start, options=opts)
        if p:
            self.edit.setText(p)
            self.changed.emit(p)


class EqWidget(QWidget):
    """Open/Close Equalizer bar with 11 vertical sliders (preamp + 10 bands)."""
    changed = pyqtSignal(list)

    def __init__(self, bands):
        super().__init__()
        # exactly the eleven sliders the panel is made of: a short or non-numeric stored list
        # (a profile from another build) used to raise while the widget was built
        self.bands = ([(b if isinstance(b, (int, float)) and not isinstance(b, bool) else 0.0)
                       for b in bands] + [0.0] * 11)[:11]
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.toggle = Expander("Open Equalizer", open_text="Close Equalizer", dark=True, height=36)
        v.addWidget(self.toggle)
        self.panel = QWidget()
        pv = QVBoxLayout(self.panel)
        pv.setContentsMargins(22, 16, 22, 20)
        row = QHBoxLayout()
        row.setSpacing(0)
        self.sliders, self.values = [], []
        for i, name in enumerate(["Preamp"] + prefs.EQ_BANDS):
            col = QVBoxLayout()
            col.setSpacing(8)
            s = VSlider(-20, 20, self.bands[i])
            s.setFixedHeight(320)
            s.changed.connect(lambda val, i=i: self._set(i, val))
            col.addWidget(s, 0, Qt.AlignHCenter)
            col.addWidget(Label(name, 14, "text_2"), 0, Qt.AlignHCenter)
            vl = Label(tr("{v} dB").format(v=f"{self.bands[i]:.1f}"), 12, "text")
            vl.setAlignment(Qt.AlignCenter)
            vl.setFixedSize(60, 30)
            vl.setStyleSheet(f"background: {theme.hexc('field')}; border-radius: 4px; color: {theme.hexc('text')};")
            col.addWidget(vl, 0, Qt.AlignHCenter)
            row.addLayout(col)
            self.sliders.append(s)
            self.values.append(vl)
        pv.addLayout(row)
        v.addWidget(self.panel)
        self.panel.hide()
        self.toggle.toggled.connect(self.panel.setVisible)

    def set_bands(self, bands):
        self.bands = list(bands)
        for s, l, b in zip(self.sliders, self.values, self.bands):
            s.set_value(b)
            l.setText(tr("{v} dB").format(v=f"{b:.1f}"))

    def _set(self, i, val):
        self.bands[i] = val
        self.values[i].setText(tr("{v} dB").format(v=f"{val:.1f}"))
        self.changed.emit(list(self.bands))

    def paintEvent(self, e):
        if self.panel.isVisible():
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(c("rail"))
            p.drawRoundedRect(QRectF(self.panel.geometry()), 6, 6)


class CategoryItem(QWidget):
    clicked = pyqtSignal()

    def __init__(self, label, icon):
        super().__init__()
        self.label, self.icon = tr(label), icon
        self.selected = False
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.selected or self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(c("hover_2") if self.selected else c("hover"))
            p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        p.drawPixmap(8, 8, icons.pixmap(self.icon, 24, None, "active" if self.selected else ""))
        p.setFont(theme.font(14))
        p.setPen(c("text_max") if self.selected else c("icon"))
        p.drawText(QRectF(44, 0, self.width() - 48, 40), Qt.AlignVCenter, self.label)


class SettingsDialog(QDialog):
    MARGIN = 24

    def __init__(self, win, category="general"):
        super().__init__(win, Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.win = win
        self.store = win.store
        self.controls = {}
        m = self.MARGIN
        self.resize(1048 + 2 * m, 865 + 2 * m)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(m, m, m, m)
        outer.setSpacing(0)
        # header
        head = QWidget()
        head.setFixedHeight(50)
        hh = QHBoxLayout(head)
        hh.setContentsMargins(8, 8, 14, 8)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search"))
        self.search.setFixedSize(200, 34)          # the rail card's width: see _align_search
        self.search.addAction(icons.icon("search", 24), QLineEdit.LeadingPosition)
        self.search.setStyleSheet(f"QLineEdit {{ background: {theme.hexc('field_2')}; border-radius: 6px; "
                                  f"padding-left: 8px; }}")
        self.search.textChanged.connect(self._search)
        hh.addWidget(self.search)
        hh.addSpacing(32)
        self.title = Label("", 16, "text")
        hh.addWidget(self.title, 1)
        close = IconButton("x", "Close", box=(32, 32))
        close.clicked.connect(self.accept)
        hh.addWidget(close)
        outer.addWidget(head)
        self.head = head
        body = QHBoxLayout()
        body.setSpacing(0)
        nav = QWidget()
        nv = QVBoxLayout(nav)
        nv.setContentsMargins(8, 8, 8, 8)
        self.navcard = QWidget()
        cv = QVBoxLayout(self.navcard)
        cv.setContentsMargins(6, 6, 6, 6)
        cv.setSpacing(8)
        self.items = {}
        group = None
        for cat in prefs.CATEGORIES:
            if cat.group != group:
                g = Label(tr("group:" + cat.group), 14, "text_2")
                g.setContentsMargins(10, 8, 10, 8)
                cv.addWidget(g)
                group = cat.group
            it = CategoryItem(cat.label, cat.icon)
            it.clicked.connect(lambda k=cat.key: self.show_category(k))
            cv.addWidget(it)
            self.items[cat.key] = it
        cv.addStretch(1)
        # 216 is the width of the rail column in the design: a 200 px card with 8 px either side,
        # and the search field above it the width of that same card. A language with longer names
        # needs more than the design does — the longest label starts 50 px into the card and has
        # to fit in what is left (the card is then that much wider, and the field follows it: see
        # _align_search) — and the rail's bar takes 10 px of the column when it appears.
        fm = QFontMetrics(theme.font(14))
        widest = max(fm.horizontalAdvance(it.label) for it in self.items.values())
        nav.setFixedWidth(max(216, widest + 86))
        navscroll = QScrollArea()
        navscroll.setWidgetResizable(True)
        navscroll.setFrameShape(QScrollArea.NoFrame)
        navscroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        navscroll.setWidget(self.navcard)
        navscroll.verticalScrollBar().rangeChanged.connect(lambda *_: self._align_search())
        nv.addWidget(navscroll, 1)
        body.addWidget(nav)
        self.nav = nav
        self.navscroll = navscroll
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        # as needed, not off: on a narrow screen (the card is clamped there) the form is wider
        # than the viewport, and its right-hand column could not be reached at all
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.area.viewport().setAutoFillBackground(False)
        body.addWidget(self.area, 1)
        outer.addLayout(body, 1)
        self._drag = None
        self.current_category = None       # the page in view, for coming back to it (see _set)
        self.reopen = None                 # set when the dialog asks to be opened again
        self.show_category(category if category in prefs.CATEGORY else "general")
        self.store.settingChanged.connect(self._external_change)

    # --- layout --------------------------------------------------------------
    def _align_search(self):
        """The search field takes the width of the rail card, so the two boxes stand on the same
        two lines — that is how the design draws them. The card loses 10 px to the rail's bar
        when the rail overflows (a short screen), and the field follows it there."""
        self.search.setFixedWidth(max(120, self.navcard.width()))

    def show_category(self, key):
        self.current_category = key
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        for k, it in self.items.items():
            it.selected = k == key
            it.update()
        cat = prefs.CATEGORY[key]
        self.title.setText(cat.title)
        self._build([(cat, cat.sections)])

    def _search(self, text):
        q = text.lower().strip()
        if not q:
            sel = next((k for k, it in self.items.items() if it.selected),
                       getattr(self, "current_category", None) or "general")
            self.show_category(sel)
            return
        groups = []
        for cat in prefs.CATEGORIES:
            secs = []
            for s in cat.sections:
                rows = [r for r in s.rows if r.label and (q in r.label.lower() or q in r.info.lower()
                                                          or q in tr(r.label).lower() or q in tr(r.info).lower())]
                if rows:
                    secs.append(prefs.Section(s.title, rows, False))
            if secs:
                groups.append((cat, secs))
        self.title.setText(tr("Search: {q}").format(q=text))
        for it in self.items.values():
            it.selected = False
            it.update()
        self._build(groups, searching=True)

    def _build(self, groups, searching=False):
        self.controls = {}
        page = QWidget()
        v = QVBoxLayout(page)
        # the pane's own padding in the design: 33 px to the first label's column, 26 to the
        # right edge of the controls; the 29 px before the first heading (below) is what sets
        # the 33 px between the header and that heading, so nothing is added on top here
        v.setContentsMargins(33, 0, 26, 30)
        v.setSpacing(0)
        if not groups:
            v.addWidget(Label("No settings match.", 14, "text_2"))
        for cat, sections in groups:
            if searching:
                v.addSpacing(8)
                v.addWidget(Label(cat.title, 13, "text_2"))
            adv_host = None
            for s in sections:
                if s.advanced and not searching:
                    if adv_host is None:
                        v.addSpacing(20)
                        exp = Expander("Advanced Options", height=38)
                        v.addWidget(exp)
                        adv_host = QWidget()
                        adv_lay = QVBoxLayout(adv_host)
                        adv_lay.setContentsMargins(0, 0, 0, 0)
                        adv_lay.setSpacing(0)
                        adv_host.hide()
                        exp.toggled.connect(adv_host.setVisible)
                        v.addWidget(adv_host)
                    target = adv_lay
                    if s.title == "Developer Options" and not self.store.get("developer_mode"):
                        continue
                else:
                    target = v
                if s.title != "Advanced Options":
                    target.addSpacing(29)
                    target.addWidget(Label(s.title, 16, "text_max", weight=theme.MEDIUM))
                    target.addSpacing(10)
                for r in s.rows:
                    if r.extra.get("hidden"):
                        continue
                    w = self._row(r)
                    if w is not None:
                        target.addWidget(w)
        v.addStretch(1)
        self.area.setWidget(page)
        self._sync_dependents()

    def _row(self, r):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 5, 0, 5)
        h.setSpacing(10)
        if r.kind in ("eq", "addons"):
            return self._control(r)
        if r.kind == "slider" and r.extra.get("labels"):
            row.setMinimumHeight(56)
        lab = Label(r.label, 14, "text")
        h.addWidget(lab)
        if r.info or r.disabled:
            # the design keeps a gap of its own between a label and its (i): the label's widget
            # is a little wider than its text, so the row's own spacing is not that gap
            h.addSpacing(8)
            # The two halves are translated before they are joined: the table has no key with the
            # "<br><br><i>" in it, so every row with a reason used to show English in Russian.
            info = tr(r.info) if r.info else ""
            if r.disabled:
                # a reason may come in parts (a shared prefix and its own sentence): each part is
                # a key of its own, so the whole thing has to be translated in pieces
                parts = r.disabled if isinstance(r.disabled, tuple) else (r.disabled,)
                reason = " ".join(tr(p) for p in parts)
                info += ("<br><br><i>" + reason + "</i>") if info else ("<i>" + reason + "</i>")
            h.addWidget(InfoIcon(info, translated=True))
        h.addStretch(1)
        w = self._control(r)
        if w is not None:
            h.addWidget(w)
        if r.disabled:
            lab.set_color("text_3")
            if w is not None:
                w.setEnabled(False)
        row.row_key = r.key
        row.label_widget = lab
        self.controls.setdefault(r.key + "__row", row)
        return row

    def _control(self, r):
        s = self.store
        val = s.get(r.key) if r.key in prefs.DEFAULTS else None
        k = r.kind
        if k == "toggle":
            w = Toggle()
            w.setChecked(bool(val))
            w.toggled.connect(lambda on, key=r.key: self._set(key, on))
        elif k == "dropdown":
            opts = list(r.options)
            dyn = r.extra.get("dynamic")
            if dyn == "screens":
                opts = ["Current screen"] + [f"{sc.name()} ({sc.geometry().width()}×{sc.geometry().height()})"
                                             for sc in QApplication.screens()]
            elif dyn == "drives":
                found = sysutil.optical_drives()
                # no drive here: a word in the dropdown rather than an empty list
                opts = found or [("", tr("No optical drive found"))]
            elif dyn == "audio_devices":
                opts = [("System default", "System default")] + [(d, desc) for d, desc in self.win.engine.audio_devices()]
            if r.key == "aspect":
                opts = self.win.aspect_options()
            elif r.key == "crop":
                opts = self.win.crop_options()
            opts = tr_opts(opts)          # labels only: stored values stay English
            icon_for = None
            if r.extra.get("eqicons"):
                icon_for = None
            if r.extra.get("vizicons"):
                vi = {"Spectometer": "spectometer", "Scope": "scope", "Spectrum": "spectrum", "Waveform": "waveform"}
                icon_for = lambda v, vi=vi: vi.get(v)
            w = Dropdown(opts, val, height=36, icon_for=icon_for)
            w.setFixedWidth(318)
            w.changed.connect(lambda v, key=r.key: self._set(key, v))
        elif k == "segmented":
            w = Segmented(tr_opts(r.options), val, height=36)
            w.setFixedWidth(318)
            w.changed.connect(lambda v, key=r.key: self._set(key, v))
        elif k == "accent":
            w = AccentPicker(val)
            w.changed.connect(lambda v: self._set("accent", v))
        elif k == "slider":
            ex = r.extra
            w = Slider(ex.get("min", 0), ex.get("max", 1), val, ex.get("step", 0), ex.get("ticks", 0), ex.get("labels"),
                       centered=ex.get("centered", False), mark=ex.get("mark"))
            w.setFixedWidth(318)
            w.released.connect(lambda v, key=r.key: self._set(key, v))
            if r.apply == "runtime":
                w.changed.connect(lambda v, key=r.key: self._set(key, v))
            suffix = tr(ex.get("suffix", ""))
            w.setToolTip(f"{val:g}{suffix}")
            w.changed.connect(lambda v, w=w, suffix=suffix: w.setToolTip(f"{v:g}{suffix}"))
        elif k == "number":
            ex = r.extra
            dec = 0 if isinstance(r.default, int) else (2 if ex.get("step", 1) < 0.1 else 1)
            w = NumberField(val, ex.get("min", -1e9), ex.get("max", 1e9), ex.get("step", 1), dec, ex.get("suffix", ""))
            w.setFixedWidth(318)
            w.valueChanged.connect(lambda v, key=r.key, isint=isinstance(r.default, int):
                                   self._set(key, int(v) if isint else v))
        elif k == "text":
            w = QLineEdit(str(val or ""))
            w.setFixedSize(318, 32)
            if r.extra.get("password"):
                w.setEchoMode(QLineEdit.Password)
            if r.extra.get("placeholder"):
                w.setPlaceholderText(tr(r.extra["placeholder"]))
            w.editingFinished.connect(lambda key=r.key, w=w: self._set(key, w.text().strip()))
        elif k == "font":
            fams = [""] + sorted(set(QFontDatabase().families()))
            if r.extra.get("mono"):
                fams = [""] + [f for f in fams[1:] if QFontDatabase().isFixedPitch(f)]
            w = Dropdown([(f, f or "System default") for f in fams], val or "", height=36)
            w.setFixedWidth(318)
            w.changed.connect(lambda v, key=r.key: self._set(key, v))
        elif k == "color":
            w = ColorField(val)
            w.setFixedWidth(318)
            w.changed.connect(lambda v, key=r.key: self._set(key, v))
        elif k == "path":
            w = PathRow(val, r.extra.get("mode", "file"), self.win)
            w.changed.connect(lambda v, key=r.key: self._set(key, v))
        elif k == "startvol":
            w = StartVolume(val)
            w.setFixedWidth(320)
            w.changed.connect(lambda v: self._set("startup_volume", v))
        elif k == "button":
            w = Button(r.default, "ghost", height=32)
            w.setFixedWidth(318 if r.extra.get("wide") else max(100, w.sizeHint().width()))
            w.clicked.connect(lambda _=False, key=r.key: self._button(key))
        elif k == "eq":
            w = EqWidget(val)
            w.changed.connect(lambda b: self._set("eq_bands", b))
            self.controls["eq_bands"] = w
            return w
        elif k == "addons":
            return self._addons()
        else:
            return None
        self.controls[r.key] = w
        return w

    def _set(self, key, value):
        self.store.set(key, value)
        if key == "language":
            # The interface is built with the words it shows (see konus.i18n), so a language
            # switch means building it again — this dialog along with it: it closes, the window
            # is rebuilt, and the dialog opens again on the page it was on (see
            # MainWindow.apply_language). Deferred: the dropdown that sent this is still closing.
            self.reopen = self.current_category
            QTimer.singleShot(0, self.accept)
            return
        if key in ("theme", "accent"):
            self.win.apply_theme()
            QTimer.singleShot(0, lambda: self.show_category(next((k for k, it in self.items.items() if it.selected),
                                                                 "general")))
        if key == "developer_mode":
            QTimer.singleShot(0, lambda: self.show_category("general"))
        if key == "eq_preset" and value != "None":
            bands = self.win.engine.eq_preset_bands(value)
            self.store.set("eq_bands", bands)
            if "eq_bands" in self.controls:
                self.controls["eq_bands"].set_bands(bands)
            self.store.set("eq_enabled", True)
            if "eq_enabled" in self.controls:
                self.controls["eq_enabled"].setChecked(True)
        if key == "pic_reset":
            pass
        self._sync_dependents()

    def _external_change(self, key, value):
        w = self.controls.get(key)
        if w is None:
            return
        try:
            if isinstance(w, Toggle):
                w.blockSignals(True)
                w.setChecked(bool(value))
                w.blockSignals(False)
                w.update()
            elif hasattr(w, "set_value"):
                w.set_value(value)
        except RuntimeError:
            pass

    def _sync_dependents(self):
        s = self.store
        for key, dep in (("single_from_fm", "single_window"), ("enqueue_opened", "single_window")):
            w = self.controls.get(key)
            if w is not None:
                w.setEnabled(bool(s.get(dep)))
        custom = s.get("buffering") == "Custom"
        for key in ("cache_file", "cache_disc", "cache_net", "cache_live"):
            w = self.controls.get(key)
            row = self.controls.get(key + "__row")
            if w is not None:
                w.setEnabled(custom)
            if row is not None:
                row.label_widget.set_color("text" if custom else "text_3")
        px = s.get("sub_size_mode") == "pixels"
        w = self.controls.get("sub_size")
        if w is not None:
            w.setEnabled(not px)
        w = self.controls.get("sub_px")           # the pixel size is the editable one in that mode
        if w is not None:
            w.setEnabled(px)

    def _button(self, key):
        w = self.win
        if key == "open_config":
            sysutil.open_path(config_dir())
        elif key == "reset_all":
            if w.confirm(tr("Reset all settings?"),
                         tr("Every setting of the “{profile}” profile goes back to its default. "
                            "Your library and playlists are kept.").format(profile=self.store.profile)):
                def effective(value):
                    code = i18n.code_for(value)
                    return i18n.detect_system() if code == "system" else code

                before = self.store.get("language")
                self.store.reset_settings()
                if effective(self.store.get("language")) != effective(before):
                    # the stored language changed: rebuild the interface like the dropdown does
                    # (i18n only follows when the window is built again)
                    self.reopen = self.current_category
                    QTimer.singleShot(0, self.accept)
                w.apply_theme()
                self.show_category("general")
        elif key == "clear_history":
            if w.confirm(tr("Clear history?"),
                         tr("Recently played items and saved positions will be forgotten.")):
                self.store.clear_history()
                w.notify("History cleared")
        elif key == "edit_shortcuts":
            w.open_shortcuts()
        elif key == "reset_shortcuts":
            if w.confirm(tr("Reset all shortcuts?"), tr("Every hotkey goes back to its default.")):
                self.store["hotkeys"] = {}
                self.store["global_hotkeys"] = {}
                w.apply_shortcuts()
                w.notify("Shortcuts reset")
        elif key == "pic_reset":
            for k in ("pic_hue", "pic_brightness", "pic_contrast", "pic_saturation", "pic_gamma"):
                self.store.set(k, prefs.DEFAULTS[k])
                c_ = self.controls.get(k)
                if c_ is not None:
                    c_.set_value(prefs.DEFAULTS[k])
        elif key == "rescan":
            w.engine.extra_once = ["--reset-plugins-cache"]
            w.engine.rebuild(force=True)
            w.notify("Plugin cache rebuilt")

    def _addons(self):
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        dirs = sysutil.lua_extension_dirs()
        found = []
        for d in dirs:
            for f in sorted(d.glob("*.lua*")) if d.exists() else []:
                found.append(f)
        listing = QWidget()
        listing.setMinimumHeight(190)
        lv = QVBoxLayout(listing)
        lv.setContentsMargins(14, 12, 14, 12)
        for f in found:
            lv.addWidget(Label(f.stem + ("   (yours)" if str(f).startswith(str(Path.home())) else ""), 14, "text"))
        if not found:
            lv.addWidget(Label("No extensions installed.", 14, "text_3"))
        lv.addStretch(1)
        listing.setStyleSheet(f"background: {theme.hexc('field')}; border-radius: 6px;")
        v.addWidget(listing)
        row = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText(tr("Search"))
        search.setFixedSize(206, 32)
        row.addWidget(search)
        row.addStretch(1)
        more = Button("Find more extensions", "ghost", height=32)
        more.clicked.connect(lambda: __import__("webbrowser").open("https://addons.videolan.org/browse?cat=lua+extensions"))
        add = Button("Add", "ghost", height=32)
        add.clicked.connect(self._add_extension)
        rem = Button("Remove", "ghost", height=32)
        rem.clicked.connect(lambda: sysutil.open_path(dirs[0]) if dirs[0].exists() else None)
        for b in (more, add, rem):
            row.addWidget(b)
        v.addLayout(row)
        return box

    def _add_extension(self):
        path, _ = FileDialog.getOpenFileName(self, "Add a VLC extension", os.path.expanduser("~/Downloads"),
                                              "VLC Lua extensions (*.lua *.luac)")
        if path:
            d = sysutil.lua_extension_dirs()[0]
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, d / os.path.basename(path))
            self.win.notify("Extension added for VLC media player")
            self.show_category("extensions")

    # --- painting / dragging ------------------------------------------------------
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m = self.MARGIN
        r = QRectF(self.rect()).adjusted(m, m, -m, -m)
        paint_shadow(p, r, 14, spread=16, alpha=0.46, dy=8)
        p.setPen(Qt.NoPen)
        p.setBrush(c("base", 0.985))
        p.drawRoundedRect(r, 14, 14)
        from PyQt5.QtGui import QPainterPath
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        p.setClipPath(path)
        p.fillRect(QRectF(r.left(), r.top(), r.width(), 50), c("panel_head"))
        p.setClipping(False)
        # The rail is the only card in the design: the page beside it is the dialog's own
        # surface, not a lighter panel — painting one there left the card invisible.
        # Where the card stands is asked of the card itself: its own geometry is in the scroll
        # area's coordinates, and translating that by the rail's position alone drew it 8 px up
        # and to the left of the items it holds (and never followed the rail as it scrolled).
        p.setBrush(c("rail", 0.95))
        corner = self.navcard.mapTo(self, QPoint(0, 0))
        p.drawRoundedRect(QRectF(corner.x(), corner.y(), self.navcard.width(), self.navcard.height()), 8, 8)

    def showEvent(self, e):
        # Built at the size the design calls for; on a screen that cannot hold it (a 1280x720
        # laptop against this 913 px card) the footer and the last rows were off-screen.
        clamp_to_screen(self)
        par = self.parentWidget()
        if par is not None:
            g = par.window().frameGeometry()
            self.move(g.center() - self.rect().center())
        clamp_position(self)
        # the rail is laid out by now, so its card's width is known — the search field needs it
        self._align_search()
        super().showEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.pos().y() < self.MARGIN + 50:
            self._drag = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPos() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.accept()
        else:
            super().keyPressEvent(e)
