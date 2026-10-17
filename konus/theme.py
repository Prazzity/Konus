"""Colours, fonts and the application stylesheet.

Values come from Juxtopposed's Figma file. Everything that paints itself reads the
current palette through `c(name)`, so switching theme or accent colour only needs a
repaint (see `apply()`).
"""

from pathlib import Path
from string import Template

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette

ASSETS = Path(__file__).with_name("assets")

# The nine accent swatches from the onboarding / General settings screens.
ACCENTS = ["#d37918", "#e15032", "#c49e23", "#19a234", "#18aad3", "#185dd3", "#8e42ff", "#f052b3", "#afafaf"]

DARK = {
    "bar": "#1b1b1b",            # title bar, control bar, frame around the video
    "bar_alpha": 0.95,
    "rail": "#191919",           # icon rail of the library sidebar, nav card
    "base": "#0e0e0e",           # library background, panels, dialogs
    "panel_head": "#090909",     # tab strip of panels, dialog header
    "field": "#090909",          # inputs, seek track, toggles
    "field_2": "#151515",        # search boxes
    "item": "#191919",           # dropdown face, toggle track
    "hover": "#1f1f1f",          # hovered / selected menu option
    "hover_2": "#222222",        # selected settings category
    "line": "#222222",           # separators
    "video": "#000000",
    "card": "#171717",           # thumbnail placeholder
    "thumb": "#303030",
    "knob_off": "#404040",
    "text": "#969696",           # normal text
    "text_2": "#6b6b6b",         # secondary text
    "text_3": "#5c5c5c",         # placeholders
    "text_hi": "#c6c6c6",        # selected nav item
    "text_max": "#eaeaea",
    "on_video": "#e6e6e6",      # text over a video frame (the same in both themes: the picture is dark)
    "icon": "#8f8f8f",
    "shadow": "#000000",
    "dim": 0.45,                 # opacity of watched items
}

LIGHT = {
    "bar": "#d5d5d5",
    "bar_alpha": 0.95,
    "rail": "#cfcfcf",
    "base": "#e7e7e7",
    "panel_head": "#d0d0d0",
    "field": "#dedede",
    "field_2": "#d9d9d9",
    "item": "#cdcdcd",
    "hover": "#c9c9c9",
    "hover_2": "#c4c4c4",
    "line": "#c4c4c4",
    "video": "#000000",
    "card": "#d0d0d0",
    "thumb": "#bdbdbd",
    "knob_off": "#a8a8a8",
    "text": "#2e2e2e",
    "text_2": "#6b6b6b",
    "text_3": "#8a8a8a",
    "text_hi": "#111111",
    "text_max": "#000000",
    "on_video": "#e6e6e6",
    "icon": "#555555",
    "shadow": "#000000",
    "dim": 0.5,
}


class _Theme(QObject):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.mode = "dark"           # dark | light | system
        self.accent = ACCENTS[0]
        self.p = dict(DARK)
        self._system_light = None        # cached "is the system theme light?" (see is_light)

    def is_light(self):
        if self.mode == "system":
            app = QGuiApplication.instance()
            if app is None:
                return False
            if self._system_light is None:
                # asked once: apply() below writes our own colours into the application palette,
                # so a later reading would be reading this app's theme instead of the system's
                self._system_light = app.palette().color(QPalette.Window).lightness() > 128
            return self._system_light
        return self.mode == "light"


T = _Theme()


def c(name, alpha=None):
    """Colour token from the current palette (or accent shades)."""
    if name == "accent":
        col = QColor(T.accent)
    elif name == "accent_2":          # darker accent: two-tone icons, ON toggle knob, slider thumb
        col = mix(T.accent, "#000000", 0.19)
    elif name == "accent_bg":         # played part of seek bars, ON toggle track
        col = mix(T.accent, T.p["field"], 0.80 if not T.is_light() else 0.62)
    else:
        col = QColor(T.p[name])
    if alpha is not None:
        col.setAlphaF(alpha)
    return col


