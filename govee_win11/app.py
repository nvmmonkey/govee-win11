"""Desktop shell: pywebview window, system tray, autostart, single instance.

Threads: pywebview owns the main thread; one daemon thread runs the asyncio loop that
owns DeviceLink + Engine; pystray runs detached in its own thread; the single-instance
listener, the state pusher (window.evaluate_js) and the power window (shutdown / sleep
messages, power.py) each have a small daemon thread.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import threading
import winreg
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

from . import paths

log = logging.getLogger(__name__)

FROZEN = paths.is_frozen()  # PyInstaller build (GoveeWin11.exe)
PACKAGE_DIR = paths.package_dir()  # web/ + data/ (sys._MEIPASS/govee_win11 when frozen)
USER_DATA = paths.user_data_dir()  # repo user_data/ from source; %APPDATA%\GoveeWin11 frozen
SETTINGS_PATH = USER_DATA / "settings.json"
LOG_PATH = USER_DATA / "govee.log"
INDEX_HTML = PACKAGE_DIR / "web" / "index.html"

WINDOW_TITLE = "Govee 控制台"  # i18n "app.title" in Chinese; the app uses its language's
WINDOW_SIZE = (1100, 720)
WINDOW_MIN_SIZE = (900, 600)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE = "GoveeWin11"
INSTANCE_PORT = 47321
SHOW_MESSAGE = b"show"
SHUTDOWN_TIMEOUT = 3.0  # total seconds the closing window waits for engine + link


# ---------------------------------------------------------------------------
# Autostart (HKCU\...\Run)
# ---------------------------------------------------------------------------

def pythonw_path() -> str:
    """Absolute path of pythonw.exe next to the running interpreter (no console)."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def autostart_command(frozen: bool | None = None, executable: str | None = None) -> str:
    """Frozen build: ``"<GoveeWin11.exe>" --tray``. From source:
    ``"<pythonw>" "<package>\\__main__.py" --tray``.

    Run entries start with an arbitrary working directory (System32), where
    ``-m govee_win11`` cannot find the package; __main__.py puts the project root on
    sys.path itself, so launching it by absolute path works from anywhere."""
    if FROZEN if frozen is None else frozen:
        return f'"{executable or sys.executable}" --tray'
    main_py = Path(__file__).resolve().parent / "__main__.py"
    return f'"{pythonw_path()}" "{main_py}" --tray'


def set_autostart(on: bool, key_path: str = RUN_KEY) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, AUTOSTART_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, AUTOSTART_VALUE)
            except FileNotFoundError:
                pass


def _normalise_command(cmd) -> str:
    """Compare Run commands loosely: case, quotes and surrounding blanks ignored."""
    return " ".join(str(cmd).replace('"', " ").split()).lower() if cmd is not None else ""


def autostart_value(key_path: str = RUN_KEY) -> str | None:
    """The stored ``GoveeWin11`` Run command (None = no entry). Read only."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_QUERY_VALUE) as k:
            value, _ = winreg.QueryValueEx(k, AUTOSTART_VALUE)
            return value if isinstance(value, str) else None
    except FileNotFoundError:
        return None


def is_autostart(key_path: str = RUN_KEY) -> bool:
    """True only when the Run entry starts *this* copy (``autostart_command()``). An
    entry left by another copy (source checkout, an older unzipped release) counts as
    off, so switching autostart on overwrites it. Never writes the key."""
    value = autostart_value(key_path)
    return value is not None and _normalise_command(value) == _normalise_command(
        autostart_command())


# ---------------------------------------------------------------------------
# Single instance (127.0.0.1:47321)
# ---------------------------------------------------------------------------

class SingleInstance:
    """Owns the instance port; a later instance connects and sends ``show``."""

    def __init__(self, port: int = INSTANCE_PORT, on_show: Callable[[], None] | None = None):
        self.port = port
        self.on_show = on_show
        self._sock: socket.socket | None = None

    def acquire(self) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # nobody may share the port (Windows)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", self.port))
            s.listen(4)
        except OSError:
            s.close()
            return False
        self._sock = s
        threading.Thread(target=self._serve, args=(s,), daemon=True,
                         name="single-instance").start()
        return True

    def _serve(self, s: socket.socket) -> None:
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return  # closed
            try:
                with conn:
                    conn.settimeout(2)
                    data = conn.recv(64)
            except OSError:
                continue
            if data.strip() == SHOW_MESSAGE and self.on_show is not None:
                try:
                    self.on_show()
                except Exception:
                    log.exception("show request failed")

    def close(self) -> None:
        s, self._sock = self._sock, None
        if s is not None:
            s.close()


def signal_existing(port: int = INSTANCE_PORT) -> bool:
    """Ask the running instance to show its window. False if nobody listens."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
            c.sendall(SHOW_MESSAGE)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(path: Path = LOG_PATH) -> logging.Handler:
    """Rotating log file (pythonw has no console). Returns the handler it added."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return handler


def _install_excepthooks() -> None:
    def hook(exc_type, exc, tb):
        log.critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            log.critical("uncaught exception in thread %s",
                         getattr(args.thread, "name", "?"),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook


def _fix_std_streams() -> None:
    """pythonw: sys.stdout/stderr are None; some libraries write to them anyway."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


