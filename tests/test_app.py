import json
import socket
import subprocess
import sys
import threading
import winreg
from pathlib import Path

import pytest

from govee_win11 import app

TEST_KEY = r"Software\GoveeWin11Test"
ROOT = Path(__file__).resolve().parent.parent


def _delete_test_key():
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
    except FileNotFoundError:
        pass


@pytest.fixture
def test_key():
    _delete_test_key()
    yield TEST_KEY
    _delete_test_key()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- autostart --------------------------------------------------------------

def test_autostart_roundtrip(test_key):
    assert app.is_autostart(test_key) is False
    app.set_autostart(True, test_key)
    assert app.is_autostart(test_key) is True
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, test_key) as k:
        value, kind = winreg.QueryValueEx(k, "GoveeWin11")
    assert kind == winreg.REG_SZ
    assert value == app.autostart_command()
    app.set_autostart(False, test_key)
    assert app.is_autostart(test_key) is False
    app.set_autostart(False, test_key)  # idempotent


def test_run_key_constant():
    assert app.RUN_KEY == r"Software\Microsoft\Windows\CurrentVersion\Run"
    assert app.AUTOSTART_VALUE == "GoveeWin11"


def test_autostart_command_uses_pythonw_and_tray():
    cmd = app.autostart_command()
    exe = app.pythonw_path()
    assert cmd.startswith(f'"{exe}" ')
    assert cmd.endswith(" --tray")
    assert Path(exe).is_absolute()
    if (Path(sys.executable).with_name("pythonw.exe")).exists():
        assert Path(exe).name.lower() == "pythonw.exe"
    # the target must not depend on the working directory (Run entries start in System32)
    target = cmd[len(f'"{exe}" '):-len(" --tray")].strip('"')
    assert Path(target).is_absolute() and Path(target).exists()


