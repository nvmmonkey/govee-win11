import json

import pytest

from govee_win11.config import Settings, load_settings, save_settings


def test_missing_file_gives_defaults(tmp_path):
    s = load_settings(tmp_path / "settings.json")
    assert s == Settings()
    assert s.brightness == 80
    assert s.palettes == {"默认": ["#ff4d4d", "#ffd43d", "#3dd6c6", "#7a5cff"]}
    assert s.screen["mode"] == "dominant"


def test_roundtrip(tmp_path):
    p = tmp_path / "sub" / "settings.json"
    s = Settings(brightness=42, active_palette="x", last_mode={"kind": "effect", "args": {"a": 1}},
                 calibration=[[0.1, 0.2], [0.3, 0.4]], autostart=True, segment_colors=["#000000"])
    s.palettes["x"] = ["#ffffff"]
    save_settings(s, p)
    assert load_settings(p) == s
    assert not p.with_name("settings.json.tmp").exists()


def test_corrupt_file_gives_defaults_and_keeps_backup(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_settings(p) == Settings()
    assert not p.exists()
    assert (tmp_path / "settings.json.bad").read_text(encoding="utf-8") == "{not json"


def test_unknown_keys_ignored(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"brightness": 10, "bogus": 1}), encoding="utf-8")
    s = load_settings(p)
    assert s.brightness == 10
    assert s.autostart is False


PRESETS = {
    "orange blur": {"effects": ["fade3"], "colors": ["#ff5000", "#f20d0d"], "speed_pct": 40},
    "yellow blue": {"effects": ["fade3"], "colors": ["#ffe000", "#3ecfe0"], "speed_pct": 40},
    "pink blue": {"effects": ["rainbow1", "rainbow2"],
                  "colors": ["#f040b0", "#0a78c8", "#0a10b0", "#c0084f"], "speed_pct": 32},
    "pink yellow": {"effects": ["rainbow1", "rainbow2"],
                    "colors": ["#ff8a00", "#8a10c0", "#50087e", "#d86a00"], "speed_pct": 53},
}


def test_default_diy_presets():
    s = Settings()
    assert s.diy_presets == PRESETS
    assert list(s.diy_presets) == ["orange blur", "yellow blue", "pink blue", "pink yellow"]
    s.diy_presets["orange blur"]["speed_pct"] = 1  # each instance owns its own copy
    assert Settings().diy_presets == PRESETS


def test_old_settings_file_gets_default_presets(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"brightness": 33, "palettes": {"a": ["#123456"]}}), encoding="utf-8")
    s = load_settings(p)
    assert s.brightness == 33
    assert s.diy_presets == PRESETS


