"""Bilingual UI, Python side (Task 19): language setting, resolution, tray, Api text."""
import json
import sys

import pytest

from govee_win11 import app, i18n
from govee_win11.config import Settings, load_settings, save_settings

from test_api import loop, make_api  # noqa: F401  (fixture + helper)


# ---- tables ---------------------------------------------------------------------

def test_tables_have_identical_keys_and_no_empty_text():
    zh, en = i18n.MESSAGES["zh"], i18n.MESSAGES["en"]
    assert set(zh) == set(en)
    for lang, table in i18n.MESSAGES.items():
        for k, v in table.items():
            assert isinstance(v, str) and v.strip(), (lang, k)


def test_placeholders_match_between_languages():
    import string
    fields = lambda s: {f for _, f, _, _ in string.Formatter().parse(s) if f}  # noqa: E731
    for k, zh in i18n.MESSAGES["zh"].items():
        assert fields(zh) == fields(i18n.MESSAGES["en"][k]), k


def test_tr_formats_and_falls_back():
    assert i18n.tr("en", "err.no_scene", id=7) == "No such scene: 7"
    assert i18n.tr("zh", "err.no_scene", id=7) == "没有这个情景：7"
    assert i18n.tr("xx", "tray.quit") == "退出"          # unknown language -> Chinese
    assert i18n.tr("en", "no.such.key") == "no.such.key"


# ---- resolution ---------------------------------------------------------------------

@pytest.fixture
def real_detection(monkeypatch):
    """Undo conftest's pin: system_language -> the real detection."""
    monkeypatch.setattr(i18n, "system_language", i18n.detect_system_language)
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.mark.parametrize("langid,expected", [
    (0x0804, "zh"),   # zh-CN
    (0x0404, "zh"),   # zh-TW
    (0x0C04, "zh"),   # zh-HK
    (0x1004, "zh"),   # zh-SG
    (0x0409, "en"),   # en-US
    (0x0809, "en"),   # en-GB
    (0x0411, "en"),   # ja-JP: anything not Chinese -> English
    (0x0407, "en"),   # de-DE
])
def test_auto_follows_the_windows_ui_language(real_detection, monkeypatch, langid, expected):
    monkeypatch.setattr(i18n, "_ui_langid", lambda: langid)
    assert i18n.resolve("auto") == expected


def test_auto_falls_back_to_the_locale(real_detection, monkeypatch):
    def boom():
        raise OSError("no kernel32")
    monkeypatch.setattr(i18n, "_ui_langid", boom)
    monkeypatch.setattr(i18n, "_locale_name", lambda: "Chinese (Simplified)_China")
    assert i18n.resolve("auto") == "zh"
    monkeypatch.setattr(i18n, "_locale_name", lambda: "zh_CN")
    assert i18n.resolve("auto") == "zh"
    monkeypatch.setattr(i18n, "_locale_name", lambda: "English_United States")
    assert i18n.resolve("auto") == "en"
    monkeypatch.setattr(i18n, "_locale_name", lambda: "")
    assert i18n.resolve("auto") == "en"


def test_explicit_setting_wins_over_the_system(monkeypatch):
    monkeypatch.setattr(i18n, "system_language", lambda: "zh")
    assert i18n.resolve("en") == "en"
    monkeypatch.setattr(i18n, "system_language", lambda: "en")
    assert i18n.resolve("zh") == "zh"
    assert i18n.resolve("auto") == "en"


@pytest.mark.parametrize("bad", ["fr", "", None, 1, True, ["en"], "EN", "zh-CN"])
def test_invalid_setting_counts_as_auto(monkeypatch, bad):
    monkeypatch.setattr(i18n, "system_language", lambda: "en")
    assert i18n.normalise_setting(bad) == "auto"
    assert i18n.resolve(bad) == "en"


# ---- settings ---------------------------------------------------------------------

def test_language_setting_defaults_to_auto_and_round_trips(tmp_path):
    p = tmp_path / "settings.json"
    assert load_settings(p).language == "auto"
    save_settings(Settings(language="en"), p)
    assert load_settings(p).language == "en"


@pytest.mark.parametrize("bad", ["fr", 3, None, {"x": 1}])
def test_invalid_stored_language_loads_as_auto(tmp_path, bad):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"brightness": 33, "language": bad}), encoding="utf-8")
    s = load_settings(p)
    assert s.language == "auto"
    assert s.brightness == 33           # the rest of the file is kept


# ---- tray -------------------------------------------------------------------------

def _menu(lang):
    return app.build_tray_menu(on_show=lambda: None, on_toggle_autostart=lambda: None,
                               on_quit=lambda: None, is_autostart=lambda: False,
                               language=lambda: lang["v"])


