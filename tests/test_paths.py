"""Frozen-app correctness (Task 20 Part B): paths, autostart command, version, --selftest.
Nothing here writes the real Run key or the developer's user_data/."""
import subprocess
import sys
import winreg
from pathlib import Path

import govee_win11
from govee_win11 import app, paths, selftest

ROOT = Path(__file__).resolve().parent.parent
TEST_KEY = r"Software\GoveeWin11TestFrozen"


def test_version():
    assert govee_win11.__version__ == "1.0.0"


def test_source_paths_keep_the_repo_user_data():
    assert paths.user_data_dir(frozen=False) == ROOT / "user_data"
    assert paths.package_dir(frozen=False) == ROOT / "govee_win11"
    assert paths.resource("web", "index.html").is_file()
    assert paths.resource("data", "device_profile.json").is_file()


def test_frozen_user_data_goes_to_appdata(tmp_path):
    assert paths.user_data_dir(frozen=True, environ={"APPDATA": str(tmp_path)}) == \
        tmp_path / "GoveeWin11"
    assert paths.user_data_dir(frozen=True, environ={}) == \
        Path.home() / "AppData" / "Roaming" / "GoveeWin11"


def test_frozen_resources_come_from_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths.is_frozen()
    assert paths.package_dir() == tmp_path / "govee_win11"
    assert paths.resource("web", "index.html") == tmp_path / "govee_win11" / "web" / "index.html"


def test_autostart_command_frozen_points_at_the_exe():
    exe = r"C:\Apps\GoveeWin11\GoveeWin11.exe"
    assert app.autostart_command(frozen=True, executable=exe) == f'"{exe}" --tray'


def test_autostart_command_from_source_is_pythonw_and_module():
    cmd = app.autostart_command(frozen=False)
    assert cmd.startswith(f'"{app.pythonw_path()}" ') and cmd.endswith('__main__.py" --tray')


def test_frozen_autostart_written_to_a_test_key(monkeypatch):
    def delete():
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
        except FileNotFoundError:
            pass
    delete()
    monkeypatch.setattr(app, "FROZEN", True)
    try:
        app.set_autostart(True, TEST_KEY)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as k:
            value, _ = winreg.QueryValueEx(k, app.AUTOSTART_VALUE)
        assert value == f'"{sys.executable}" --tray'
    finally:
        delete()


def test_selftest_flag_parses():
    assert app.parse_args(["--selftest"]).selftest is True
    assert app.parse_args([]).selftest is False


def test_selftest_passes_from_source(tmp_path):
    r = subprocess.run([sys.executable, "-m", "govee_win11", "--selftest"], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith(f"selftest ok {govee_win11.__version__}\n")


def test_selftest_failure_exit_code_and_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(selftest, "DEPENDENCIES", ("no_such_module_for_selftest",))
    monkeypatch.setattr(selftest, "APP_MODULES", ())
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "GoveeWin11.exe"))
    assert selftest.run() == 1
    assert capsys.readouterr().out.startswith("selftest failed: import no_such_module_for_selftest")
    assert (tmp_path / "selftest.txt").read_text(encoding="utf-8").startswith("selftest failed")


def test_selftest_ok_writes_the_file_when_frozen(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(selftest, "DEPENDENCIES", ("json",))
    monkeypatch.setattr(selftest, "PRESENT_ONLY", ())
    monkeypatch.setattr(selftest, "APP_MODULES", ("govee_win11.config",))
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "GoveeWin11.exe"))
    assert selftest.run() == 0
    assert (tmp_path / "selftest.txt").read_text(encoding="utf-8").startswith(
        "selftest ok 1.0.0\n")


def test_state_carries_the_version(tmp_path):
    import asyncio
    from govee_win11.api import Api
    from govee_win11.config import Settings
    from govee_win11.profile import DeviceProfile

    class Link:
        status = "disconnected"

        def set_linger(self, s):
            pass

    class Engine:
        link = Link()
        min_segment_interval = 0.2
        current = None

    loop = asyncio.new_event_loop()
    try:
        api = Api(Engine(), DeviceProfile(), Settings(), tmp_path / "settings.json", loop)
        s = api.get_state()
        assert s["version"] == "1.0.0" and s["data_dir"] == str(tmp_path)
    finally:
        loop.close()