def hexc(name):
    return c(name).name()


def mix(a, b, t):
    """Blend colour a towards b by t (0..1)."""
    a, b = QColor(a), QColor(b)
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def luminance(col):
    """Relative luminance (WCAG 2.x), 0 (black) .. 1 (white)."""
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    col = QColor(col)
    return 0.2126 * channel(col.red()) + 0.7152 * channel(col.green()) + 0.0722 * channel(col.blue())


def contrast(a, b):
    """Contrast ratio between two colours, 1:1 .. 21:1 (WCAG AA for body text is 4.5)."""
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def readable(col, bg, target=4.5):
    """`col` moved away from `bg` until it can be read on it: darkened on a light background,
    lightened on a dark one. Darkening is the wrong direction on a dark surface — the accent on
    the dark theme's near-black accent_bg fell to 1.1:1 when it was made darker."""
    if contrast(col, bg) >= target:
        return QColor(col)
    # the direction is decided by which end reads better on this background, not by how light
    # the background looks: a mid-tone face (a blue accent tinted over a light field) still
    # wants the darker label
    dark_way, light_way = mix(col, "#000000", 0.9), mix(col, "#ffffff", 0.9)
    towards = "#000000" if contrast(dark_way, bg) >= contrast(light_way, bg) else "#ffffff"
    for step in range(1, 10):
        moved = mix(col, towards, step / 10)
        if contrast(moved, bg) >= target:
            return moved
    return mix(col, towards, 0.9)


def accent_text(bg="base"):
    """The accent used as *text* on `bg`. Fine as it is in the dark theme; the light theme's
    pale surfaces left it at 1.4–2.9:1 (the Save button's label, the playing row in a list,
    a selected segment), so there it is darkened until the text reads."""
    return readable(T.accent, c(bg))


def selection_text():
    """White or black, whichever reads on the accent: white on the yellow or grey accent is
    2.2:1, black on it is 9:1."""
    return "#ffffff" if contrast("#ffffff", T.accent) >= contrast("#000000", T.accent) else "#000000"


def apply(mode=None, accent=None):
    if mode is not None:
        T.mode = mode
    if accent is not None:
        T.accent = accent
    T.p = dict(LIGHT if T.is_light() else DARK)
    app = QGuiApplication.instance()
    if app is not None and hasattr(app, "setStyleSheet"):
        app.setStyleSheet(stylesheet())
        # The palette a widget falls back on when it has not painted itself yet. Its defaults are
        # the system's (white Base/Window), so a strip the app had not repainted — a scroll area's
        # viewport caught mid-resize, for instance — flashed pale along the window's edge.
        pal = app.palette()
        for role, token in ((QPalette.Window, "base"), (QPalette.Base, "base"),
                            (QPalette.Button, "item"), (QPalette.Text, "text"),
                            (QPalette.WindowText, "text"), (QPalette.ButtonText, "text"),
                            (QPalette.ToolTipBase, "item"), (QPalette.ToolTipText, "text"),
                            (QPalette.Highlight, "accent")):
            pal.setColor(role, c(token))
        pal.setColor(QPalette.HighlightedText, QColor(selection_text()))
        app.setPalette(pal)
    T.changed.emit()


def styled(widget, css):
    """Give a widget a stylesheet of its own, written from the theme as it is then — and written
    again for every theme change.

    A rule written once with hexc() in it keeps the colours of the theme it was written in, and
    nothing in the app re-writes it: the search box of the Settings dialog stayed dark under the
    light theme it had just been switched to. `css` is called for the rule, so it reads the theme
    each time. The keeper is a child of the widget it serves, so a dialog that is opened and closed
    all day leaves nothing behind. Widgets that paint themselves (see konus.widgets) need none of
    this — only stock ones, whose look is a stylesheet, do."""
    class _Keeper(QObject):
        def apply(self):
            try:
                widget.setStyleSheet(css())
            except RuntimeError:               # gone with its dialog between two changes
                pass

    keeper = _Keeper(widget)                   # a child of the widget: it lives exactly as long
    T.changed.connect(keeper.apply)
    keeper.apply()


