"""First-run onboarding (1090×759): welcome + media folder, theme and accent colour,
basic keyboard shortcuts."""

import os

from PyQt5.QtCore import QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (QApplication, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLineEdit, QSizePolicy,
                             QSpacerItem, QStackedWidget, QVBoxLayout,
                             QWidget)

from . import icons, theme
from .i18n import tr
from .theme import ACCENTS, c
from .widgets import Label, clamp_position, clamp_to_screen, paint_shadow, scroll
from .filedialog import FileDialog


class SolidButton(QWidget):
    clicked = pyqtSignal()

    def __init__(self, text, kind="accent", width=100):
        super().__init__()
        self.text, self.kind = tr(text), kind
        self.setFixedSize(width, 32)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if self.kind == "accent":
            bg = c("accent").lighter(108) if self.underMouse() else c("accent")
            fg = QColor("#1b1b1b")
        else:
            bg = c("hover") if self.underMouse() else c("bar")
            fg = c("text")
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(self.rect()), 4, 4)
        p.setFont(theme.font(16, theme.SEMIBOLD))
        p.setPen(fg)
        p.drawText(self.rect(), Qt.AlignCenter, self.text)


class ThemeTile(QWidget):
    clicked = pyqtSignal(str)

    W, H = 124, 160          # the design size; the artwork is drawn from the live size

    def __init__(self, key, label):
        super().__init__()
        self.key, self.label = key, label
        self.selected = False
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        self.clicked.emit(self.key)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # the tile scales with the card: the mock window keeps its square, the label its line.
        # The box is centred — the selection ring is drawn 10 px outside it, and an offset box
        # pushed that ring past the widget's right edge (the orange outline came out cut off)
        side = min(self.width() - 24, self.height() - 60)
        box = QRectF((self.width() - side) / 2, 12, side, side)
        if self.selected:
            p.setPen(QPen(c("accent"), 4))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box.adjusted(-10, -10, 10, 10), 14, 14)
        p.setPen(Qt.NoPen)
        from PyQt5.QtGui import QPainterPath, QPolygonF
        from PyQt5.QtCore import QPointF
        path = QPainterPath()
        path.addRoundedRect(box, 10, 10)
        if self.key == "light":
            p.setBrush(QColor("#b4b4b4"))
            p.drawPath(path)
        elif self.key == "dark":
            p.setBrush(QColor("#232323"))
            p.drawPath(path)
        else:
            p.setBrush(QColor("#232323"))
            p.drawPath(path)
            p.setClipPath(path)
            p.setBrush(QColor("#b4b4b4"))
            p.drawPolygon(QPolygonF([QPointF(box.left() + 40, box.top()), QPointF(box.right(), box.top()),
                                     QPointF(box.right(), box.bottom()), QPointF(box.left() + 88, box.bottom())]))
            p.setClipping(False)
        p.setFont(theme.font(16))
        # the tile is a mock-up of the window over a dark backdrop: its label has to be light
        p.setPen(c("on_video"))
        p.drawText(QRectF(0, box.bottom() + 16, self.width(), 26), Qt.AlignCenter, self.label)


class Swatches(QWidget):
    picked = pyqtSignal(str)
    STEP = 80                       # the design step; a narrower card gets smaller circles

    def __init__(self, value):
        super().__init__()
        self.value = value
        self.step = self.STEP
        self.radius = 20
        self.setFixedSize(len(ACCENTS) * self.step, 64)
        self.setCursor(Qt.PointingHandCursor)

    def set_step(self, step):
        self.step = max(34, int(step))
        self.radius = max(12, round(self.step * 0.25))
        self.setFixedSize(len(ACCENTS) * self.step, max(40, round(self.step * 0.8)))

    def mouseReleaseEvent(self, e):
        i = int(e.x() // max(1, self.step))
        if 0 <= i < len(ACCENTS):
            self.value = ACCENTS[i]
            self.update()
            self.picked.emit(self.value)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cy = self.height() / 2
        r = self.radius
        for i, col in enumerate(ACCENTS):
            cx = i * self.step + self.step / 2
            sel = col.lower() == self.value.lower()
            q = QColor(col)
            if not sel:
                q = theme.mix(col, "#0e0e0e", 0.4)
            else:
                ring = QColor(col)
                ring.setAlphaF(0.35)
                p.setPen(QPen(ring, 4))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QRectF(cx - r * 1.5, cy - r * 1.5, r * 3, r * 3))
            p.setPen(Qt.NoPen)
            p.setBrush(q)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))


