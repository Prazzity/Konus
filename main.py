"""Entry point: python main.py [files, folders or URLs...]

The packaged build (`packaging/`) runs this same file with the data files — the assets, the
licences and the libVLC runtime — inside the bundle. The portable build is the same program
with `konus/_portable.py` in the bundle: from that flag on, everything the player writes —
settings, library, cache, log — goes into a `data` folder beside the executable (see
konus.portable).
"""

import json
import os
import shutil
import sys
import warnings

if getattr(sys, "frozen", False):
    # Frozen build: the bundle holds the data files, and the executable's own folder is
    # looked at first, so a runtime dropped in there by hand still wins.
    EXE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    HERE = getattr(sys, "_MEIPASS", EXE_DIR)
else:
    HERE = EXE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIRS = [os.path.join(EXE_DIR, "vendor"), os.path.join(HERE, "vendor")]
for _d in reversed(VENDOR_DIRS):
    if os.path.isdir(_d):
        sys.path.insert(0, _d)                     # bundled python-vlc binding
sys.path.insert(0, HERE)
# The bundled python-vlc has docstrings Python 3.12 warns about the first time it compiles them.
warnings.filterwarnings("ignore", category=SyntaxWarning)

from konus import portable  # noqa: E402  (after sys.path, before anything reads a path)
from konus import startup  # noqa: E402  (marks startup steps when KONUS_STARTUP_LOG is set)

# The portable build settles on its data folder — creating it, or deciding it cannot and
# falling back to AppData — before anything else can ask where its files go.
portable.setup()
startup.mark(f"data folder decided ({portable.data_dir() or 'AppData'})")

# os.add_dll_directory() handles; keep them alive for the whole run.
_VLC_DLL_DIRS = []


def find_vlc_dir():
    """The folder that holds libvlc.dll. Looked up in $VLC_DIR, the runtime fetched next to
    main.py (fetch_libvlc.py fills runtime/libvlc), a vlc_dir.txt written next to main.py
    (setup.bat writes it), the registry, then the usual folders."""
    for var in ("VLC_DIR",):
        p = os.environ.get(var)
        if p and os.path.isfile(os.path.join(p, "libvlc.dll")):
            return p
    for base in (EXE_DIR, HERE):
        bundled = os.path.join(base, "runtime", "libvlc")
        if os.path.isfile(os.path.join(bundled, "libvlc.dll")):
            return bundled
    for base in (EXE_DIR, HERE):
        f = os.path.join(base, "vlc_dir.txt")
        if not os.path.isfile(f):
            continue
        try:
            with open(f, encoding="utf-8-sig") as fh:
                p = fh.read().strip().strip('"')
            if p and os.path.isfile(os.path.join(p, "libvlc.dll")):
                return p
        except OSError:
            pass
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"Software\VideoLAN\VLC") as k:
                    p = winreg.QueryValueEx(k, "InstallDir")[0]
                if p and os.path.isfile(os.path.join(p, "libvlc.dll")):
                    return p
            except OSError:
                pass
    except ImportError:
        pass
    cands = []
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(var):
            cands.append(os.path.join(os.environ[var], "VideoLAN", "VLC"))
    w = shutil.which("vlc") or shutil.which("vlc.exe")
    if w:
        cands.append(os.path.dirname(w))
    for c in cands:
        if os.path.isfile(os.path.join(c, "libvlc.dll")):
            return c
    return None


def setup_vlc_windows():
    """Point the bundled python-vlc at libvlc.dll before it gets imported."""
    d = find_vlc_dir()
    if not d:
        return
    os.environ.setdefault("PYTHON_VLC_LIB_PATH", os.path.join(d, "libvlc.dll"))
    plugins = os.path.join(d, "plugins")
    if os.path.isdir(plugins):
        os.environ.setdefault("PYTHON_VLC_MODULE_PATH", plugins)
    try:
        _VLC_DLL_DIRS.append(os.add_dll_directory(d))
    except (AttributeError, OSError):
        pass


