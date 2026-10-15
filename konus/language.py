"""The language screen (640×470): the first-run question, asked before the interface exists.

The interface is written in English and translated as each widget is built (see konus.i18n),
so the language has to be known before the first widget is made. That is the whole point of
asking here rather than in Settings: the window then comes up in the chosen language, with
nothing to restart. main.py opens this on a run where the question was never asked; picking a
language marks it asked, so it appears once — changing the language later is a Settings row,
and it lands at once there (see MainWindow.apply_language).

The screen itself is not translated, because there is nothing to translate it into yet: every
row carries its language's name written *in* that language, and the two lines at the top say
the same thing in English and in Russian, which is what this build speaks.
"""

from PyQt5.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from . import i18n, icons, theme
from .theme import c
from .widgets import Button, Label, paint_shadow

# The two lines the screen always carries. Written out rather than read from a table: there is
# no language in use to read one from yet — that is what this screen settles. A table added
# later gets its line here.
TITLE = "Choose your language"
SUBTITLE = "Выберите язык интерфейса"


class Choice(QWidget):
    """One language: its name written in that language, a dot that fills when it is picked,
    and — for the system choice — the language Windows is actually in."""

    picked = pyqtSignal(str)
    confirmed = pyqtSignal(str)

    def __init__(self, code, name, hint=""):
        super().__init__()
        self.code, self.name, self.hint = code, name, hint
        self.selected = False
        self.setFixedHeight(56)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.picked.emit(self.code)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.confirmed.emit(self.code)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if self.selected:
            p.setBrush(c("hover"))
        else:
            p.setBrush(c("hover") if self.underMouse() else c("field"))
        p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        cy = self.height() / 2
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(c("accent") if self.selected else c("knob_off"), 2))
        p.drawEllipse(QPointF(28, cy), 8, 8)
        if self.selected:
            p.setPen(Qt.NoPen)
            p.setBrush(c("accent"))
            p.drawEllipse(QPointF(28, cy), 4, 4)
        f = theme.font(16, theme.MEDIUM if self.selected else 50)
        p.setFont(f)
        p.setPen(c("text_hi") if self.selected else c("text"))
        p.drawText(QRectF(54, 0, self.width() - 70, self.height()), Qt.AlignVCenter, self.name)
        if self.hint:
            p.setFont(theme.font(12))
            p.setPen(c("text_2"))
            p.drawText(QRectF(self.width() - 150, 0, 136, self.height()),
                       Qt.AlignVCenter | Qt.AlignRight, self.hint)


class LanguageDialog(QDialog):
    MARGIN = 24

    def __init__(self, code="system"):
        super().__init__(None, Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.chosen = code if code in i18n.CHOICES else "system"
        self._drag = None
        self.resize(640 + 2 * self.MARGIN, 470 + 2 * self.MARGIN)
        v = QVBoxLayout(self)
        v.setContentsMargins(self.MARGIN + 40, self.MARGIN + 30, self.MARGIN + 40, self.MARGIN + 26)
        v.setSpacing(0)
        logo = QLabel()
        logo.setPixmap(icons.pixmap("logo", 44))
        v.addWidget(logo, 0, Qt.AlignHCenter)
        v.addSpacing(20)
        for text, size, color, weight in ((TITLE, 24, "text_hi", 300), (SUBTITLE, 16, "text_2", None)):
            line = Label(text, size, color, weight=weight)
            line.setAlignment(Qt.AlignCenter)
            v.addWidget(line)
            v.addSpacing(6)
        v.addSpacing(20)
        self.rows = {}
        for code in i18n.CHOICES:
            name = i18n.native_name(code)
            hint = ""
            if code == "system":
                name = f"{i18n.in_language('en', 'System language')} · {i18n.in_language('ru', 'System language')}"
                hint = i18n.native_name(i18n.system_language())
            row = Choice(code, name, hint)
            row.picked.connect(self.pick)
            row.confirmed.connect(lambda c=code: (self.pick(c), self.accept()))
            v.addWidget(row)
            v.addSpacing(8)
            self.rows[code] = row
        v.addStretch(1)
        self.go = Button("Continue", "primary", height=36).fit_to_text(minimum=170, pad=44)
        self.go.clicked.connect(self.accept)
        v.addWidget(self.go, 0, Qt.AlignHCenter)
        self.pick(self.chosen)

    def pick(self, code):
        """Mark a language, and let the button speak it: it is the one word on screen that can
        answer in the language being chosen."""
        self.chosen = code
        for key, row in self.rows.items():
            row.selected = key == code
            row.update()
        spoken = i18n.system_language() if code == "system" else code
        self.go.setText(i18n.in_language(spoken, "Continue"))
        self.go.fit_to_text(minimum=170, pad=44)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Up, Qt.Key_Down):
            order = list(self.rows)
            step = -1 if e.key() == Qt.Key_Up else 1
            self.pick(order[(order.index(self.chosen) + step) % len(order)])
        else:
            super().keyPressEvent(e)          # Esc rejects, Enter accepts

    def showEvent(self, e):
        screen = self.screen()
        if screen is not None:
            g = screen.availableGeometry()
            self.move(g.center() - self.rect().center())
        super().showEvent(e)

    def mousePressEvent(self, e):
        self._drag = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m = self.MARGIN
        r = QRectF(self.rect()).adjusted(m, m, -m, -m)
        paint_shadow(p, r, 14, spread=16, alpha=0.5, dy=8)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#0e0e0e") if not theme.T.is_light() else c("base"))
        p.drawRoundedRect(r, 14, 14)


def ask(store):
    """Ask for the language, once, and keep the answer. main.py calls this before the engine
    and the window are made, so the interface is built in the language chosen here.

    Escape (or closing the screen) keeps the language the store already had — English, on a
    fresh install — and the question is not asked again either way: the row for changing it
    later is in Settings."""
    dialog = LanguageDialog(store.get("language") or "system")
    code = dialog.chosen if dialog.exec_() == QDialog.Accepted else i18n.code_for(store.get("language"))
    i18n.set_language(code)
    store.set("language", code)
    store["language_chosen"] = True
    store.save()
    return code
