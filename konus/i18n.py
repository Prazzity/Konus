"""Localization.

The interface is written in English. This module maps those strings to the user's
language at the moment they enter the UI — mostly inside widget constructors (see the
`tr()` calls in widgets, dialogs, panels and prefs) — so the code keeps its English
literals and the store keeps English *values* (theme names, fx presets, language codes).
Only what the user actually reads is replaced.

Translations live in `i18n_<code>.py` next to this file as a plain dict:

    TABLE = {"Play": "Воспроизвести", ...}

Strings that hold placeholders are written the same way and formatted at the call
site, e.g. tr("Added {n} to {name}").format(n=3, name=title).
"""
from importlib import import_module

# (name shown in Preferences, code). "System" follows the OS locale at startup.
LANGUAGES = [("System", "system"), ("English", "en"), ("Русский", "ru")]

FALLBACK = "en"
_current = FALLBACK
_table = {}
_tables = {}


def _load(code):
    if code not in _tables:
        try:
            mod = import_module(f".i18n_{code}", __package__)
            _tables[code] = getattr(mod, "TABLE", {})
        except ImportError:
            _tables[code] = {}
    return _tables[code]


def code_for(name):
    """The language code behind the name shown in Preferences ("Русский" -> "ru")."""
    for shown, code in LANGUAGES:
        if name == shown or name == code:
            return code
    return name or "en"


def detect_system():
    """The Windows UI language, as one of our codes (falls back to English).

    Python's locale module reports the C/en-US locale whatever the system language is,
    so ask Windows itself which language its own interface is in."""
    try:
        import ctypes
        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        if primary == 0x19:              # LANG_RUSSIAN
            return "ru"
    except Exception:
        pass
    return FALLBACK


def language():
    return _current


def set_language(name):
    """`name` is what Preferences stores: "System", "English", "Русский" or a code."""
    global _current, _table
    code = code_for(name)
    if code == "system":
        code = detect_system()
    _current = code
    _table = _load(code) if code != "en" else {}
    return _current


def tr(text):
    """The user-facing form of an English UI string (unchanged for English)."""
    if not text or not _table:
        return text
    out = _table.get(text)
    if out is None:
        return text
    return out


def trn(singular, plural, n):
    """A counted noun with its number: "3 items" / "3 элемента"."""
    if _current != "ru":
        return f"{n} {singular if n == 1 else plural}"
    word = tr(singular) if n % 10 == 1 and n % 100 != 11 else (
        tr(plural + "_few") if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else tr(plural))
    return f"{n} {word}"


def tr_opts(options):
    """[(value, label)] for dropdowns: the stored value stays English, the label is
    translated. Plain strings keep working as both."""
    out = []
    for o in options:
        if isinstance(o, (tuple, list)) and len(o) == 2:
            out.append((o[0], tr(str(o[1]))))
        else:
            out.append((o, tr(str(o))))
    return out
