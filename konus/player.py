"""The player area: video surface (or the music view), the control bar, and the
overlay bars used in fullscreen / hide-UI / always-auto-hide modes.

Gotcha: libVLC draws into a native X window, and widgets beside it become native too;
native children can't be translucent. Overlays are therefore separate translucent tool
windows with their own bar instances.
"""

from PyQt5.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QFontMetrics, QImage, QPainter, QPainterPath, QPixmap, QRegion
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from . import diag, icons, theme
from .controls import ControlBar, TitleBar
from .i18n import tr
from .theme import c


class Surface(QFrame):
    """Mouse handling shared by the video and the music view."""
    clicked = pyqtSignal()
    doubleClicked = pyqtSignal()
    middleClicked = pyqtSignal()
    wheeled = pyqtSignal(int, int)        # dx, dy steps
    moved = pyqtSignal(QPoint)            # global position
    contextMenu = pyqtSignal(QPoint)

    NOTCH = 120                           # one wheel notch, in eighths of a degree

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self._press = None
        self._acc = [0.0, 0.0]            # horizontal and vertical leftovers, see wheelEvent

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPos()
        elif e.button() == Qt.RightButton:
            self.contextMenu.emit(e.globalPos())
        elif e.button() == Qt.MiddleButton:
            self.middleClicked.emit()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._press is not None:
            if (e.globalPos() - self._press).manhattanLength() < 5:
                # Straight away, not after the double-click interval: "click to play/pause" has
                # to feel immediate. A double click arrives as a separate event (see
                # mouseDoubleClickEvent) and takes this one back.
                self.clicked.emit()
            self._press = None

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.doubleClicked.emit()    # the press that opened it never set self._press,
            self._press = None           # so its release will not fire another clicked
        else:
            self.mousePressEvent(e)     # a quick second middle/right click is still a click

    def mouseMoveEvent(self, e):
        self.moved.emit(e.globalPos())
        if self._press is not None and (e.globalPos() - self._press).manhattanLength() > 8:
            self._press = None
            # Dragging moves the window only where there's no title bar to drag instead
            # (picture in picture, hidden interface). Otherwise it does nothing, like VLC.
            w = self.window()
            if hasattr(w, "start_move") and not w.isFullScreen() and getattr(w, "video_drags_window", lambda: True)():
                w.start_move()

    def wheelEvent(self, e):
        # Qt's horizontal delta is positive towards the left; flip it so scrolling right is +1.
        d = e.angleDelta()
        px = e.pixelDelta()
        dx, dy = -d.x(), d.y()
        if dx == 0 and dy == 0 and not px.isNull():
            # touchpads: the movement arrives as pixels, with an empty angle delta
            dx, dy = -px.x(), px.y()
        sx, sy = self._steps(0, dx), self._steps(1, dy)
        diag.log("wheel", ax=d.x(), ay=d.y(), px=px.x(), py=px.y(), phase=int(e.phase()),
                 mods=int(e.modifiers()), inverted=e.inverted(), dx=dx, dy=dy,
                 steps=f"{sx},{sy}", left=f"{self._acc[0]:.0f},{self._acc[1]:.0f}")
        if sx or sy:
            self.wheeled.emit(sx, sy)

    def _steps(self, axis, delta):
        """Whole wheel notches for a delta, carrying the rest over to the next event.

        A plain wheel sends 120 per notch and this is one step, as before. Touchpads and
        high-resolution wheels send a stream of small deltas (8, 16, ...): treating every
        one of them as a full notch made the picture jump seconds ahead per event."""
        if not delta:
            return 0
        acc = self._acc[axis]
        if acc and (acc > 0) != (delta > 0):
            acc = 0                        # a change of direction takes effect at once
        acc += delta
        whole = int(acc / self.NOTCH)
        self._acc[axis] = acc - whole * self.NOTCH
        return whole


