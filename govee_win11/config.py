"""Settings storage (JSON, atomic write, corrupt-file backup)."""
from __future__ import annotations

import dataclasses
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_palettes() -> dict[str, list[str]]:
    return {"默认": ["#ff4d4d", "#ffd43d", "#3dd6c6", "#7a5cff"]}


def _default_screen() -> dict:
    # keep equal to screen.DEFAULT_SETTINGS (tested; not imported to keep config light)
    return {"monitor": 1, "saturation": 2.0, "transition_s": 1.5,
            "gain": [1.0, 1.0, 1.0], "mode": "dominant", "tempo": 50}


SCREEN_MODES = ("dominant", "fade", "projection", "region")
# Screen defaults shipped before Task 16 -> the Task 16 defaults; in a pre-Task-16 file
# (no "tempo" key yet) a stored value still equal to the old one is upgraded on load.
# Files written since have tempo, so the upgrade runs once: a user who later picks
# 1.3 / 0.6 keeps it.
_OLD_SCREEN_DEFAULTS = {"saturation": (1.3, 2.0), "smoothing": (0.6, 0.55)}
TRANSITION_RANGE = (0.3, 5.0)  # screen.TRANSITION_MIN / MAX


def smoothing_to_transition(smoothing) -> float:
    """Old 平滑度 (0-1) -> 过渡时间 (s): its time constant 0.15 * 10 ** s (s clamped to
    0-1) x 3 (95 % time), clamped to TRANSITION_RANGE, to the slider's 0.1 s step.
    Junk -> the default 1.5 s."""
    if isinstance(smoothing, bool):
        return _default_screen()["transition_s"]
    try:
        x = float(smoothing)
    except (TypeError, ValueError):
        return _default_screen()["transition_s"]
    if math.isnan(x):
        return _default_screen()["transition_s"]
    x = min(1.0, max(0.0, x))
    t = 3.0 * 0.15 * 10.0 ** x
    lo, hi = TRANSITION_RANGE
    t = min(hi, max(lo, t))
    return math.floor(t * 10.0 + 0.5 + 1e-9) / 10.0


def _migrate_screen(data: dict) -> None:
    sc = data.get("screen")
    if not isinstance(sc, dict):
        return
    if "tempo" not in sc:
        for key, (old, new) in _OLD_SCREEN_DEFAULTS.items():
            if sc.get(key) == old:
                sc[key] = new
    if "transition_s" not in sc and "smoothing" in sc:
        sc["transition_s"] = smoothing_to_transition(sc.pop("smoothing"))
    if sc.get("mode") not in SCREEN_MODES:
        sc["mode"] = "dominant"
    for key, v in _default_screen().items():
        sc.setdefault(key, v)


TRANSITION_RATES = (3, 5, 8, 10)          # 每段每秒刷新次数 options
TRANSITION_MIXES = ("linear", "perceptual")  # 混合方式: 光强 / 人眼 (effects.MIX_MODES)


def default_transition() -> dict:
    return {"rate": 5, "mix": "linear"}


def _sanitise_transition(data: dict) -> None:
    """Invalid / missing 过渡方式 values fall back to the defaults (unknown keys dropped)."""
    if "transition" not in data:
        return
    tr = data.get("transition")
    out = default_transition()
    if isinstance(tr, dict):
        rate = tr.get("rate")
        if not isinstance(rate, bool) and rate in TRANSITION_RATES:
            out["rate"] = int(rate)
        if tr.get("mix") in TRANSITION_MIXES:
            out["mix"] = tr["mix"]
    data["transition"] = out


def default_color_cal() -> dict:
    """颜色校准（白平衡）: per-channel gain in percent; 100/100/100 = no change."""
    return {"r": 100, "g": 100, "b": 100}


def clamp_color_cal(cal) -> dict:
    """Any stored value -> ``{"r", "g", "b"}`` ints clamped to 0..100; a missing or
    non-numeric channel (or a non-dict) loads as the default 100."""
    out = default_color_cal()
    if not isinstance(cal, dict):
        return out
    for k in out:
        v = cal.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
            continue
        out[k] = int(min(100, max(0, round(v))))
    return out


