"""Release packaging files (Task 20 Part C) are present and consistent."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _lines(name):
    return [ln.strip() for ln in (ROOT / name).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def test_runtime_requirements_have_no_dev_tools():
    req = [ln.lower() for ln in _lines("requirements.txt")]
    assert not any(ln.startswith(("pytest", "pyinstaller")) for ln in req)
    for pkg in ("bleak", "pywebview", "pystray", "pillow", "numpy", "pyaudiowpatch"):
        assert any(ln.startswith(pkg) for ln in req), pkg


def test_dev_requirements():
    dev = [ln.lower() for ln in _lines("requirements-dev.txt")]
    assert "-r requirements.txt" in dev
    for pkg in ("pytest", "pytest-asyncio", "pyinstaller"):
        assert pkg in dev


def test_spec_is_one_dir_windowed_with_assets_and_icon():
    spec = (ROOT / "GoveeWin11.spec").read_text(encoding="utf-8")
    assert 'name="GoveeWin11"' in spec and "console=False" in spec
    assert "exclude_binaries=True" in spec and "COLLECT(" in spec  # one-dir
    assert '("govee_win11/web", "govee_win11/web")' in spec
    assert '("govee_win11/data/*.json", "govee_win11/data")' in spec
    assert 'icon="assets/GoveeWin11.ico"' in spec
    assert (ROOT / "assets" / "GoveeWin11.ico").is_file()


def test_gitignore_ignores_build_output_not_the_spec():
    ignore = _lines(".gitignore")
    assert "build/" in ignore and "dist/" in ignore
    assert not any("spec" in ln for ln in ignore)


def test_build_script_runs_the_selftest_and_zips():
    ps = (ROOT / "build.ps1").read_text(encoding="utf-8")
    assert "GoveeWin11.spec" in ps and "--selftest" in ps and "selftest.txt" in ps
    assert "GoveeWin11-v$version-win64.zip" in ps


def test_readmes_link_each_other_and_stay_impersonal():
    import re
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    assert "(README.zh-CN.md)" in en.split("\n\n")[1]
    assert "(README.md)" in zh.split("\n\n")[1]
    for text in (en, zh):
        assert "C:" + chr(92) + "Users" not in text
        assert not re.search(r"\b[0-9A-F]{2}(:[0-9A-F]{2}){5}\b", text)
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
        assert "docs/reference/govee-diy-format.md" in text
        assert "MIT" in text
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert lic.startswith("MIT License") and "Copyright (c) 2026 nvmmonkey" in lic