class KeyCap(QWidget):
    H = 50                   # the design height; a smaller card gets a flatter cap

    def __init__(self, text):
        super().__init__()
        self.text = text
        self.size = 16
        self.set_height(self.H)

    def set_height(self, h, size=None):
        self.size = int(size or self.size)
        self.setFixedSize(max(44, QFontMetrics(theme.font(self.size)).horizontalAdvance(self.text) + 44),
                          max(28, int(h)))
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 8)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#151515"))
        p.drawRoundedRect(r.translated(0, 5), 8, 8)
        p.setPen(QPen(QColor("#3a3a3a"), 2))
        p.setBrush(QColor("#232323"))
        p.drawRoundedRect(r, 8, 8)
        p.setFont(theme.font(self.size))
        p.setPen(QColor("#cfcfcf"))
        p.drawText(r, Qt.AlignCenter, self.text)


class PreviewBars(QWidget):
    """Step 2's backdrop: a miniature player (title bar + control bar) in the chosen colours."""

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), c("video") if not theme.T.is_light() else QColor("#0a0a0a"))
        p.fillRect(QRectF(0, 0, w, 22), c("bar"))
        p.drawPixmap(8, 3, icons.pixmap("logo", 16))
        for i, name in enumerate(("x", "fullscreen", "maximize", "minimize")):
            p.drawPixmap(w - 24 - i * 20, 3, icons.pixmap(name, 16))
        y = h - 30
        p.fillRect(QRectF(0, y, w, 30), c("bar"))
        p.drawPixmap(10, y + 7, icons.pixmap("previous", 16))
        p.setPen(Qt.NoPen)
        p.setBrush(c("accent"))
        p.drawRoundedRect(QRectF(32, y + 7, 30, 16), 3, 3)
        p.drawPixmap(39, y + 7, icons.pixmap("play", 16, "#1b1b1b"))
        p.drawPixmap(68, y + 7, icons.pixmap("next", 16))
        p.setFont(theme.font(9))
        p.setPen(c("icon"))
        p.drawText(QRectF(92, y, 50, 30), Qt.AlignVCenter, "00:02:10")
        p.setBrush(c("field"))
        p.drawRoundedRect(QRectF(140, y + 7, w - 360, 16), 3, 3)
        p.setBrush(c("accent_bg"))
        p.drawRoundedRect(QRectF(141, y + 8, 50, 14), 2, 2)
        p.setBrush(c("accent"))
        p.drawRoundedRect(QRectF(187, y + 8, 4, 14), 2, 2)
        p.setPen(c("icon"))
        p.drawText(QRectF(w - 214, y, 50, 30), Qt.AlignVCenter, "00:25:40")
        p.drawPixmap(w - 160, y + 7, icons.pixmap("vol-high", 16))
        p.setBrush(QColor("#95d318"))
        p.drawRect(QRectF(w - 140, y + 12, 20, 6))
        for i, name in enumerate(("lists", "playback settings", "subtitles", "audio")):
            p.drawPixmap(w - 24 - i * 22, y + 7, icons.pixmap(name, 16))