LANGUAGES = ("auto", "zh", "en")  # i18n.SETTINGS


def normalise_language(value) -> str:
    """Stored language setting -> "auto" | "zh" | "en" (anything invalid -> "auto")."""
    return value if isinstance(value, str) and value in LANGUAGES else "auto"


def _sanitise_device(data: dict) -> None:
    """A stored device address that is not ``XX:XX:XX:XX:XX:XX`` -> None (discovery)."""
    from .discovery import normalise_address
    if "device_address" in data:
        data["device_address"] = normalise_address(data["device_address"])
    if data.get("device_address") is None or not isinstance(data.get("device_name"), str):
        data["device_name"] = None


def _default_diy_presets() -> dict[str, dict]:
    """Built-in example DIY presets (DIYs made in the Govee app, recreated here)."""
    return {
        "orange blur": {"effects": ["fade3"], "colors": ["#ff5000", "#f20d0d"],
                        "speed_pct": 40},
        "yellow blue": {"effects": ["fade3"], "colors": ["#ffe000", "#3ecfe0"],
                        "speed_pct": 40},
        "pink blue": {"effects": ["rainbow1", "rainbow2"],
                      "colors": ["#f040b0", "#0a78c8", "#0a10b0", "#c0084f"], "speed_pct": 32},
        "pink yellow": {"effects": ["rainbow1", "rainbow2"],
                        "colors": ["#ff8a00", "#8a10c0", "#50087e", "#d86a00"], "speed_pct": 53},
    }


# Speeds the built-ins shipped with before the on-device speed scale; a stored built-in that
# still equals its old default exactly is upgraded on load (user-edited ones are left alone).
_OLD_BUILTIN_SPEEDS = {"orange blur": 50, "yellow blue": 50, "pink blue": 45, "pink yellow": 75}


def _migrate_presets(presets) -> None:
    if not isinstance(presets, dict):
        return
    new = _default_diy_presets()
    for name, old_speed in _OLD_BUILTIN_SPEEDS.items():
        p = presets.get(name)
        if not isinstance(p, dict) or p.get("speed_pct") != old_speed:
            continue
        d = new[name]
        if p == {**d, "speed_pct": old_speed}:
            p["speed_pct"] = d["speed_pct"]


@dataclass
class Settings:
    brightness: int = 80
    palettes: dict[str, list[str]] = field(default_factory=_default_palettes)
    active_palette: str = "默认"
    last_mode: dict | None = None
    screen: dict = field(default_factory=_default_screen)
    music: dict = field(default_factory=lambda: {"sensitivity": 1.0})
    calibration: list[list[float]] | None = None
    autostart: bool = False
    segment_colors: list[str] | None = None
    diy_presets: dict[str, dict] = field(default_factory=_default_diy_presets)
    power_sync: bool = True  # strip off at shutdown / sleep, on at login / resume
    transition: dict = field(default_factory=default_transition)  # 过渡方式（实验）
    color_cal: dict = field(default_factory=default_color_cal)  # 颜色校准（白平衡）
    language: str = "auto"  # "auto" (Windows display language) | "zh" | "en"
    # the strip (设置 → 设备); None = find it by scanning (discovery.py) and save it
    device_address: str | None = None
    device_name: str | None = None


def load_settings(path: Path) -> Settings:
    path = Path(path)
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("settings root must be an object")
        names = {f.name for f in dataclasses.fields(Settings)}
        _migrate_presets(data.get("diy_presets"))
        _migrate_screen(data)
        _sanitise_transition(data)
        if "color_cal" in data:
            data["color_cal"] = clamp_color_cal(data["color_cal"])
        if "language" in data:
            data["language"] = normalise_language(data["language"])
        _sanitise_device(data)
        return Settings(**{k: v for k, v in data.items() if k in names})
    except (ValueError, TypeError):
        try:
            os.replace(path, path.with_name(path.name + ".bad"))
        except OSError:
            pass
        return Settings()


def save_settings(s: Settings, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(s), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