def test_presets_roundtrip(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings()
    s.diy_presets["mine"] = {"effects": ["fade1", "future_effect"], "colors": ["#010203"],
                             "speed_pct": 10}
    del s.diy_presets["pink yellow"]
    save_settings(s, p)
    loaded = load_settings(p)
    assert loaded == s
    assert loaded.diy_presets["mine"]["effects"] == ["fade1", "future_effect"]  # kept as-is
    assert "pink yellow" not in loaded.diy_presets


OLD_SPEEDS = {"orange blur": 50, "yellow blue": 50, "pink blue": 45, "pink yellow": 75}


def test_old_builtin_speeds_are_upgraded_on_load(tmp_path):
    p = tmp_path / "settings.json"
    old = json.loads(json.dumps(PRESETS))
    for name, sp in OLD_SPEEDS.items():
        old[name]["speed_pct"] = sp
    p.write_text(json.dumps({"diy_presets": old}), encoding="utf-8")
    assert load_settings(p).diy_presets == PRESETS


def test_user_edited_builtins_are_left_alone(tmp_path):
    p = tmp_path / "settings.json"
    old = json.loads(json.dumps(PRESETS))
    for name, sp in OLD_SPEEDS.items():
        old[name]["speed_pct"] = sp
    old["orange blur"]["speed_pct"] = 61                 # speed edited
    old["yellow blue"]["colors"] = ["#ffe001", "#3ecfe0"]  # colours edited, old speed
    old["pink blue"]["speed_pct"] = 32                   # already new
    p.write_text(json.dumps({"diy_presets": old}), encoding="utf-8")
    got = load_settings(p).diy_presets
    assert got["orange blur"]["speed_pct"] == 61
    assert got["yellow blue"]["speed_pct"] == 50
    assert got["pink blue"]["speed_pct"] == 32
    assert got["pink yellow"]["speed_pct"] == 53


# ---- screen sync defaults (Task 16): old defaults upgraded, user values kept ----

def test_default_screen_matches_screen_module():
    from govee_win11.screen import DEFAULT_SETTINGS
    assert Settings().screen == DEFAULT_SETTINGS
    assert Settings().screen["saturation"] == 2.0 and Settings().screen["transition_s"] == 1.5
    assert "smoothing" not in Settings().screen
    from govee_win11 import config, screen
    assert config.TRANSITION_RANGE == (screen.TRANSITION_MIN, screen.TRANSITION_MAX)


def test_old_screen_defaults_are_upgraded(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"screen": {"monitor": 2, "saturation": 1.3, "smoothing": 0.6,
                                        "gain": [1.0, 0.9, 0.8], "mode": "dominant"}}),
                 encoding="utf-8")
    sc = load_settings(p).screen
    # 0.6 -> 0.55 (Task 16) -> old tau 0.53 s x 3 = 1.6 s
    assert sc["saturation"] == 2.0 and sc["transition_s"] == 1.6
    assert "smoothing" not in sc
    assert sc["monitor"] == 2 and sc["gain"] == [1.0, 0.9, 0.8]  # rest untouched
    assert sc["tempo"] == 50                                    # new key filled in


def test_user_screen_values_are_kept(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"screen": {"saturation": 1.7, "transition_s": 3.3, "mode": "region",
                                        "tempo": 80}}), encoding="utf-8")
    sc = load_settings(p).screen
    assert (sc["saturation"], sc["transition_s"], sc["mode"], sc["tempo"]) == (1.7, 3.3, "region", 80)


def test_unknown_screen_mode_falls_back_to_dominant(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"screen": {"mode": "bogus"}}), encoding="utf-8")
    assert load_settings(p).screen["mode"] == "dominant"


def test_screen_migration_runs_once(tmp_path):
    # a file written after Task 16 (has tempo): 1.3 / 0.6 are the user's own choice
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"screen": {"saturation": 1.3, "smoothing": 0.6, "tempo": 50,
                                        "mode": "fade"}}), encoding="utf-8")
    sc = load_settings(p).screen
    # smoothing 0.6 kept as the user's choice: tau 0.15 * 10 ** 0.6 = 0.597 s -> 1.8 s
    assert (sc["saturation"], sc["transition_s"], sc["mode"]) == (1.3, 1.8, "fade")
    # and a migrated file saved again keeps the user's later edits
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"screen": {"saturation": 1.3, "smoothing": 0.6}}), encoding="utf-8")
    s = load_settings(old)
    s.screen["saturation"] = 1.3
    s.screen["transition_s"] = 4.0
    save_settings(s, old)
    sc = load_settings(old).screen
    assert sc["saturation"] == 1.3 and sc["transition_s"] == 4.0


@pytest.mark.parametrize("smoothing,expected", [(0.0, 0.5), (1.0, 4.5), (0.3, 0.9),
                                                ("junk", 1.5), (None, 1.5), (5, 4.5)])
def test_smoothing_migrates_to_transition_time(tmp_path, smoothing, expected):
    # old tau = 0.15 * 10 ** smoothing (0..1), x 3 = 95 % time, clamped to 0.3..5 s
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"screen": {"smoothing": smoothing, "tempo": 50}}), encoding="utf-8")
    assert load_settings(p).screen["transition_s"] == expected