# ---------------------------------------------------------------------------
# State pushes to the page
# ---------------------------------------------------------------------------

class StatePusher:
    """Delivers the latest state to ``window.onState`` from its own thread.

    ``submit`` never blocks (it is called from the asyncio loop); intermediate states
    are dropped. Nothing is evaluated before ``set_ready`` (window loaded)."""

    def __init__(self, evaluate: Callable[[str], Any]):
        self._evaluate = evaluate
        self._cond = threading.Condition()
        self._latest: dict | None = None
        self._ready = False
        self._closed = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="state-push")
        self._thread.start()

    def submit(self, state: dict) -> None:
        with self._cond:
            if self._closed:
                return
            self._latest = state
            self._cond.notify()

    def set_ready(self) -> None:
        with self._cond:
            self._ready = True
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._latest = None
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._closed and not (self._ready and self._latest is not None):
                    self._cond.wait()
                if self._closed:
                    return
                state, self._latest = self._latest, None
            try:
                payload = json.dumps(state, ensure_ascii=False)
                self._evaluate("window.onState && window.onState(" + payload + ")")
            except Exception:
                log.debug("state push failed", exc_info=True)


# ---------------------------------------------------------------------------
# Window / tray behaviour
# ---------------------------------------------------------------------------

class Shell:
    """Window + tray glue: minimise -> hide to tray, close -> stop everything."""

    def __init__(self, window, api, *, stop_tray: Callable[[], None] = lambda: None,
                 stop_pusher: Callable[[], None] = lambda: None,
                 before_close: Callable[[], float | None] = lambda: None,
                 start_hidden: bool = False):
        self._window = window
        self._api = api
        self._stop_tray = stop_tray
        self._stop_pusher = stop_pusher
        self._before_close = before_close
        self._hidden_minimized = False
        self._hidden = start_hidden  # in tray: ignore spurious shown/restored events
        self._closed = False
        self._lock = threading.Lock()

    def _visible(self, visible: bool) -> None:
        try:
            self._api.set_window_visible(visible)
        except Exception:
            log.exception("set_window_visible failed")

    def on_visible(self) -> None:
        """shown / restored window events."""
        if not self._closed and not self._hidden:
            self._visible(True)

    def on_minimized(self) -> None:
        self._hidden = True
        self._hidden_minimized = True
        self._visible(False)
        try:
            self._window.hide()
        except Exception:
            log.exception("hide failed")

    def show(self) -> None:
        try:
            self._window.show()
            self._hidden = False
            self._visible(True)
            if self._hidden_minimized:
                self._hidden_minimized = False
                self._window.restore()
        except Exception:
            log.exception("show failed")

    def on_closing(self):
        """Stop modes, disconnect, stop the tray. Never cancels the close (returns None).

        pywebview runs this synchronously on the GUI thread, so the window is hidden
        first (no frozen "未响应" window) and the shutdown gets one short total deadline
        (``SHUTDOWN_TIMEOUT``); the process exits right after anyway."""
        with self._lock:
            if self._closed:
                return None
            self._closed = True
        log.info("closing: stopping engine and link")
        try:
            self._window.hide()
        except Exception:
            log.exception("hide failed")
        self._stop_pusher()
        left = None
        try:
            # Windows is shutting down: strip off first. Returns the seconds left of the
            # shutdown bound (None = ordinary close) so the whole path stays inside it.
            left = self._before_close()
        except Exception:
            log.exception("before-close hook failed")
        timeout = SHUTDOWN_TIMEOUT
        if isinstance(left, (int, float)) and not isinstance(left, bool):
            timeout = min(SHUTDOWN_TIMEOUT, max(0.0, float(left)))
        if timeout > 0.05:
            try:
                self._api.shutdown(timeout=timeout)
            except Exception:
                log.exception("shutdown failed")
        else:
            log.warning("shutdown bound used up: skipping engine/link teardown")
        try:
            self._stop_tray()
        except Exception:
            log.exception("tray stop failed")
        return None

    def quit(self) -> None:
        """Tray 退出: closing the window runs on_closing, then the process exits."""
        try:
            self._window.destroy()
        except Exception:
            log.exception("destroy failed")


