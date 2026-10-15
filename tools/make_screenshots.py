"""Take the screenshots the READMEs show, from the application itself.

    .venv\\Scripts\\python.exe tools\\make_screenshots.py --media "E:\\Movies\\Open Movies"
    .venv\\Scripts\\python.exe tools\\make_screenshots.py --media "E:\\Movies\\Open Movies" ^
        --lang ru --out screenshots\\ru
    .venv\\Scripts\\python.exe tools\\make_screenshots.py --media "E:\\Movies\\Caminandes" --only tracks

The app really runs: libVLC decodes, the panels are the shipped ones, the window is the real
window. It is photographed from the screen region it occupies, because the video is drawn by
libVLC into the widget's window handle and a Qt widget grab would come out black there.

Two things are arranged first, because a screenshot should not show this machine:

* everything the app writes goes to a scratch folder (%TEMP%\\konus-shots), so the pictures
  show a fresh install, not this desktop's settings and history;
* the library points at `--media`, which holds the films to show. Blender Studio's open movies
  are what it is meant for: they may be shown and reshared with credit (CC BY), the README
  credits them, and each picture is taken at a chosen minute of its film — the timestamps in
  SHOTS — rather than wherever playback happened to be.

The shots, and what each of them needs in `--media`:

* `player`, `preset`, `library` — films, any folder of them;
* `tracks` — a folder of *episodes* with a subfolder of their audio tracks ("RUS Sound" in the
  wild), because that is what the matcher looks at;
* `subtitles` — a folder of episodes with `.srt` files beside them.
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

SCRATCH = Path(os.environ.get("KONUS_SHOTS_CONFIG") or Path(tempfile.gettempdir()) / "konus-shots")
# run the app out of a scratch folder instead of the real one, so photographing it never
# touches the settings, the library or the cache of whoever runs this (see konus.portable)
os.environ["KONUS_DATA_DIR"] = str(SCRATCH)

from PyQt5.QtCore import QEvent, QPointF, Qt, QTimer                      # noqa: E402
from PyQt5.QtGui import QGuiApplication, QMouseEvent                     # noqa: E402
from PyQt5.QtWidgets import QApplication                                 # noqa: E402

import main as entry                                                     # noqa: E402

# What is photographed, what it plays, and the minute of it that looks good: (name, film,
# timestamp). The films are matched by the start of their file name; the first one present is
# used as a fallback, so the tool still runs with a different library of open films.
SHOTS = [
    ("player",    "Sprite Fright",   15_000),   # the girl and the snail, in colour
    ("preset",    "Sprite Fright",   55_000),   # the family by the tent
    ("library",   "Sprite Fright",   15_000),
    ("tracks",    "Caminandes",      30_000),   # a folder of episodes with their tracks
    ("subtitles", "Tears of Steel", 322_000),   # live action, the street in Amsterdam
]

PRESET = "Open Movies"


def find_movie(media, key):
    films = [p for p in sorted(media.iterdir()) if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")]
    if not films:
        raise SystemExit(f"no films in {media} — the README screenshots show Blender's open movies; "
                         "see this file's docstring for where they come from")
    for f in films:
        if f.name.lower().startswith(key.lower()):
            return f
    return films[0]


def boot(media, lang):
    """Start the app the way main.py does, on a library that points at `media`."""
    entry.setup_vlc_windows()
    entry.load_vlc_binding()

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    from konus import APP_ID, i18n, theme, ui
    app = QApplication([APP_ID])
    app.setApplicationName(APP_ID)
    app.setQuitOnLastWindowClosed(False)

    from konus.store import Store
    store = Store()
    store["onboarded"] = True
    store["video_folders"] = [str(media)]
    store["theme"] = "dark"
    store["accent"] = "#d37918"
    store["title_filename"] = True
    store.save()

    i18n.set_language(lang)
    theme.load_fonts()
    app.setStyle(ui.Style("Fusion"))
    theme.apply(store.get("theme"), store.get("accent"))
    app.setFont(theme.font(14))

    from konus.engine import Engine
    from konus.window import MainWindow
    engine = Engine(store)
    win = MainWindow(engine, store)

    avail = QGuiApplication.primaryScreen().availableGeometry()
    size = (min(1600, avail.width() - 40), min(900, avail.height() - 60))
    win.resize(*size)
    win.move(avail.x() + (avail.width() - size[0]) // 2,
             avail.y() + max(0, (avail.height() - size[1]) // 2 - 20))
    win.show()
    print(f"window {size[0]}x{size[1]} on a {avail.width()}x{avail.height()} desktop, "
          f"device pixel ratio {QGuiApplication.primaryScreen().devicePixelRatio()}")
    return app, store, engine, win


def pump(app, seconds):
    """Wait while keeping the interface alive."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def capture(app, win, path, extra=()):
    """Photograph the window — and any floating window given with it — from the screen.

    The window is asked to stay on top for the moment of the grab: this process is not the
    one the user is typing into, so Windows will not let it come to the front by itself, and
    a screen grab would photograph whatever window happens to be there instead.
    """
    from konus import sysutil
    widgets = (win, *extra)
    for widget in widgets:
        sysutil.set_above(widget, True)
    pump(app, 0.8)
    try:
        rect = win.frameGeometry()
        for widget in extra:
            rect = rect.united(widget.frameGeometry())
        shot = QGuiApplication.primaryScreen().grabWindow(0, rect.x(), rect.y(),
                                                          rect.width(), rect.height())
        path.parent.mkdir(parents=True, exist_ok=True)
        shot.save(str(path), "JPEG", 92)
        print(f"  wrote {path.relative_to(ROOT)}  {shot.width()}x{shot.height()} "
              f"from {rect.x()},{rect.y()}")
    finally:
        for widget in widgets:
            sysutil.set_above(widget, False)