def load_vlc_binding():
    """Load the bundled python-vlc from vendor/vlc.py, so the binding stays one plain file
    that can be replaced on its own (LGPL) in a packaged build too. Without that file the
    ordinary `import vlc` is left to do its work."""
    for d in VENDOR_DIRS:
        path = os.path.join(d, "vlc.py")
        if os.path.isfile(path):
            break
    else:
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("vlc", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vlc"] = module
    spec.loader.exec_module(module)


def main():
    setup_vlc_windows()
    load_vlc_binding()
    startup.mark("libvlc.dll located, python-vlc bound")

    from PyQt5.QtCore import Qt
    from PyQt5.QtNetwork import QLocalServer, QLocalSocket
    from PyQt5.QtWidgets import QApplication

    from konus import APP_ID, APP_NAME
    startup.mark("PyQt imported")

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    # argv[0] is the app id, and the arguments after it are ours: Qt takes its own switches out
    # of the list it is given, while sys.argv stays as the caller passed it, for the files below.
    app = QApplication([APP_ID] + sys.argv[1:])
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    from konus.store import Store
    store = Store()
    startup.mark("settings loaded")

    args = sys.argv[1:]
    uid = os.environ.get("USERNAME") or "user"
    server_name = f"{APP_ID}-{uid}"
    single = store.get("single_window") and (store.get("single_from_fm") or not args)

    # Single instance: hand the arguments to a running window, if there is one.
    if single:
        sock = QLocalSocket()
        sock.connectToServer(server_name)
        if sock.waitForConnected(300):
            sock.write(json.dumps([os.path.abspath(a) if os.path.exists(a) else a for a in args]).encode())
            sock.waitForBytesWritten(1000)
            sock.disconnectFromServer()
            return 0

    from konus import i18n, theme
    i18n.set_language(store.get("language"))
    theme.load_fonts()
    from konus import ui
    app.setStyle(ui.Style("Fusion"))
    theme.apply(store.get("theme"), store.get("accent"))
    app.setFont(theme.font(14))

    if not store["language_chosen"]:
        # The first run asks which language the interface should speak *before* the interface is
        # built: its words enter the widgets as they are made (see konus.i18n), so this is the
        # one moment where the answer needs no rebuilding — the window simply comes up in it.
        from konus.language import ask
        ask(store)
        i18n.set_language(store.get("language"))

    from konus.engine import Engine
    from konus.engine import plugins_cache
    from konus.window import Shell

    # The startup marks (konus.startup) note whether libVLC was given a usable plugin list:
    # an unusable one means the engine walks all 360 plugins, which is the slow start this
    # program has had twice.
    startup.mark("plugin cache: " + (plugins_cache() or "none — libVLC will walk the plugins"))
    engine = Engine(store)
    startup.mark("libVLC engine up")
    # `shell.win` is the window on screen, and stays so across a language switch: the switch
    # builds the interface again (see konus.i18n), which means a new window — everything here
    # goes through the shell so it is talking to the current one (see konus.window.Shell).
    shell = Shell(engine, store)
    startup.mark("window built")

    server = None
    if store.get("single_window"):
        server = QLocalServer()
        QLocalServer.removeServer(server_name)
        server.listen(server_name)

        def incoming():
            while server.hasPendingConnections():
                conn = server.nextPendingConnection()
                buf = bytearray()

                def on_ready(c=conn, b=buf):
                    b.extend(bytes(c.readAll()))

                def on_done(c=conn, b=buf):
                    b.extend(bytes(c.readAll()))
                    try:
                        items = json.loads(b.decode() or "[]")
                    except ValueError:
                        items = []
                    shell.win.handle_args(items)
                    c.deleteLater()

                conn.readyRead.connect(on_ready)
                conn.disconnected.connect(on_done)
                if conn.state() == QLocalSocket.UnconnectedState:
                    on_done()

        server.newConnection.connect(incoming)

    if store.get("start_in_tray") and shell.win.tray is not None and shell.win.tray.isVisible():
        pass
    else:
        # With KONUS_STARTUP_LOG this marks the first painted frame; with KONUS_STARTUP_EXIT it
        # closes the window right after it (the window, not the app: the tray would keep the
        # process alive otherwise, see konus.startup). The watcher goes in before show(), or the
        # first paint can slip past it.
        startup.watch_window(shell.win, lambda: shell.win.close())
        shell.win.show()
    if not store["onboarded"]:
        from konus.onboarding import Onboarding
        Onboarding(shell.win).exec_()
        store.save()
    if args:
        shell.win.handle_args(args)
    code = app.exec_()
    if server is not None:
        server.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
