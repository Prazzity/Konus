"""Open/save dialogs with a sane size.

Qt's own file dialog sizes itself to its widest combo box. Our media filter lists every
extension, so it came out as wide as the screen and couldn't be made narrower. Here the
filter shows only its name ("Media") and the combos stop growing with their contents.
Same call signatures as QFileDialog's static helpers.
"""

from PyQt5.QtCore import QEvent, QObject, Qt, QTimer
from PyQt5.QtWidgets import QApplication, QComboBox, QFileDialog, QToolButton, QWidget

from . import sysutil, theme
from .i18n import tr


class _SideButtons(QObject):
    """Send the mouse's back/forward buttons to the dialog's own history buttons.

    The shell's dialog does this itself, but Qt's own one — the one used when "Show the file
    browser inside the open dialog" is on — only binds them to the two toolbar buttons. The
    filter is installed on the application because the press lands on whichever child the
    pointer is over: the file list, the places sidebar, the path field."""

    def __init__(self, dlg):
        super().__init__(dlg)
        self.dlg = dlg

    def eventFilter(self, obj, ev):
        if (ev.type() == QEvent.MouseButtonPress
                and ev.button() in (Qt.BackButton, Qt.ForwardButton)
                and isinstance(obj, QWidget) and obj.window() is self.dlg):
            name = "backButton" if ev.button() == Qt.BackButton else "forwardButton"
            btn = self.dlg.findChild(QToolButton, name)
            if btn is not None:
                btn.click()
            return True
        return False


def _run(parent, caption, directory, file_filter, mode, options):
    # The shell draws the caption and the filter combo, but the strings are ours: they get
    # translated here, so every caller can keep its English literal.
    dlg = QFileDialog(parent, tr(caption or ""), directory or "", tr(file_filter or ""))
    dlg.setOptions(QFileDialog.Options(options or 0) | QFileDialog.HideNameFilterDetails)
    if mode == "file":
        dlg.setFileMode(QFileDialog.ExistingFile)
    elif mode == "files":
        dlg.setFileMode(QFileDialog.ExistingFiles)
    elif mode == "dir":
        dlg.setFileMode(QFileDialog.Directory)
        dlg.setOption(QFileDialog.ShowDirsOnly, True)
    elif mode == "save":
        dlg.setAcceptMode(QFileDialog.AcceptSave)
        dlg.setFileMode(QFileDialog.AnyFile)
    for combo in dlg.findChildren(QComboBox):
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(12)
    anchor = parent.window() if parent is not None else None
    screen = (anchor.screen() if anchor is not None else None) or QApplication.primaryScreen()
    avail = screen.availableGeometry()
    dlg.setMinimumSize(480, 320)
    w = max(480, min(960, int(avail.width() * 0.8)))
    h = max(320, min(620, int(avail.height() * 0.8)))
    dlg.resize(w, h)
    if anchor is not None and anchor.isVisible():
        c = anchor.frameGeometry().center()
        # the whole card inside the screen, not just its corner: with only the lower bound the
        # Open/Cancel row ended up beyond the edge, and the dialog has its own buttons there
        x = min(max(avail.left(), c.x() - w // 2), avail.right() - w)
        y = min(max(avail.top(), c.y() - h // 2), avail.bottom() - h)
        dlg.move(max(avail.left(), x), max(avail.top(), y))
    # The shell draws this dialog, so it follows the *system* appearance: with Windows in light
    # mode its title bar stayed light while the app is dark. Ask DWM for a dark caption —
    # before the dialog is shown, not on a timer after it: the attribute only counts for the
    # first paint, and waiting left a light strip across the top for a moment. Creating the
    # native window here (winId) makes it settable that early; the shell's own child windows
    # appear a little later, so they are caught again a couple of times.
    dark = not theme.T.is_light()
    try:
        dlg.winId()                       # create the native window now, while it is invisible
    except (RuntimeError, TypeError):
        pass
    sysutil.dark_title_bar(dlg, dark)
    for delay in (60, 250, 900):
        QTimer.singleShot(delay, lambda d=dlg, k=dark: sysutil.dark_title_bar(d, k))
    # The shell's own dialog already handles the side buttons; Qt's widget dialog does not.
    side = _SideButtons(dlg) if dlg.testOption(QFileDialog.DontUseNativeDialog) else None
    app = QApplication.instance()
    if side is not None:
        app.installEventFilter(side)
    try:
        accepted = dlg.exec_() == QFileDialog.Accepted
    finally:
        if side is not None:
            app.removeEventFilter(side)
    if not accepted:
        return []
    return dlg.selectedFiles()


class FileDialog:
    @staticmethod
    def getOpenFileName(parent=None, caption="", directory="", filter="", initialFilter="", options=0):
        files = _run(parent, caption, directory, filter, "file", options)
        return (files[0] if files else "", "")

    @staticmethod
    def getOpenFileNames(parent=None, caption="", directory="", filter="", initialFilter="", options=0):
        return (_run(parent, caption, directory, filter, "files", options), "")

    @staticmethod
    def getExistingDirectory(parent=None, caption="", directory="", options=0):
        files = _run(parent, caption, directory, "", "dir", options)
        return files[0] if files else ""

    @staticmethod
    def getSaveFileName(parent=None, caption="", directory="", filter="", initialFilter="", options=0):
        files = _run(parent, caption, directory, filter, "save", options)
        return (files[0] if files else "", "")