class Onboarding(QDialog):
    MARGIN = 24

    def __init__(self, win):
        super().__init__(win, Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.win = win
        self.store = win.store
        m = self.MARGIN
        self.resize(1090 + 2 * m, 759 + 2 * m)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(m, m, m, m)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self._gaps = []              # (spacer, its design height) — scaled to the room
        self._scalables = []         # callables that resize what is not a gap
        for page in (self._welcome(), self._theme(), self._shortcuts()):
            self.stack.addWidget(page)
        self._drag = None

    def _gap(self, lay, height):
        """A vertical gap that shrinks with the card (see _fit_pages)."""
        item = QSpacerItem(0, height, QSizePolicy.Minimum, QSizePolicy.Fixed)
        lay.addSpacerItem(item)
        self._gaps.append((item, height))
        return item

    def _apply_scale(self, s):
        for item, height in self._gaps:
            item.changeSize(0, max(6, round(height * s)))
        for resize in self._scalables:
            resize(s)
        for page in self._pages():
            page.updateGeometry()

    def _pages(self):
        return [self.stack.widget(i) for i in range(self.stack.count())]

    def _fit_pages(self, room=None):
        """The pages are designed for the 856 px card. On a smaller screen every fixed gap, the
        key caps and the theme tiles scale with the room there is, so the wizard keeps its shape,
        its buttons stay on screen and nothing needs a scrollbar.

        Two passes, because a page keeps some of its height fixed (a title line, the footer
        buttons): the first guess is made from the room, the second one from what the pages
        still ask for — which is also what has to be small enough for the card to shrink."""
        design = 856 - 2 * self.MARGIN
        room = room or self.stack.height() or design
        scale = max(0.5, min(1.0, room / design))
        self._fit_scale = scale
        self._apply_scale(scale)
        for _ in range(4):
            need = max([p.minimumSizeHint().height() for p in self._pages()] or [0])
            if need <= room or scale <= 0.5:
                break
            scale = max(0.5, scale * (room / need) * 0.98)
            self._fit_scale = scale
            self._apply_scale(scale)
        self.stack.widget(self.stack.currentIndex()).update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_pages()

    def _buttons(self, back=True, next_text="Next"):
        """The Back + Next/Finish group as one fixed-width box.

        A group that is free to take a few spare pixels puts the same Back button six pixels
        to the left on the page with the longer footer. Fixed width, it is the same rectangle
        on every page; the callers align the box right."""
        box = QWidget()
        box.setFixedSize(208 if back else 100, 32)
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        if back:
            b = SolidButton("Back", "dark")
            b.clicked.connect(lambda: self.stack.setCurrentIndex(self.stack.currentIndex() - 1))
            row.addWidget(b)
        n = SolidButton(next_text)
        n.clicked.connect(self._next)
        row.addWidget(n)
        return box

    def _welcome(self):
        w = QWidget()
        self._welcome_page = w
        v = QVBoxLayout(w)
        self._welcome_lay = v
        v.setContentsMargins(248, 110, 50, 56)
        v.setSpacing(0)
        logo = Label("")
        logo.setPixmap(icons.pixmap("logo", 128))
        logo.setAlignment(Qt.AlignCenter)
        logo.setContentsMargins(0, 0, 198, 0)
        self._welcome_logo = logo
        v.addWidget(logo)
        self._gap(v, 40)
        t = Label("Welcome to Konus!", 32, "text_hi", weight=300)
        t.setAlignment(Qt.AlignCenter)
        t.setContentsMargins(0, 0, 198, 0)
        self._welcome_title = t
        v.addWidget(t)
        self._gap(v, 40)
        body = Label("Konus plays everything through libVLC, the engine VLC media player uses, so it opens "
                     "what your VLC opens. An independent player: not affiliated with VideoLAN, and it carries no "
                     "VLC name, cone or logo.", 16, "text",
                     wrap=True)
        body.setFixedWidth(594)
        self._welcome_body = body
        v.addWidget(body)
        self._gap(v, 50)
        v.addWidget(Label("Choose a Media Folder to get started with", 14, "icon"))
        self._gap(v, 14)
        row = QHBoxLayout()
        row.setSpacing(10)
        default = os.path.join(os.path.expanduser("~"), "Videos")
        if self.store["video_folders"]:
            default = self.store["video_folders"][0]
        self.folder = QLineEdit(default)
        self.folder.setFixedSize(470, 36)
        theme.styled(self.folder, lambda: f"QLineEdit {{ background: {theme.hexc('field')}; "
                                          f"border: 1px solid {theme.hexc('line')}; border-radius: 6px; "
                                          f"padding: 0 12px; font-size: 16px; "
                                          f"color: {theme.hexc('text_hi')}; }}")
        row.addWidget(self.folder)
        browse = SolidButton("Browse", "dark", width=114)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        v.addWidget(self._buttons(back=False), 0, Qt.AlignRight)
        return w

    def _browse(self):
        d = FileDialog.getExistingDirectory(self, "Choose a media folder", os.path.expanduser(self.folder.text()),
                                             options=self.win.dialog_options())
        if d:
            self.folder.setText(d)

    def _theme(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 16, 16, 0)
        v.setSpacing(0)
        self.preview = PreviewBars()
        self.preview.setFixedHeight(594)
        pv = QVBoxLayout(self.preview)
        pv.setContentsMargins(0, 160, 0, 0)
        self._theme_gap = QSpacerItem(0, 70, QSizePolicy.Minimum, QSizePolicy.Fixed)
        self._scalables.append(self._fit_theme)
        t = Label("Select your preferred theme and color.", 24, "text", weight=300)
        t.set_color("on_video")
        t.setAlignment(Qt.AlignCenter)
        pv.addWidget(t)
        pv.addSpacerItem(self._theme_gap)
        tiles = QHBoxLayout()
        tiles.addStretch(1)
        self.tiles = {}
        for key, label in (("light", "Light"), ("dark", "Dark"), ("system", "System")):
            tile = ThemeTile(key, tr(label))
            tile.clicked.connect(self._pick_theme)
            tiles.addWidget(tile)
            tiles.addSpacing(32)
            self.tiles[key] = tile
        tiles.addStretch(1)
        pv.addLayout(tiles)
        pv.addStretch(1)
        v.addWidget(self.preview)
        self._theme_bottom = QHBoxLayout()
        bottom = self._theme_bottom
        bottom.setContentsMargins(36, 34, 34, 56)
        self._scalables.append(lambda s, b=bottom: b.setContentsMargins(
            36, max(12, round(34 * s)), 34, max(20, round(56 * s))))
        self.swatches = Swatches(self.store.get("accent"))
        self.swatches.picked.connect(self._pick_accent)
        bottom.addWidget(self.swatches)
        bottom.addStretch(1)
        buttons = self._buttons()
        bottom.addWidget(buttons)
        # The swatches are 64 px tall and the buttons 32: centred in the row, Next sat 16 px
        # above Finish on the next page. Bottom-aligned, both are in the same place, so the
        # click that leaves one page is over the button that leaves the next.
        bottom.setAlignment(buttons, Qt.AlignRight | Qt.AlignBottom)
        v.addLayout(bottom)
        self._pick_theme(self.store.get("theme"), apply=False)
        return w

    def _pick_theme(self, key, apply=True):
        for k, t in self.tiles.items():
            t.selected = k == key
            t.update()
        if apply:
            self.store.set("theme", key)
            self.win.apply_theme()
            self.update()

    def _pick_accent(self, col):
        self.store.set("accent", col)
        self.win.apply_theme()
        self.update()
        for t in self.tiles.values():
            t.update()
        self.preview.update()

    def _shortcuts(self):
        w = QWidget()
        self._shortcut_page = w
        v = QVBoxLayout(w)
        v.setContentsMargins(62, 70, 50, 56)
        self._scalables.append(lambda s, v=v: v.setContentsMargins(
            62, max(24, round(70 * s)), 50, max(20, round(56 * s))))
        t = Label("Learn the basic keyboard shortcuts.", 24, "text", weight=300)
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        self._gap(v, 50)
        self._shortcut_grid = QGridLayout()
        grid = self._shortcut_grid
        grid.setVerticalSpacing(30)
        grid.setHorizontalSpacing(0)
        self._caps = []
        keys = self.win.hotkeys()
        left = [("Play / Pause", [keys.get("play_pause", "Space")]),
                ("Seek Backward / Forward", ["←", "→"]),
                ("Previous / Next Item", ["<", ">"]),
                ("Previous / Next Frame", [",", "."]),
                ("Cycle Subtitle Track", [keys.get("sub_track", "V")]),
                ("Cycle Audio Track", [keys.get("audio_track", "B")])]
        right = [("Toggle Fullscreen", [keys.get("fullscreen", "F")]),
                 ("Increase / Decrease Volume", ["↑", "↓"]),
                 ("Show / Hide Queue", [keys.get("queue", "Q")]),
                 ("Show / Hide File Browser", [keys.get("library", "L")]),
                 ("Take a Snapshot", keys.get("snapshot", "Shift+S").upper().split("+")),
                 ("Record", keys.get("record", "Shift+R").upper().split("+"))]
        self._shortcut_labels = []
        for col, rows in ((0, left), (2, right)):
            for r, (label, caps) in enumerate(rows):
                lab = Label(label, 16, "text")
                self._shortcut_labels.append(lab)
                grid.addWidget(lab, r, col)
                h = QHBoxLayout()
                h.setSpacing(8)
                for i, cap in enumerate(caps):
                    if i and len(caps) == 2 and cap not in ("S", "R"):
                        h.addWidget(Label("/", 16, "text_2"))
                    cap_widget = KeyCap(cap)
                    self._caps.append(cap_widget)
                    h.addWidget(cap_widget)
                h.addStretch(1)
                cw = QWidget()
                cw.setLayout(h)
                grid.addWidget(cw, r, col + 1)
        grid.setColumnMinimumWidth(0, 240)
        grid.setColumnMinimumWidth(2, 240)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        v.addLayout(grid)
        v.addStretch(1)
        bottom = QHBoxLayout()
        bottom.addWidget(Label("Find the full list in the menu.", 16, "text"))
        bottom.addStretch(1)
        buttons = self._buttons(next_text="Finish")
        bottom.addWidget(buttons)
        # The group is placed right as a whole, so the Back button sits in the same place here
        # as it does on the theme page.
        bottom.setAlignment(buttons, Qt.AlignRight | Qt.AlignBottom)
        v.addLayout(bottom)
        return w

    def _fit_theme(self, s):
        # the welcome page: its 248 px left inset (and the 198 px right insets of the logo and
        # the title, which cancel it out) only work on the wide stack — on a narrow card the
        # title ran off the left edge and the body text past the right one
        avail = self.stack.width() or 1090
        if s >= 0.85:
            self._welcome_lay.setContentsMargins(248, 110, 50, 56)
            pad = 198
        else:
            pad = 0
            side = max(24, (avail - 600) // 2)
            self._welcome_lay.setContentsMargins(side, max(28, round(110 * s)), side, max(20, round(56 * s)))
        self._welcome_logo.setContentsMargins(0, 0, pad, 0)
        self._welcome_title.setContentsMargins(0, 0, pad, 0)
        self._welcome_body.setFixedWidth(min(594, max(280, avail - 2 * (pad and 248 or max(24, (avail - 600) // 2)))))

        self.preview.setFixedHeight(max(320, round(594 * s)))
        pv = self.preview.layout()
        pv.setContentsMargins(0, max(50, round(160 * s)), 0, 0)
        self._theme_gap.changeSize(0, max(16, round(70 * s)))
        for tile in self.tiles.values():
            tile.setFixedSize(max(84, round(tile.W * s)), max(104, round(tile.H * s)))

        # the swatch row sits on the same line as the buttons: it has to give way with them
        room = self.stack.width() - 250
        self.swatches.set_step(min(Swatches.STEP, room / max(1, len(ACCENTS))))

        # the labels and the caps lose a size or two with the card: at 240 px columns a long
        # label like "Seek Backward / Forward" was cut mid-word on a narrow screen
        size = 16 if s >= 0.85 else (15 if s >= 0.72 else 14)
        for lab in self._shortcut_labels:
            # on a card this narrow a long label cannot fit one line at all: let it wrap
            lab.setWordWrap(s < 0.8)
            lab.setFont(theme.font(size))
        for cap in self._caps:
            cap.set_height(KeyCap.H * s, size)
        self._shortcut_grid.setVerticalSpacing(max(12, round(30 * s)))
        col = max(124, min(240, round(216 * (self.stack.width() / 1090))))
        self._shortcut_grid.setColumnMinimumWidth(0, col)
        self._shortcut_grid.setColumnMinimumWidth(2, col)

    def _next(self):
        i = self.stack.currentIndex()
        if i == 0:
            folder = os.path.expanduser(self.folder.text().strip())
            if folder and os.path.isdir(folder):
                music = os.path.basename(folder.rstrip("/")).lower() in ("music", "songs", "audio")
                self.store.add_folder("music" if music else "video", folder)
        if i < self.stack.count() - 1:
            self.stack.setCurrentIndex(i + 1)
        else:
            self.store["onboarded"] = True
            self.accept()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m = self.MARGIN
        r = QRectF(self.rect()).adjusted(m, m, -m, -m)
        paint_shadow(p, r, 14, spread=16, alpha=0.5, dy=8)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#0e0e0e") if not theme.T.is_light() else c("base"))
        p.drawRoundedRect(r, 14, 14)

    def showEvent(self, e):
        # Built at the size the design calls for; on a screen that cannot hold it (a 1280x720
        # laptop against this 913 px card) the footer and the last rows were off-screen.
        clamp_to_screen(self)
        par = self.parentWidget()
        if par is not None:
            g = par.window().frameGeometry()
            self.move(g.center() - self.rect().center())
        clamp_position(self)
        super().showEvent(e)
        # the pages first, so a small screen can really shrink the card (a page's own minimum
        # would otherwise keep the card at its design height and push its footer off the screen)
        screen = QApplication.primaryScreen()
        if screen is not None:
            room = screen.availableGeometry().height() - 2 * self.MARGIN - 20
            self._fit_pages(room)
            clamp_to_screen(self)
        self._fit_pages()
        clamp_position(self)

    def mousePressEvent(self, e):
        self._drag = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.store["onboarded"] = True
            self.reject()
        else:
            super().keyPressEvent(e)