# --- fonts ----------------------------------------------------------------
_family = None


def load_fonts():
    global _family
    for name in ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf"):
        QFontDatabase.addApplicationFont(str(ASSETS / "fonts" / name))
    families = set(QFontDatabase().families())
    for fam in ("Inter", "Segoe UI", "Arial"):
        if fam in families:
            _family = fam
            break
    return _family


def family():
    return _family or QFont().defaultFamily()


def font(size=14, weight=QFont.Normal):
    f = QFont(family())
    f.setPixelSize(size)
    f.setWeight(weight)
    f.setHintingPreference(QFont.PreferNoHinting)
    return f


MEDIUM = QFont.Medium
SEMIBOLD = QFont.DemiBold


# --- stylesheet (for the few stock widgets left: scroll bars, text fields, tooltips) ---
_QSS = Template("""
* { font-family: "$FONT"; color: $TEXT; }
QLineEdit, QPlainTextEdit, QTextEdit, QMenu, QToolTip, QComboBox, QPushButton { font-size: 14px; }
QInputDialog { background: $BASE; }
QWidget { background: transparent; }
QToolTip { background: $ITEM; color: $TEXT; border: 1px solid $LINE; padding: 5px 8px; border-radius: 4px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle { background: $SCROLL; border-radius: 3px; }
QScrollBar::handle:vertical { min-height: 30px; }
QScrollBar::handle:horizontal { min-width: 30px; }
QScrollBar::handle:hover { background: $SCROLL_HOVER; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QLineEdit, QPlainTextEdit, QTextEdit {
    background: $FIELD; border: none; border-radius: 6px; padding: 7px 12px; color: $TEXT;
    selection-background-color: $ACCENT; selection-color: $ACCENT_TEXT;
}
QLineEdit:disabled { color: $TEXT3; }
QLineEdit#Bare { background: transparent; padding: 0; border-radius: 0; }
QListView, QTreeView, QListWidget, QTreeWidget, QTableWidget { background: transparent; border: none; outline: 0; }
QHeaderView::section { background: transparent; color: $TEXT2; border: none; padding: 6px 4px; }
QMenu { background: $ITEM; border: none; padding: 4px; }
QMenu::item { padding: 8px 18px; border-radius: 6px; color: $TEXT; }
QMenu::item:selected { background: $HOVER; }
QFileDialog { background: $BASE; }
QFileDialog QWidget { color: $TEXT; }
QFileDialog QListView, QFileDialog QTreeView { background: $FIELD; }
QFileDialog QPushButton { background: $ITEM; border-radius: 6px; padding: 6px 14px; }
QFileDialog QComboBox { background: $ITEM; border-radius: 6px; padding: 4px 8px; }
QMessageBox { background: $BASE; }
QMessageBox QPushButton { background: $ITEM; border-radius: 6px; padding: 6px 14px; min-width: 70px; }
""")


def stylesheet():
    light = T.is_light()
    return _QSS.substitute(
        FONT=family(),
        TEXT=hexc("text"), TEXT2=hexc("text_2"), TEXT3=hexc("text_3"),
        FIELD=hexc("field"), ITEM=hexc("item"), HOVER=hexc("hover"), LINE=hexc("line"), BASE=hexc("base"),
        ACCENT=T.accent,
        ACCENT_TEXT=selection_text(),
        SCROLL="rgba(0,0,0,0.18)" if light else "rgba(255,255,255,0.10)",
        SCROLL_HOVER="rgba(0,0,0,0.3)" if light else "rgba(255,255,255,0.2)",
    )