class VideoFrame(Surface):
    """Native child window that libVLC draws into (rounded with an X shape mask).

    libVLC's OpenGL output renders straight into this window, so Qt must never paint it:
    any Qt repaint (focus change, resize, palette change) would flash over the video.
    While nothing plays, PlayerArea shows a plain black page instead.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_PaintOnScreen)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self.radius = 9

    def paintEngine(self):
        return None

    def paintEvent(self, e):
        # the video output draws into a native child window of its own; the area it has not
        # covered yet (it is resized a moment after us) used to be the system's white, which
        # showed as pale strips along the edges during a window resize
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._mask()

    def set_radius(self, r):
        self.radius = r
        self._mask()

    def _mask(self):
        if self.radius <= 0:
            self.clearMask()
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self.radius, self.radius)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))


class MusicView(Surface):
    """Audio: a large cover over a blurred copy of it, "Title - Artist" underneath.
    With the Waveform visualizer it draws the track's waveform scrolling past instead."""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.cover = None
        self.blur = None
        self.text = ""
        self.peaks = None
        self.pos = 0.0
        self.waveform = False

    def set_media(self, cover_path, text):
        self.text = text
        pm = QPixmap(cover_path) if cover_path else QPixmap()
        self.cover = pm if not pm.isNull() else None
        self.blur = _blurred(self.cover) if self.cover else None
        self.update()

    def set_wave(self, peaks, pos, on):
        self.peaks, self.pos, self.waveform = peaks, pos, on
        if on:
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#000000"))
        if self.blur is not None:
            s = self.blur.size().scaled(self.size(), Qt.KeepAspectRatioByExpanding)
            r = QRect(0, 0, s.width(), s.height())
            r.moveCenter(self.rect().center())
            p.setOpacity(0.55)
            p.drawPixmap(r, self.blur)
            p.setOpacity(1)
            p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        h = self.height()
        side = int(min(self.width() - 80, h - 200, 738))
        text_y = None
        if self.waveform and self.peaks:
            self._paint_wave(p)
            text_y = int(h * 0.78)
        elif side > 60:
            box = QRectF((self.width() - side) / 2, max(30, (h - side - 90) / 2), side, side)
            path = QPainterPath()
            path.addRoundedRect(box, 16, 16)
            if self.cover is not None and self.win.store.get("cover_art"):
                p.setClipPath(path)
                pm = self.cover.scaled(int(side * self.devicePixelRatioF()), int(side * self.devicePixelRatioF()),
                                       Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                pm.setDevicePixelRatio(self.devicePixelRatioF())
                p.drawPixmap(box.topLeft(), pm)
                p.setClipping(False)
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#000000"))
                p.drawPath(path)
                p.drawPixmap(QRectF(box.center().x() - 48, box.center().y() - 48, 96, 96).toRect(),
                             icons.pixmap("music", 96, "#3a3a3a"))
            text_y = int(box.bottom() + 70)
        if text_y is not None:
            f = theme.font(24)
            p.setFont(f)
            p.setPen(QColor("#969696"))
            p.drawText(QRectF(20, text_y - 18, self.width() - 40, 36), Qt.AlignCenter,
                       QFontMetrics(f).elidedText(self.text, Qt.ElideRight, self.width() - 40))

    def _paint_wave(self, p):
        n = len(self.peaks)
        w, h = self.width(), self.height()
        mid = h * 0.45
        span = 160                       # bins visible
        center = self.pos * n
        step = w / span
        for i in range(span):
            idx = int(center - span / 2 + i)
            if not 0 <= idx < n:
                continue
            v = self.peaks[idx]
            bh = max(3.0, v * h * 0.5)
            x = i * step
            p.setPen(Qt.NoPen)
            p.setBrush(c("accent") if idx <= center else QColor("#3a3a3a"))
            p.drawRoundedRect(QRectF(x + step * 0.2, mid - bh / 2, step * 0.6, bh), 2, 2)


def _blurred(pm, radius=8):
    img = pm.toImage().scaled(48, 48, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    for _ in range(2):
        img = img.scaled(24, 24, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        img = img.scaled(64, 64, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return QPixmap.fromImage(img.scaled(640, 640, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))


class StatsBox(QWidget):
    """Show Statistics: a translucent box over the top-left of the video."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus
                         | Qt.NoDropShadowWindowHint | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.rows = {}
        self.resize(300, 300)

    def set_rows(self, rows):
        self.rows = {tr(k): tr(v) for k, v in rows.items()}
        fm = QFontMetrics(theme.font(12))
        # The label column is as wide as the longest label: translations run longer than the
        # English, and at a fixed 150 px the values sat on top of the labels.
        self.lab_w = max([fm.horizontalAdvance(k) for k in self.rows] or [0]) + 14
        val_w = max([fm.horizontalAdvance(v) for v in self.rows.values()] or [0]) + 14
        self.resize(min(620, max(300, self.lab_w + val_w + 24)),
                    24 + len(self.rows) * (fm.height() + 4))
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        f = theme.font(12)
        fm = QFontMetrics(f)
        p.setFont(f)
        lab_w = getattr(self, "lab_w", 150)
        y = 12
        for k, v in self.rows.items():
            p.setPen(QColor("#9a9a9a"))
            p.drawText(QRectF(12, y, lab_w, fm.height()), Qt.AlignLeft | Qt.AlignVCenter, k)
            p.setPen(QColor("#e6e6e6"))
            p.drawText(QRectF(12 + lab_w + 8, y, self.width() - lab_w - 32, fm.height()),
                       Qt.AlignRight | Qt.AlignVCenter, v)
            y += fm.height() + 4


class OverlayWindow(QWidget):
    """A translucent tool window holding a title bar or a control bar over the video."""

    def __init__(self, win, kind):
        super().__init__(win, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.win = win
        self.kind = kind
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.bar = TitleBar(win, overlay=True) if kind == "top" else ControlBar(win, overlay=True)
        lay.addWidget(self.bar)
        self.setMouseTracking(True)

    def paintEvent(self, e):
        p = QPainter(self)
        col = c("bar")
        col.setAlphaF(max(0.1, min(1.0, self.win.store.get("fs_opacity") / 100)))
        p.fillRect(self.rect(), col)
        if self.kind == "bottom":
            p.fillRect(QRect(0, 0, self.width(), 1), c("line"))


class BlankView(Surface):
    """Black, rounded area shown while nothing is playing."""

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), c("bar"))
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 9, 9)
        p.fillPath(path, QColor("#000000"))


class PlayerArea(QWidget):
    """Video (or music view) above the control bar."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setAttribute(Qt.WA_StyledBackground, False)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.frame = QWidget()
        self.frame_lay = QVBoxLayout(self.frame)
        self.frame_lay.setContentsMargins(10, 1, 10, 0)
        self.stack = QStackedWidget()
        self.video = VideoFrame()
        self.music = MusicView(win)
        self.blank = BlankView()
        self.stack.addWidget(self.video)
        self.stack.addWidget(self.music)
        self.stack.addWidget(self.blank)
        self.stack.setCurrentWidget(self.blank)
        self.frame_lay.addWidget(self.stack)
        v.addWidget(self.frame, 1)
        self.bar = ControlBar(win)
        v.addWidget(self.bar)

    def set_inset(self, inset, top=1):
        self.frame_lay.setContentsMargins(inset, top if inset else 0, inset, 0 if self.bar.isVisible() else inset)
        self.video.set_radius(9 if inset else 0)

    def video_rect_global(self):
        w = self.stack
        return QRect(w.mapToGlobal(QPoint(0, 0)), w.size())

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), c("bar"))