def test_tray_menu_labels_per_language():
    lang = {"v": "zh"}
    items = list(_menu(lang).items)
    assert [i.text for i in items] == ["显示主窗口", "开机自启", "退出"]
    lang["v"] = "en"                    # switched in 设置: the same menu re-reads
    assert [i.text for i in items] == ["Show window", "Start with Windows", "Quit"]


def test_tray_menu_defaults_to_chinese():
    menu = app.build_tray_menu(on_show=lambda: None, on_toggle_autostart=lambda: None,
                               on_quit=lambda: None, is_autostart=lambda: False)
    assert [i.text for i in menu.items][0] == "显示主窗口"


def test_window_title_per_language():
    assert i18n.tr("zh", "app.title") == app.WINDOW_TITLE
    assert i18n.tr("en", "app.title") == "Govee Control"


def test_power_window_block_reason_follows_language():
    from govee_win11 import power
    lang = {"v": "zh"}
    w = power.PowerWindow(object(), block_reason=lambda: i18n.tr(lang["v"],
                                                                 "power.block_reason"))
    assert w._reason_text() == power.BLOCK_REASON
    lang["v"] = "en"
    assert w._reason_text() == "Turning the light strip off…"
    assert power.PowerWindow(object())._reason_text() == power.BLOCK_REASON


# ---- Api ----------------------------------------------------------------------------

def test_get_language_reports_setting_and_resolved(loop, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(i18n, "system_language", lambda: "en")
    api, _, _ = make_api(loop, tmp_path)
    assert api.get_language() == {"setting": "auto", "resolved": "en"}
    assert api.get_state()["language"] == {"setting": "auto", "resolved": "en"}
    api2, _, _ = make_api(loop, tmp_path / "b", settings=Settings(language="zh"))
    assert api2.get_language() == {"setting": "zh", "resolved": "zh"}


def test_set_language_saves_notifies_and_pushes(loop, tmp_path):  # noqa: F811
    pushed, heard = [], []
    api, _, path = make_api(loop, tmp_path, push=pushed.append)
    api.add_language_listener(heard.append)
    r = api.set_language("en")
    assert r == {"ok": True, "setting": "en", "resolved": "en"}
    assert load_settings(path).language == "en"
    assert heard == ["en"]
    assert pushed[-1]["language"] == {"setting": "en", "resolved": "en"}
    assert api.language() == "en"
    r = api.set_language("auto")       # conftest: the system is Chinese
    assert r["resolved"] == "zh" and heard == ["en", "zh"]


@pytest.mark.parametrize("bad", ["fr", None, 1, ""])
def test_set_language_rejects_invalid(loop, tmp_path, bad):  # noqa: F811
    api, _, path = make_api(loop, tmp_path)
    r = api.set_language(bad)
    assert r["ok"] is False and "English" in r["error"]
    assert api.get_language()["setting"] == "auto"
    assert not path.exists()


def test_errors_follow_the_language(loop, tmp_path):  # noqa: F811
    api, _, _ = make_api(loop, tmp_path)
    assert api.start_effect("nope", 50)["error"] == "未知效果：nope"
    api.set_language("en")
    assert api.start_effect("nope", 50)["error"] == "Unknown effect: nope"
    assert api.set_brightness(500)["error"] == "Brightness must be between 0 and 100"
    assert api.apply_scene(999)["error"] == "No such scene: 999"


def test_link_messages_follow_the_language(loop, tmp_path):  # noqa: F811
    from govee_win11.link import BT_UNAVAILABLE_MESSAGE, BUSY_MESSAGE
    api, engine, _ = make_api(loop, tmp_path, settings=Settings(language="en"))
    engine.link.status = "busy_elsewhere"
    st = api.get_state()
    assert st["message"] == i18n.tr("en", "link.busy_elsewhere") != BUSY_MESSAGE
    engine.link.status = "bt_unavailable"
    r = api.start_effect("aurora", 50)
    assert r["ok"] is True and r["warning"] == i18n.tr("en", "link.bt_unavailable")
    assert r["warning"] != BT_UNAVAILABLE_MESSAGE
    engine.oneshot_result = False
    assert api.set_power(True)["error"] == i18n.tr("en", "link.bt_unavailable")
    engine.link.status = "disconnected"
    assert api.set_power(True)["error"] == i18n.tr("en", "err.send_failed")


def test_unexpected_exception_is_worded_in_the_language(loop, tmp_path, monkeypatch):  # noqa: F811
    api, _, _ = make_api(loop, tmp_path, settings=Settings(language="en"))

    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(api, "_oneshot", boom)
    assert api.set_power(True) == {"ok": False, "error": "Something went wrong: kaput"}