def capture_menu(win, path):
    """Photograph the window with its open popup menu composited in.

    A Qt popup is a window of its own that closes as soon as the focus moves, and this
    process cannot hold the focus while it takes a picture — so the menu is rendered from the
    widget itself and painted where it sits over the window, in the same grab.
    """
    from PyQt5.QtCore import QPoint
    from PyQt5.QtGui import QPainter
    from konus.widgets import PopupMenu

    rect = win.frameGeometry()
    shot = QGuiApplication.primaryScreen().grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height())
    menus = [w for w in QApplication.topLevelWidgets() if isinstance(w, PopupMenu) and w.isVisible()]
    if not menus:
        print("  ! the menu did not open: the picture will be missing it")
    painter = QPainter(shot)
    for menu in menus:
        pos = menu.mapToGlobal(QPoint(0, 0)) - rect.topLeft()
        print(f"    menu at window {pos.x()},{pos.y()}  size {menu.width()}x{menu.height()}  "
              f"window at {rect.x()},{rect.y()}")
        painter.drawPixmap(pos, menu.grab())
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    shot.save(str(path), "JPEG", 92)
    print(f"  wrote {path.relative_to(ROOT)}  {shot.width()}x{shot.height()} with {len(menus)} menu(s)")


def stage_shot(name, app, win, store, engine, media, out):
    """Put the window in the state the shot is about, and photograph it."""
    shot = lambda filename, extra=(): capture(app, win, out / filename, extra)

    if name == "player":
        if not win.sidebar_open:                 # the library beside the video, as in the design
            win.toggle_sidebar()
        QTimer.singleShot(1400, lambda: shot("player.jpg"))

    elif name == "preset":
        # The feature the README leads with: settings saved as a preset and tied to the
        # folder, so every episode in it comes back to them.
        store.save_preset(PRESET, engine.preset_values())
        store.set_folder_preset(str(media), PRESET)
        engine.apply_preset(PRESET)
        engine.current_preset = PRESET
        win.toggle_panel("settings", "video", force=True)
        QTimer.singleShot(1600, lambda: (press_preset_dropdown(win),
                                         capture_menu(win, out / "preset.jpg"),
                                         win.close_panels()))

    elif name == "library":
        win.show_library_window()
        win.library.navigate(("videos", str(media)))
        win.place_mini()
        QTimer.singleShot(2400, lambda: (shot("library.jpg"), win.show_player()))

    elif name == "tracks":
        from konus.dialogs import AudioTracksDialog
        win.show_player()
        win.close_panels()
        if win.sidebar_open:
            win.toggle_sidebar()
        dialog = AudioTracksDialog(win, str(media))     # the folder of episodes and their tracks
        dialog.show()
        QTimer.singleShot(1600, lambda: (shot("tracks.jpg", extra=[dialog]), dialog.close()))

    elif name == "subtitles":
        from konus.dialogs import AddSubtitlesDialog
        win.show_player()
        win.close_panels()
        if win.sidebar_open:                     # the dialog is the subject: nothing beside it
            win.toggle_sidebar()
        dialog = AddSubtitlesDialog(win, str(media), "folder", None)
        dialog.show()
        QTimer.singleShot(1400, lambda: (shot("subtitles.jpg", extra=[dialog]), dialog.close()))