def test_transition_time_present_wins_over_smoothing(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"screen": {"smoothing": 1.0, "transition_s": 2.2, "tempo": 50}}),
                 encoding="utf-8")
    assert load_settings(p).screen["transition_s"] == 2.2


# ---- Task 17 ------------------------------------------------------------------------

def test_power_sync_defaults_on_and_roundtrips(tmp_path):
    assert Settings().power_sync is True
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"brightness": 50}), encoding="utf-8")  # pre-Task-17 file
    assert load_settings(p).power_sync is True
    s = Settings(power_sync=False)
    save_settings(s, p)
    assert load_settings(p).power_sync is False


def test_transition_defaults_and_sanitising(tmp_path):
    assert Settings().transition == {"rate": 5, "mix": "linear"}
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"transition": {"rate": 8, "mix": "perceptual"}}), encoding="utf-8")
    assert load_settings(p).transition == {"rate": 8, "mix": "perceptual"}
    p.write_text(json.dumps({"transition": {"rate": 7, "mix": "hsv", "x": 1}}), encoding="utf-8")
    assert load_settings(p).transition == {"rate": 5, "mix": "linear"}
    p.write_text(json.dumps({"transition": "junk"}), encoding="utf-8")
    assert load_settings(p).transition == {"rate": 5, "mix": "linear"}
    p.write_text(json.dumps({"transition": {"mix": "perceptual"}}), encoding="utf-8")
    assert load_settings(p).transition == {"rate": 5, "mix": "perceptual"}


# ---- Task 18: 颜色校准（白平衡） -------------------------------------------------------

def test_color_cal_default_and_roundtrip(tmp_path):
    assert Settings().color_cal == {"r": 100, "g": 100, "b": 100}
    p = tmp_path / "settings.json"
    save_settings(Settings(color_cal={"r": 90, "g": 75, "b": 100}), p)
    assert load_settings(p).color_cal == {"r": 90, "g": 75, "b": 100}


@pytest.mark.parametrize("stored,expected", [
    ({"r": 150, "g": -5, "b": 42.6}, {"r": 100, "g": 0, "b": 43}),
    ({"r": "80", "g": True, "b": None}, {"r": 100, "g": 100, "b": 100}),
    ({"g": 60}, {"r": 100, "g": 60, "b": 100}),
    ("junk", {"r": 100, "g": 100, "b": 100}),
    ({"r": float("nan"), "g": 50, "b": 50, "x": 1}, {"r": 100, "g": 50, "b": 50}),
])
def test_color_cal_clamped_on_load(tmp_path, stored, expected):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"color_cal": stored, "brightness": 33}), encoding="utf-8")
    s = load_settings(p)
    assert s.color_cal == expected
    assert s.brightness == 33  # the rest of the file still loads


def test_device_defaults_to_discovery(tmp_path):
    s = load_settings(tmp_path / "settings.json")
    assert s.device_address is None and s.device_name is None


def test_device_roundtrip_and_sanitising(tmp_path):
    p = tmp_path / "settings.json"
    save_settings(Settings(device_address="AA:BB:CC:DD:EE:FF", device_name="Govee_H6125_EEFF"), p)
    s = load_settings(p)
    assert (s.device_address, s.device_name) == ("AA:BB:CC:DD:EE:FF", "Govee_H6125_EEFF")
    p.write_text(json.dumps({"device_address": "aa-bb-cc-dd-ee-01"}), encoding="utf-8")
    s = load_settings(p)
    assert (s.device_address, s.device_name) == ("AA:BB:CC:DD:EE:01", None)
    p.write_text(json.dumps({"device_address": "garbage", "device_name": "x"}), encoding="utf-8")
    s = load_settings(p)
    assert (s.device_address, s.device_name) == (None, None)