def test_entry_point_runs_from_any_cwd(tmp_path):
    """The autostart target works when launched outside the project directory."""
    target = app.autostart_command().split('" ')[1].rsplit(" --tray", 1)[0].strip('"')
    r = subprocess.run([sys.executable, target, "--help"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "--tray" in r.stdout


def test_parse_args():
    assert app.parse_args([]).tray is False
    assert app.parse_args(["--tray"]).tray is True


# ---- single instance --------------------------------------------------------

def test_second_instance_signals_first():
    port = _free_port()
    shown = threading.Event()
    first = app.SingleInstance(port, on_show=shown.set)
    try:
        assert first.acquire() is True
        second = app.SingleInstance(port)
        assert second.acquire() is False
        assert app.signal_existing(port) is True
        assert shown.wait(3)
    finally:
        first.close()
    # after the first instance exits the port is free again
    third = app.SingleInstance(port)
    try:
        assert third.acquire() is True
    finally:
        third.close()


def test_signal_without_instance_returns_false():
    assert app.signal_existing(_free_port()) is False


def test_single_instance_ignores_other_messages():
    port = _free_port()
    shown = threading.Event()
    first = app.SingleInstance(port, on_show=shown.set)
    try:
        assert first.acquire()
        with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
            c.sendall(b"hello")
        assert not shown.wait(0.5)
    finally:
        first.close()


def test_default_port():
    assert app.INSTANCE_PORT == 47321


# ---- paths / logging ----------------------------------------------------------

def test_paths_are_relative_to_package_not_cwd():
    assert app.SETTINGS_PATH == ROOT / "user_data" / "settings.json"
    assert app.LOG_PATH == ROOT / "user_data" / "govee.log"
    assert app.INDEX_HTML == ROOT / "govee_win11" / "web" / "index.html"
    assert app.INDEX_HTML.exists()


def test_setup_logging_writes_rotating_file(tmp_path):
    import logging
    from logging.handlers import RotatingFileHandler
    path = tmp_path / "logs" / "govee.log"
    root = logging.getLogger()
    before = list(root.handlers)
    handler = app.setup_logging(path)
    try:
        assert isinstance(handler, RotatingFileHandler)
        logging.getLogger("govee_win11.test").warning("hello 日志")
        handler.flush()
        assert "hello 日志" in path.read_text(encoding="utf-8")
    finally:
        root.removeHandler(handler)
        handler.close()
    assert root.handlers == before


# ---- state pusher -------------------------------------------------------------

def test_state_pusher_waits_for_load_and_sends_latest():
    calls = []
    done = threading.Event()

    def evaluate(js):
        calls.append(js)
        done.set()

    p = app.StatePusher(evaluate)
    p.start()
    try:
        p.submit({"a": 1})
        p.submit({"a": 2, "s": "中文"})
        assert not done.wait(0.3)  # window not loaded yet: nothing evaluated
        p.set_ready()
        assert done.wait(2)
        assert len(calls) == 1
        js = calls[0]
        prefix = "window.onState && window.onState("
        assert js.startswith(prefix) and js.endswith(")")
        assert json.loads(js[len(prefix):-1]) == {"a": 2, "s": "中文"}
    finally:
        p.close()


def test_state_pusher_survives_evaluate_errors_and_stops():
    seen = []
    second = threading.Event()

    def evaluate(js):
        seen.append(js)
        if len(seen) == 1:
            raise RuntimeError("js failed")
        second.set()

    p = app.StatePusher(evaluate)
    p.start()
    p.set_ready()
    p.submit({"n": 1})
    import time
    deadline = time.time() + 2
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    p.submit({"n": 2})
    assert second.wait(2)
    p.close()
    p.submit({"n": 3})
    time.sleep(0.2)
    assert len(seen) == 2


# ---- shell (window/tray behaviour) ----------------------------------------------

class FakeWindow:
    def __init__(self):
        self.calls = []

    def hide(self):
        self.calls.append("hide")

    def show(self):
        self.calls.append("show")

    def restore(self):
        self.calls.append("restore")

    def destroy(self):
        self.calls.append("destroy")


class FakeApi:
    def __init__(self, window=None):
        self.calls = []
        self.window = window
        self.timeouts = []
        self.window_calls_at_shutdown = None
        self.visible = []

    def set_window_visible(self, visible):
        self.visible.append(visible)

    def shutdown(self, timeout=None):
        self.calls.append("shutdown")
        self.timeouts.append(timeout)
        if self.window is not None:
            self.window_calls_at_shutdown = list(self.window.calls)


def test_shell_minimize_hides_and_show_restores():
    w = FakeWindow()
    shell = app.Shell(w, FakeApi())
    shell.on_minimized()
    assert w.calls == ["hide"]
    shell.show()
    assert w.calls == ["hide", "show", "restore"]
    shell.show()  # not minimised: just bring to front
    assert w.calls[-1] == "show"


def test_shell_closing_stops_everything_once():
    w, api, order = FakeWindow(), FakeApi(), []
    shell = app.Shell(w, api, stop_tray=lambda: order.append("tray"),
                      stop_pusher=lambda: order.append("pusher"))
    assert shell.on_closing() is not False  # never cancels the close
    assert api.calls == ["shutdown"]
    assert order == ["pusher", "tray"]
    shell.on_closing()
    assert api.calls == ["shutdown"]


def test_shell_quit_destroys_window():
    w = FakeWindow()
    shell = app.Shell(w, FakeApi())
    shell.quit()
    assert w.calls == ["destroy"]


def test_shell_show_swallows_window_errors():
    class Broken(FakeWindow):
        def show(self):
            raise RuntimeError("gone")
    app.Shell(Broken(), FakeApi()).show()


# ---- tray -------------------------------------------------------------------------

def test_tray_menu_labels_and_autostart_check():
    state = {"on": True}
    hits = []
    menu = app.build_tray_menu(on_show=lambda: hits.append("show"),
                               on_toggle_autostart=lambda: hits.append("auto"),
                               on_quit=lambda: hits.append("quit"),
                               is_autostart=lambda: state["on"])
    items = list(menu.items)
    assert [i.text for i in items] == ["显示主窗口", "开机自启", "退出"]
    assert items[0].default is True
    assert items[1].checked is True
    state["on"] = False
    assert items[1].checked is False


def test_tray_icon_image():
    img = app.make_tray_image()
    assert img.size == (64, 64)
    assert img.mode == "RGBA"
    assert img.getpixel((32, 32))[3] == 0      # ring: transparent centre
    assert img.getpixel((32, 6))[3] == 255     # opaque coloured rim


def test_shell_closing_hides_window_first_and_bounds_shutdown():
    w = FakeWindow()
    api = FakeApi(window=w)
    shell = app.Shell(w, api)
    shell.on_closing()
    assert api.window_calls_at_shutdown == ["hide"]  # hidden before the (blocking) shutdown
    assert api.timeouts == [app.SHUTDOWN_TIMEOUT]
    assert app.SHUTDOWN_TIMEOUT <= 3.0


def test_shell_closing_survives_hide_failure():
    class BadWindow(FakeWindow):
        def hide(self):
            raise RuntimeError("gone")

    api = FakeApi()
    app.Shell(BadWindow(), api).on_closing()
    assert api.calls == ["shutdown"]


def test_shell_drives_linger_visibility():
    w, api = FakeWindow(), FakeApi()
    shell = app.Shell(w, api)
    shell.on_minimized()
    shell.show()
    shell.on_visible()
    assert api.visible == [False, True, True]
    shell.on_closing()
    shell.on_visible()
    assert api.visible == [False, True, True]  # nothing after close


def test_shell_survives_visibility_error():
    class Bad(FakeApi):
        def set_window_visible(self, visible):
            raise RuntimeError("x")
    app.Shell(FakeWindow(), Bad()).on_minimized()


def test_shell_ignores_shown_event_while_hidden():
    api = FakeApi()
    shell = app.Shell(FakeWindow(), api, start_hidden=True)
    shell.on_visible()                      # spurious pywebview shown at --tray startup
    assert api.visible == []
    shell.show()
    assert api.visible == [True]
    shell.on_minimized()
    shell.on_visible()
    assert api.visible == [True, False]


def test_shell_closing_runs_before_close_hook_before_shutdown():
    w, order = FakeWindow(), []

    class OrderApi(FakeApi):
        def shutdown(self, timeout=None):
            order.append("shutdown")

    shell = app.Shell(w, OrderApi(), stop_pusher=lambda: order.append("pusher"),
                      before_close=lambda: order.append("power-off"))
    shell.on_closing()
    assert order == ["pusher", "power-off", "shutdown"]
    shell.on_closing()
    assert order == ["pusher", "power-off", "shutdown"]


def test_shell_closing_survives_before_close_error():
    api = FakeApi()

    def boom():
        raise RuntimeError("x")

    app.Shell(FakeWindow(), api, before_close=boom).on_closing()
    assert api.calls == ["shutdown"]


# ---- review fix round 1: the whole shutdown close path fits the power bound -------------

def test_shell_closing_fits_api_shutdown_into_time_left():
    api = FakeApi()
    app.Shell(FakeWindow(), api, before_close=lambda: 1.25).on_closing()
    assert api.timeouts == [1.25]
    api = FakeApi()
    app.Shell(FakeWindow(), api, before_close=lambda: 60.0).on_closing()
    assert api.timeouts == [app.SHUTDOWN_TIMEOUT]
    api = FakeApi()
    app.Shell(FakeWindow(), api, before_close=lambda: None).on_closing()   # normal close
    assert api.timeouts == [app.SHUTDOWN_TIMEOUT]
    api = FakeApi()
    app.Shell(FakeWindow(), api, before_close=lambda: 0.0).on_closing()    # bound used up
    assert api.calls == []


def test_shell_closing_during_shutdown_with_hanging_strip_stays_within_bound():
    import time as _time
    from govee_win11.power import PowerSync

    class HangingActions:
        def power_sync_enabled(self):
            return True

        def power_off_keep_mode(self, timeout):
            _time.sleep(5)                # strip unreachable: BLE connect hangs
            return False

        def power_on_keep_mode(self, timeout):
            return False

        def restore_last_mode(self):
            return False

    class SlowApi(FakeApi):
        def shutdown(self, timeout=None):
            super().shutdown(timeout)
            _time.sleep(timeout)          # engine.stop blocked on the engine lock

    bound = 0.4
    ps = PowerSync(HangingActions(), shutting_down=lambda: True, shutdown_bound=bound)
    t0 = _time.monotonic()
    app.Shell(FakeWindow(), SlowApi(), before_close=ps.on_app_closing).on_closing()
    assert _time.monotonic() - t0 < bound + 0.3   # was bound + 3 s (api.shutdown)


def test_no_hard_coded_device_address():
    from govee_win11 import probe
    assert not hasattr(app, "DEVICE_ADDRESS")
    assert not hasattr(probe, "DEFAULT_ADDRESS")


def _write_test_value(key, value):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, app.AUTOSTART_VALUE, 0, winreg.REG_SZ, value)


def test_foreign_run_entry_counts_as_off_and_switching_on_overwrites_it(test_key):
    foreign = r'"C:\Old\pythonw.exe" "C:\Old\govee_win11\__main__.py" --tray'
    _write_test_value(test_key, foreign)
    assert app.autostart_value(test_key) == foreign
    assert app.is_autostart(test_key) is False      # another copy's entry: not "on"
    assert app.autostart_value(test_key) == foreign  # reading never rewrites it
    app.set_autostart(True, test_key)
    assert app.autostart_value(test_key) == app.autostart_command()
    assert app.is_autostart(test_key) is True


def test_run_entry_match_ignores_case_and_quotes(test_key):
    cmd = app.autostart_command()
    _write_test_value(test_key, "  " + cmd.upper().replace('"', "") + " ")
    assert app.is_autostart(test_key) is True


def test_frozen_entry_is_not_this_source_copy(test_key, monkeypatch):
    _write_test_value(test_key, app.autostart_command(frozen=True,
                                                      executable=r"C:\Apps\GoveeWin11.exe"))
    assert app.is_autostart(test_key) is False
    monkeypatch.setattr(app, "FROZEN", True)
    monkeypatch.setattr(app.sys, "executable", r"C:\Apps\GoveeWin11.exe")
    assert app.is_autostart(test_key) is True