def press_preset_dropdown(win):
    """Press the preset dropdown of the panel's visible tab, which is what opens its menu.

    Each tab carries a footer with its own dropdown, and the hidden ones sit at a position
    that belongs to another page — pressing one of those opens the menu in the wrong place.
    """
    from konus.panels import PresetDropdown
    dropdowns = [d for d in win.panels["settings"].findChildren(PresetDropdown) if d.isVisible()]
    if not dropdowns:
        print("  ! no preset dropdown on screen")
        return
    QApplication.sendEvent(dropdowns[0], QMouseEvent(QEvent.MouseButtonPress, QPointF(12, 12),
                                                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))


def build_plan(app, win, store, engine, media, out, wanted):
    plan = []                                   # (delay in ms, what to do)
    current = None
    shots = [s for s in sorted(SHOTS, key=lambda s: s[1]) if not wanted or s[0] in wanted]

    for name, film, ms in shots:
        movie = find_movie(media, film)
        if movie != current:
            current = movie
            plan.append((600, lambda m=movie: win.play([str(m)])))
            plan.append((5000, lambda ms=ms: engine.seek_to(ms)))     # let it start, then jump
        else:
            plan.append((400, lambda ms=ms: engine.seek_to(ms)))
        plan.append((2000, lambda n=name: stage_shot(n, app, win, store, engine, media, out)))
        plan.append((2500, lambda: None))        # the floating windows opened by a shot take a moment
    plan.append((400, lambda: None))
    return plan


def run_plan(app, win, plan):
    """Chain the steps on the event loop, keeping the window in front between them."""
    queue = list(plan)

    def run(fn):
        try:
            fn()
        except Exception as exc:                 # a shot that fails should not stop the rest
            print(f"  ! {type(exc).__name__}: {exc}")
        win.raise_()
        win.activateWindow()
        for _ in range(30):                      # let the compositor put it in front before the grab
            app.processEvents()
            time.sleep(0.01)

    def advance():
        if not queue:
            app.quit()
            return
        delay, fn = queue.pop(0)
        QTimer.singleShot(delay, lambda: (run(fn), advance()))

    advance()
    sys.exit(app.exec_())


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--media", required=True, type=Path,
                    help="folder of films to show: openly licensed ones, since the pictures end up in the README")
    ap.add_argument("--out", default=ROOT / "screenshots", type=Path, help="where the pictures go")
    ap.add_argument("--lang", default="en", help="interface language for the screenshots")
    ap.add_argument("--only", default="", help="comma-separated subset of "
                                              + ",".join(s[0] for s in SHOTS))
    args = ap.parse_args()

    media = args.media.resolve()
    if not media.is_dir():
        raise SystemExit(f"{media} is not there: put the films to show in it (see the module docstring)")
    out = args.out if args.out.is_absolute() else (ROOT / args.out).resolve()
    wanted = {s.strip() for s in args.only.split(",") if s.strip()}

    app, store, engine, win = boot(media, args.lang)
    run_plan(app, win, build_plan(app, win, store, engine, media, out, wanted))


if __name__ == "__main__":
    main()