def make_tray_image(size: int = 64):
    """A ring of coloured arcs (an RGBIC strip glyph) on a transparent background."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    colors = [(255, 77, 77), (255, 170, 40), (255, 222, 60), (61, 214, 120),
              (61, 190, 230), (122, 92, 255)]
    pad = 2
    step = 360 / len(colors)
    for i, c in enumerate(colors):
        d.pieslice([pad, pad, size - 1 - pad, size - 1 - pad],
                   start=-90 + i * step, end=-90 + (i + 1) * step, fill=c + (255,))
    inner = size * 0.28
    d.ellipse([size / 2 - inner, size / 2 - inner, size / 2 + inner, size / 2 + inner],
              fill=(0, 0, 0, 0))
    return img


def build_tray_menu(*, on_show: Callable[[], None], on_toggle_autostart: Callable[[], None],
                    on_quit: Callable[[], None], is_autostart: Callable[[], bool],
                    language: Callable[[], str] = lambda: "zh"):
    """Tray menu; the item texts follow ``language()`` ("zh" | "en"), read whenever
    pystray builds the menu (``icon.update_menu()`` after a switch)."""
    import pystray

    from .i18n import tr

    def text(key):
        return lambda item: tr(language(), key)

    return pystray.Menu(
        pystray.MenuItem(text("tray.show"), lambda icon, item: on_show(), default=True),
        pystray.MenuItem(text("tray.autostart"), lambda icon, item: on_toggle_autostart(),
                         checked=lambda item: is_autostart()),
        pystray.MenuItem(text("tray.quit"), lambda icon, item: on_quit()),
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from .i18n import system_language, tr

    lang = system_language()  # settings are not loaded yet: the Windows language
    p = argparse.ArgumentParser(prog="govee_win11", description=tr(lang, "cli.description"))
    p.add_argument("--tray", action="store_true", help=tr(lang, "cli.tray"))
    p.add_argument("--selftest", action="store_true", help=tr(lang, "cli.selftest"))
    return p.parse_args(argv)


def main(start_hidden: bool = False) -> None:
    _fix_std_streams()
    setup_logging(LOG_PATH)
    _install_excepthooks()
    instance = SingleInstance(INSTANCE_PORT)
    if not instance.acquire():
        log.info("another instance is running; asking it to show its window")
        signal_existing(INSTANCE_PORT)
        return
    try:
        _run(instance, start_hidden)
    except Exception:
        log.exception("fatal error")
    finally:
        instance.close()
        log.info("exit")
        logging.shutdown()
        os._exit(0)  # pystray / loop / worker threads must not keep the process alive


def _run(instance: SingleInstance, start_hidden: bool) -> None:
    import pystray
    import webview

    from .api import Api
    from .config import load_settings
    from .discovery import scan
    from .i18n import tr
    from .engine import Engine
    from .link import DeviceLink, winrt_adapter_available
    from .power import PowerSync, PowerWindow
    from .profile import load_profile

    log.info("starting (hidden=%s)", start_hidden)
    profile = load_profile()
    settings = load_settings(SETTINGS_PATH)
    settings.autostart = is_autostart()  # the registry is the truth

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True, name="asyncio-loop").start()

    holder: dict[str, Api] = {}

    def on_status(status, message):
        log.info("link status: %s", status)
        api = holder.get("api")
        if api is not None:
            api.on_status(status, message)

    def on_frame(frame):
        api = holder.get("api")
        if api is not None:
            api.on_frame(frame)

    def on_device(chosen, others):
        log.info("auto-discovered %s (%s); %d other(s)", chosen.name, chosen.address,
                 len(others))
        api = holder.get("api")
        if api is not None:
            api.on_device_found(chosen, others)

    # settings.device_address None -> the link scans for an H6125 on its first connect
    link = DeviceLink(settings.device_address, on_status=on_status,
                      adapter_check=winrt_adapter_available, discover=scan,
                      on_device=on_device)
    engine = Engine(link, profile, on_frame=on_frame)

    window = None
    pusher = StatePusher(lambda js: window.evaluate_js(js))
    api = Api(engine, profile, settings, SETTINGS_PATH, loop, push=pusher.submit,
              set_autostart=lambda on: set_autostart(on), is_autostart=lambda: is_autostart(),
              discover=scan)
    holder["api"] = api

    window = webview.create_window(
        tr(api.language(), "app.title"), url=str(INDEX_HTML), js_api=api,
        width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=WINDOW_MIN_SIZE,
        hidden=start_hidden)

    icon = pystray.Icon("GoveeWin11", make_tray_image(), tr(api.language(), "app.title"))

    def stop_tray():
        icon.visible = False  # remove the notification-area icon now (no ghost icon)
        icon.stop()

    # strip follows shutdown / sleep / login (settings.power_sync); own hidden window
    power_sync = PowerSync(api)
    power_window = PowerWindow(power_sync,
                               block_reason=lambda: tr(api.language(), "power.block_reason"))
    if not power_window.start():
        log.warning("power window unavailable: shutdown/sleep will not switch the strip")

    shell = Shell(window, api, stop_tray=stop_tray, stop_pusher=pusher.close,
                  before_close=power_sync.on_app_closing, start_hidden=start_hidden)

    def toggle_autostart():
        r = api.set_autostart(not is_autostart())
        if not r.get("ok"):
            log.warning("autostart toggle failed: %s", r.get("error"))
        icon.update_menu()

    icon.menu = build_tray_menu(on_show=shell.show, on_toggle_autostart=toggle_autostart,
                                on_quit=shell.quit, is_autostart=is_autostart,
                                language=api.language)

    def on_language(lang):
        """Language switched in 设置: tray tooltip + menu, window title follow at once."""
        title = tr(lang, "app.title")
        try:
            icon.title = title
            icon.update_menu()
        except Exception:
            log.exception("tray language update failed")
        try:
            window.set_title(title)
        except Exception:
            log.exception("window title update failed")

    api.add_language_listener(on_language)

    window.events.loaded += pusher.set_ready
    window.events.minimized += shell.on_minimized
    window.events.shown += shell.on_visible
    window.events.restored += shell.on_visible
    window.events.closing += shell.on_closing
    instance.on_show = shell.show

    api.set_window_visible(not start_hidden)
    pusher.start()
    icon.run_detached()
    if start_hidden:
        # autostart at login: strip on (power sync), then the last mode
        threading.Thread(target=power_sync.on_tray_start, daemon=True, name="restore").start()

    webview.start(gui="edgechromium")

    shell.on_closing()  # no-op if the closing event already ran
    power_sync.wait_pending()  # a shutdown power-off still running (bounded)
    power_window.stop()
    loop.call_soon_threadsafe(loop.stop)
