"""Small Windows helpers: opening folders in Explorer, keeping a window on top, the VLC Lua
extension folders, the drive list for the disc dialog and the dark caption on native dialogs.
"""

import ctypes
import os
import subprocess
from ctypes import wintypes
from pathlib import Path


def open_path(path):
    """Open a folder (or file) in Explorer."""
    os.startfile(str(path))                              # noqa: S606 - the point is to open it


def reveal(path):
    """Show a file with its folder open and the file selected."""
    subprocess.Popen(["explorer", "/select,", os.path.normpath(str(path))])


def set_above(widget, on):
    """Keep `widget`'s window above the others (or stop doing that)."""
    hwnd = int(widget.winId())
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
    user32 = ctypes.windll.user32
    # The argtypes matter: without them the -1 sentinel is passed as a 32-bit value in a
    # 64-bit slot (0x00000000FFFFFFFF), SetWindowPos fails with ERROR_INVALID_WINDOW_HANDLE
    # and the window never becomes topmost.
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE)


def lua_extension_dirs(vlc_dir=None):
    """Where VLC keeps its Lua extensions."""
    dirs = [Path(os.environ.get("APPDATA", Path.home())) / "vlc" / "lua" / "extensions"]
    if vlc_dir:
        dirs.append(Path(vlc_dir) / "lua" / "extensions")
    return dirs


def optical_drives():
    """Drive letters for the Open Disc dialog ('D:' and the like)."""
    DRIVE_CDROM = 5
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    out = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(ord('A') + i)}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_CDROM:
                out.append(root[:2])
    return out


def dark_title_bar(widget, dark=True):
    """Ask Windows to draw a window's caption dark.

    The app paints itself, so it stays dark whatever the system setting is; a native dialog
    (the file chooser) is drawn by the shell instead and keeps a light title bar when Windows
    is in light mode. DWM can be told per window, and the shell's dialog is a child of ours,
    so every window below the dialog gets the same attribute."""
    if widget is None:
        return 0
    user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
    value = ctypes.c_int(1 if dark else 0)
    done = []

    def apply(hwnd):
        for attr in (20, 19):          # DWMWA_USE_IMMERSIVE_DARK_MODE: current, older builds
            if dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                            ctypes.sizeof(value)) == 0:
                done.append(int(hwnd))
                return

    try:
        top = int(widget.winId())
    except (RuntimeError, TypeError):
        return 0
    apply(top)
    children = []

    def cb(hwnd, _l):
        children.append(hwnd)
        return True

    try:
        user32.EnumChildWindows(wintypes.HWND(top), ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    except Exception:
        pass
    for hwnd in children:
        apply(hwnd)
    return len(done)
