"""Global hotkeys: work while other applications have focus.

RegisterHotKey() registers them with the system; the thread below runs its own message loop
and the WM_HOTKEY messages arrive there. A key that is already taken by another program
simply fails to register.
"""

import ctypes
import threading
from ctypes import wintypes

from PyQt5.QtCore import QObject, pyqtSignal

_user32 = ctypes.windll.user32

MODS = {"ctrl": 0x0002, "shift": 0x0004, "alt": 0x0001, "meta": 0x0008, "super": 0x0008}
KEYNAMES = {
    "space": 0x20, "esc": 0x1B, "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
    "pgup": 0x21, "pgdown": 0x22, "pageup": 0x21, "pagedown": 0x22, "del": 0x2E, "ins": 0x2D,
    "return": 0x0D, "enter": 0x0D, "home": 0x24, "end": 0x23, "tab": 0x09,
    "backspace": 0x08, "pause": 0x13, "print": 0x2C, "plus": 0x6B,
    "media play": 0xB3, "media next": 0xB0, "media previous": 0xB1, "media stop": 0xB2,
    "volume up": 0xAF, "volume down": 0xAE, "volume mute": 0xAD,
}
# F1–F12, which the shortcuts dialog offers and the most common thing to bind globally: they used
# to parse to nothing, so the key was accepted, stored, and never registered.
KEYNAMES.update({f"f{i}": 0x6F + i for i in range(1, 13)})
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class GlobalHotkeys(QObject):
    triggered = pyqtSignal(str)

    def __init__(self, mapping):
        super().__init__()
        self.mapping = mapping
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread.ident:
            _user32.PostThreadMessageW(self._thread.ident, WM_QUIT, 0, 0)

    def _parse(self, seq):
        if str(seq).strip() == "+":
            return (KEYNAMES["plus"], 0)      # the numpad plus alone (Ctrl++ comes out of the
        parts = [p.strip() for p in str(seq).replace("++", "+plus").split("+") if p.strip()]
        mods = 0
        key = None
        for p in parts:
            low = p.lower()
            if low in MODS:
                mods |= MODS[low]
            else:
                key = p
        if key is None:
            return None
        name = key.lower()
        if name in KEYNAMES:
            vk = KEYNAMES[name]
        elif len(key) == 1:
            found = self._letter_vk(key)
            if found is None:
                return None
            vk, needs_shift = found
            # "%" is Shift+5 and has to be registered as such (the bare "5" was swallowed
            # system-wide instead), but a letter's shift bit only means "this is the uppercase
            # form": QKeySequence writes "L" for the letter, so Shift must not be added there
            if needs_shift and not key.isalnum():
                mods |= 0x0004                       # MOD_SHIFT
        else:
            return None
        return (vk, mods)

    @staticmethod
    def _letter_vk(key):
        """The virtual key for a single character.

        VkKeyScanW asks the *active* keyboard layout: with a Russian layout loaded it returns
        -1 for a Latin letter, so every global hotkey with a letter silently failed to
        register. Ask the English layout as well — the physical key is what the user means."""
        for layout in (None, 0x04090409):        # active first, then en-US
            try:
                r = (_user32.VkKeyScanW(ord(key.upper())) if layout is None
                     else _user32.VkKeyScanExW(ord(key.upper()), layout))
            except AttributeError:
                continue
            if r != -1:
                return r & 0xFF, bool((r >> 8) & 1)      # (virtual key, needs shift)
        return None

    def _run(self):
        ids = {}
        nid = 1
        for aid, seq in self.mapping.items():
            parsed = self._parse(seq)
            if not parsed:
                continue
            vk, mods = parsed
            if _user32.RegisterHotKey(None, nid, mods | MOD_NOREPEAT, vk):
                ids[nid] = aid
                nid += 1
        if not ids:
            return
        msg = wintypes.MSG()
        while not self._stop.is_set() and _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                aid = ids.get(int(msg.wParam))
                if aid:
                    self.triggered.emit(aid)
        for hid in ids:
            _user32.UnregisterHotKey(None, hid)

