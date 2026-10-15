"""Window drags (move / resize) for the frameless windows.

The drag is handed to the system: ReleaseCapture() plus WM_NCLBUTTONDOWN with the HT* code
for the edge, which gives the window the move and resize behaviour any other window has —
snapping, Aero behaviour, and dragging a maximized window back to its normal size. This also
sidesteps Qt's startSystemMove(), which is unreliable next to the native video child window.

erase_colour() gives a window class the app's own background colour, and snap_to_top() adds
the maximize a drag to the top edge should do (see there for why the system skips it).
"""

import ctypes

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QCursor, QGuiApplication

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
WM_NCLBUTTONDOWN = 0x00A1
GCLP_HBRBACKGROUND = -10

_user32.GetClassLongPtrW.restype = ctypes.c_void_p
_user32.GetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.SetClassLongPtrW.restype = ctypes.c_void_p
_user32.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
_gdi32.CreateSolidBrush.restype = ctypes.c_void_p
_gdi32.CreateSolidBrush.argtypes = [ctypes.c_ulong]

# Brushes handed to a window class stay in use for as long as the class does, so they are
# created once per colour and never freed.
_brushes = {}


def erase_colour(window, colour):
    """Give the window class a background brush in the app's own colour.

    The class background brush is what Windows has to fill a part of a window the app has not
    painted — the strip a resize has just exposed, for one — and Qt registers its window
    classes with a *white* one (measured: solid 0xffffff). Nothing in the app is white, so the
    strip belongs to the app's background in this colour. The brush belongs to the class, so
    one call covers every window Qt builds of that kind."""
    key = (colour.red(), colour.green(), colour.blue())
    brush = _brushes.get(key)
    if brush is None:
        brush = _gdi32.CreateSolidBrush(key[0] | key[1] << 8 | key[2] << 16)   # COLORREF
        _brushes[key] = brush
    try:
        hwnd = ctypes.c_void_p(int(window.winId()))
    except (RuntimeError, TypeError):
        return
    _user32.SetClassLongPtrW(hwnd, GCLP_HBRBACKGROUND, brush)

HT = {
    int(Qt.LeftEdge): 10,                                   # HTLEFT
    int(Qt.RightEdge): 11,                                  # HTRIGHT
    int(Qt.TopEdge): 12,                                    # HTTOP
    int(Qt.TopEdge | Qt.LeftEdge): 13,                      # HTTOPLEFT
    int(Qt.TopEdge | Qt.RightEdge): 14,                     # HTTOPRIGHT
    int(Qt.BottomEdge): 15,                                 # HTBOTTOM
    int(Qt.BottomEdge | Qt.LeftEdge): 16,                   # HTBOTTOMLEFT
    int(Qt.BottomEdge | Qt.RightEdge): 17,                  # HTBOTTOMRIGHT
}


def snap_to_top(window):
    """Maximize when a title-bar drag ended against the top edge of the screen.

    Windows' own "drag to the top edge to maximize" (Aero Snap) never fires for this window:
    it is frameless, so its styles are WS_POPUP without WS_THICKFRAME/WS_MAXIMIZEBOX, and the
    system only snaps windows it considers resizable. The native move loop still runs, so the
    drag itself works — this adds the piece the system skips."""
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    if screen is None or window.isFullScreen() or window.isMaximized():
        return
    if QCursor.pos().y() <= screen.geometry().top() + 4:
        # the main window animates the growth (see MainWindow.maximize_animated); anything
        # else just maximizes
        grow = getattr(window, "maximize_animated", None)
        grow() if grow else window.showMaximized()


def start(window, edges=None, snap=False):
    """Hand the current left-button drag on `window` to the window manager.

    `snap` is for the main window only: the small always-on-top windows (PiP, mini player)
    keep their own geometry, and maximizing one of them on a drag is not wanted."""
    handle = window.windowHandle()
    if handle is None:
        return
    code = 2 if edges is None else HT.get(int(edges))        # 2 = HTCAPTION
    if code is None:
        return
    hwnd = int(window.winId())
    _user32.ReleaseCapture()
    # SendMessageW blocks for the whole drag: the default handler runs the system's modal
    # move loop, so the call returns once the button was released.
    _user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, code, 0)
    if snap and code == 2:
        snap_to_top(window)

