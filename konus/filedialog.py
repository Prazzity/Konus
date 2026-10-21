"""Open/save dialogs with a sane size.

Qt's own file dialog sizes itself to its widest combo box. Our media filter lists every
extension, so it came out as wide as the screen and couldn't be made narrower. Here the
filter shows only its name ("Media") and the combos stop growing with their contents.
Same call signatures as QFileDialog's static helpers.
"""

import os
import time

from PyQt5.QtCore import QEvent, QObject, Qt, QTimer, QUrl
from PyQt5.QtWidgets import (QApplication, QComboBox, QFileDialog, QFileSystemModel, QToolButton,
                             QWidget)

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


def _store_of(parent):
    """The settings store behind a dialog's parent: the window itself, a dialog's `.win`, or
    the window a widget belongs to. None when the caller has no store (the static dialogs can
    be used on their own)."""
    obj, seen = parent, set()
    while obj is not None and id(obj) not in seen:
        seen.add(id(obj))
        store = getattr(obj, "store", None)
        if store is not None:
            return store
        nxt = getattr(obj, "win", None)
        if nxt is not None:
            obj = nxt
            continue
        obj = obj.parentWidget() if isinstance(obj, QWidget) else None
    return None


def _preload(dlg, directory, timeout=0.6):
    """Let the file list load before the window is mapped.

    The dialog is mapped before its first paint, and the first paint has to wait for the
    directory: until it comes the compositor shows a white ghost of the window over the dark
    app — the flash that lasted a moment on every open. The model is loaded here, without
    showing the dialog, so the first visible frame has its rows and no ghost is left to show."""
    model = dlg.findChild(QFileSystemModel)
    if model is None:
        return
    loaded = []
    model.directoryLoaded.connect(lambda _path: loaded.append(True))
    model.setRootPath(dlg.directory().absolutePath() or directory)
    # Lay the dialog and its views out, and let them paint once, while the window is not on
    # screen. Hiding it again after the attribute is dropped leaves show() a real mapping to
    # do, so the window that comes up has a painted backing store instead of a white one.
    dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
    dlg.show()
    end = time.monotonic() + timeout
    while not loaded and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    dlg.setAttribute(Qt.WA_DontShowOnScreen, False)
    dlg.hide()


def _run(parent, caption, directory, file_filter, mode, options):
    # The shell draws the caption and the filter combo, but the strings are ours: they get
    # translated here, so every caller can keep its English literal.
    dlg = QFileDialog(parent, tr(caption or ""), directory or "", tr(file_filter or ""))
    dlg.setOptions(QFileDialog.Options(options or 0) | QFileDialog.HideNameFilterDetails)
    # The folders picked lately, most used first, on the left of the dialog itself. The two
    # standard places stay in front of them: "This PC" (`file://`) and the user's own folder.
    # Setting the list replaces the shortcuts Qt remembers, so leaving them out removed both.
    store = _store_of(parent)
    places = store.folder_places() if store is not None else []
    if store is not None:
        urls = [QUrl("file://"), QUrl.fromLocalFile(os.path.expanduser("~"))]
        seen = {u.toString() for u in urls}
        for path in places:
            url = QUrl.fromLocalFile(path)
            if url.toString() not in seen:
                seen.add(url.toString())
                urls.append(url)
        dlg.setSidebarUrls(urls)
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
    if dlg.testOption(QFileDialog.DontUseNativeDialog):
        _preload(dlg, directory)
    # The window is mapped before its first paint, and the file list takes a moment to load:
    # until then the compositor shows a white ghost of the window over the dark app. Painting
    # the first frame here, before the modal loop goes to work, leaves no gap for it.
    dlg.show()
    dlg.repaint()
    try:
        accepted = dlg.exec_() == QFileDialog.Accepted
    finally:
        if side is not None:
            app.removeEventFilter(side)
    if not accepted:
        return []
    paths = dlg.selectedFiles()
    if store is not None:
        # A folder opened through this dialog is a folder used: it enters the same recent
        # list, so it is one click away the next time.
        for path in paths:
            folder = path if os.path.isdir(path) else os.path.dirname(path)
            if folder and os.path.isdir(folder):
                store.visit_folder(folder)
        if paths:
            store.libraryChanged.emit("recent")
    return paths


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
