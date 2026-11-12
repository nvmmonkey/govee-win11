import asyncio
import json
import threading

import pytest

from govee_win11.api import Api
from govee_win11.config import Settings, load_settings
from govee_win11.effects import EffectMode
from govee_win11.link import BUSY_MESSAGE
from govee_win11.music import MusicMode
from govee_win11.profile import DeviceProfile
from govee_win11.protocol import brightness_packet, power_packet, segment_packet
from govee_win11.scenes import Scene, packets_for
from govee_win11.screen import ScreenSyncMode

N = 15


class FakeLink:
    def __init__(self):
        self.status = "disconnected"
        self.transient: list = []
        self.transient_result = True
        self.linger: list = []

    def set_linger(self, seconds):
        self.linger.append(seconds)

    async def send_transient(self, packets, key=None):
        self.transient.append((list(packets), key))
        return self.transient_result


class FakeEngine:
    def __init__(self):
        self.link = FakeLink()
        self.min_segment_interval = 0.2
        self.current = None
        self.calls: list = []
        self.oneshots: list = []
        self.oneshot_result = True
        self.paced_calls: list = []
        self.recolors = 0

    def recolor(self):
        self.recolors += 1

    async def run(self, mode):
        self.calls.append(("run", mode))
        self.current = mode

    async def stop(self):
        self.calls.append(("stop",))
        self.current = None

    async def oneshot(self, packets, key=None, paced=False):
        self.calls.append(("oneshot", key))
        self.current = None
        self.paced_calls.append(paced)
        self.paced = paced
        self.oneshots.append((list(packets), key))
        return self.oneshot_result


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    t = threading.Thread(target=lp.run_forever, daemon=True)
    t.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    t.join(2)
    lp.close()


SCENES = [
    Scene(id=1, name="极光", category="自然", hint="aurora", code=0x0102),
    Scene(id=2, name="日出", category="自然", hint="", code=0x0203),
    Scene(id=3, name="派对", category="生活", hint="", code=0x0304),
]


def make_api(loop, tmp_path, *, profile=None, settings=None, **kw):
    engine = FakeEngine()
    profile = profile or DeviceProfile(segment_count=N, brightness_max=100, scene_cmd_version=1)
    path = tmp_path / "settings.json"
    api = Api(engine, profile, settings or Settings(), path, loop,
              scenes=kw.pop("scenes", SCENES), **kw)
    return api, engine, path


# ---- plan tests -------------------------------------------------------------

def test_set_brightness_scales_to_profile(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    r = api.set_brightness(50)
    assert r["ok"] is True
    assert engine.link.transient[-1] == ([brightness_packet(50)], "brightness")
    assert load_settings(path).brightness == 50

    api255, engine255, _ = make_api(loop, tmp_path / "b",
                                    profile=DeviceProfile(segment_count=N, brightness_max=255))
    api255.set_brightness(50)
    assert engine255.link.transient[-1] == ([brightness_packet(128)], "brightness")


def test_set_brightness_keeps_running_mode(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    api.start_effect("aurora", 50)
    api.set_brightness(30)
    assert ("stop",) not in engine.calls
    assert not any(c[0] == "oneshot" for c in engine.calls)
    assert engine.current is not None


def test_start_effect_records_last_mode(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    r = api.start_effect("aurora", 50)
    assert r["ok"] is True
    kind, mode = engine.calls[-1]
    assert kind == "run" and isinstance(mode, EffectMode)
    assert mode.effect == "aurora"
    assert mode.speed == pytest.approx(1.0)
    assert mode.n == N
    # default colours = active palette
    assert mode.palette == [(0xff, 0x4d, 0x4d), (0xff, 0xd4, 0x3d), (0x3d, 0xd6, 0xc6), (0x7a, 0x5c, 0xff)]
    expected = {"kind": "effect",
                "args": {"effect": "aurora", "speed_pct": 50, "colors": None, "width": None}}
    assert api.get_state()["settings"]["last_mode"] == expected
    assert load_settings(path).last_mode == expected


def test_start_effect_with_colors_and_width(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    r = api.start_effect("aurora", 100, ["#FF0000", "#0000ff"], 4)
    assert r["ok"] is True
    mode = engine.current
    assert mode.palette == [(255, 0, 0), (0, 0, 255)]
    assert mode.speed == pytest.approx(4.0)
    assert mode.width == 4


def test_start_effect_rejects_unknown_effect(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    r = api.start_effect("nope", 50)
    assert r["ok"] is False and "效果" in r["error"]
    assert engine.calls == []


def test_stop_mode_clears_last_mode(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.start_effect("aurora", 50)
    r = api.stop_mode()
    assert r["ok"] is True
    assert engine.calls[-1] == ("stop",)
    assert load_settings(path).last_mode is None
    assert api.get_state()["mode"] is None


def test_errors_return_chinese_message(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    engine.oneshot_result = False
    engine.link.status = "busy_elsewhere"
    r = api.set_segments(["#ff0000"] * N)
    assert r == {"ok": False, "error": BUSY_MESSAGE}
    assert BUSY_MESSAGE == "连不上灯带：可能被手机占用，请关闭手机蓝牙或 Govee App"
    r = api.set_power(True)
    assert r == {"ok": False, "error": BUSY_MESSAGE}
    engine.link.transient_result = False
    r = api.set_brightness(40)
    assert r == {"ok": False, "error": BUSY_MESSAGE}


def test_get_state_is_json_serializable(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    engine.current = EffectMode(["rainbow1", "rainbow2"], [(255, 0, 0), (0, 0, 255)], 1.0, N)
    api.on_frame([(1, 2, 3)] * N)
    state = api.get_state()
    text = json.dumps(state, ensure_ascii=False)
    back = json.loads(text)
    for key in ("status", "power", "brightness", "mode", "segment_count", "palettes",
                "active_palette", "settings", "frame"):
        assert key in back
    assert back["segment_count"] == N
    assert back["mode"]["kind"] == "effect"
    assert back["mode"]["args"]["effect"] == ["rainbow1", "rainbow2"]
    assert back["frame"] == ["#010203"] * N


# ---- segments / palettes ------------------------------------------------------

def test_set_segments_groups_colours_and_persists(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    colors = ["#ff0000"] * 5 + ["#00FF00"] * 10
    r = api.set_segments(colors)
    assert r["ok"] is True
    packets, key = engine.oneshots[-1]
    assert key == "segments"
    assert packets == [segment_packet((255, 0, 0), 0b11111),
                       segment_packet((0, 255, 0), 0b111111111100000)]
    assert load_settings(path).segment_colors == ["#ff0000"] * 5 + ["#00ff00"] * 10
    assert api.get_state()["frame"][0] == "#ff0000"


def test_set_segments_tiles_short_lists_and_rejects_bad_colours(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    assert api.set_segments(["#ff0000", "#0000ff"])["ok"] is True
    assert api.get_state()["settings"]["segment_colors"][:4] == ["#ff0000", "#0000ff"] * 2
    bad = api.set_segments(["red"])
    assert bad["ok"] is False and "颜色" in bad["error"]
    assert api.set_segments([])["ok"] is False


def test_start_then_static_command_clears_last_mode(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.start_effect("flow1", 50)
    api.fill("#123456")
    assert load_settings(path).last_mode is None
    assert engine.oneshots[-1][0] == [segment_packet((0x12, 0x34, 0x56), (1 << N) - 1)]


def test_set_power_sends_packet_and_tracks_state(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    assert api.get_state()["power"] is None
    assert api.set_power(False)["ok"] is True
    assert engine.oneshots[-1] == ([power_packet(False)], "power")
    assert api.get_state()["power"] is False


def test_palette_crud(loop, tmp_path):
    api, _, path = make_api(loop, tmp_path)
    r = api.generate_palette("vivid", 200, "calm", "analogous", 4)
    assert r["ok"] is True and len(r["colors"]) == 4
    assert api.generate_palette("bogus", 0, "calm", "analogous", 4)["ok"] is False
    assert api.save_palette("海洋", r["colors"])["ok"] is True
    assert api.set_active_palette("海洋")["ok"] is True
    s = load_settings(path)
    assert s.palettes["海洋"] == r["colors"] and s.active_palette == "海洋"
    assert api.set_active_palette("不存在")["ok"] is False
    assert api.save_palette("  ", ["#ff0000"])["ok"] is False
    assert api.save_palette("x", [])["ok"] is False
    assert api.delete_palette("海洋")["ok"] is True
    s = load_settings(path)
    assert "海洋" not in s.palettes and s.active_palette == "默认"
    r = api.delete_palette("默认")
    assert r["ok"] is False and "至少" in r["error"]


# ---- effects / presets ------------------------------------------------------

def test_list_effects_includes_aurora(loop, tmp_path):
    api, _, _ = make_api(loop, tmp_path)
    effects = api.list_effects()
    assert {"id": "aurora", "label": "极光", "device": False} in effects
    assert {"id": "projection", "label": "投影", "device": False} in effects
    assert all(set(e) == {"id", "label", "device"} for e in effects)


def test_presets_crud_and_validation(loop, tmp_path):
    api, _, path = make_api(loop, tmp_path)
    names = [p["name"] for p in api.list_presets()]
    assert {"orange blur", "yellow blue", "pink blue", "pink yellow"} <= set(names)
    ok = api.save_preset("mine", ["flow1", "aurora"], ["#ff0000"], 30)
    assert ok["ok"] is True
    assert load_settings(path).diy_presets["mine"] == {
        "effects": ["flow1", "aurora"], "colors": ["#ff0000"], "speed_pct": 30}
    r = api.save_preset("bad", ["nope"], ["#ff0000"], 30)
    assert r["ok"] is False and "效果" in r["error"]
    r = api.save_preset("bad", ["flow1"], [], 30)
    assert r["ok"] is False and "颜色" in r["error"]
    r = api.save_preset("bad", ["flow1"], ["#ff0000"], 101)
    assert r["ok"] is False and "速度" in r["error"]
    r = api.save_preset("bad", [], ["#ff0000"], 50)
    assert r["ok"] is False
    r = api.save_preset("", ["flow1"], ["#ff0000"], 50)
    assert r["ok"] is False
    assert api.delete_preset("mine")["ok"] is True
    assert "mine" not in load_settings(path).diy_presets
    assert api.delete_preset("mine")["ok"] is False


def test_apply_preset_mix_records_last_mode(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    r = api.apply_preset("pink blue")
    assert r["ok"] is True
    mode = engine.current
    assert isinstance(mode, EffectMode)
    assert mode.effect == ["rainbow1", "rainbow2"]
    assert mode.palette[0] == (0xf0, 0x40, 0xb0)
    assert load_settings(path).last_mode == {"kind": "preset", "args": {"name": "pink blue"}}
    json.dumps(api.get_state())
    assert api.apply_preset("missing")["ok"] is False


# ---- scenes -----------------------------------------------------------------

def test_list_scenes_by_category(loop, tmp_path):
    api, _, _ = make_api(loop, tmp_path)
    cats = api.list_scenes()
    assert [c["category"] for c in cats] == ["自然", "生活"]
    assert cats[0]["scenes"][0] == {"id": 1, "name": "极光", "hint": "aurora"}


def test_apply_scene_uses_profile_cmd_version(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    assert api.apply_scene(2)["ok"] is True
    assert engine.oneshots[-1] == (packets_for(SCENES[1], 1), "scene")
    assert api.apply_scene(999)["ok"] is False


# ---- screen / music / calibration --------------------------------------------

def test_start_screen_sync_merges_options_and_records(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path, list_monitors=lambda: [{"index": 1}])
    assert api.list_monitors() == [{"index": 1}]
    r = api.start_screen_sync({"monitor": 2, "saturation": 1.5, "bogus": 1})
    assert r["ok"] is True
    mode = engine.current
    assert isinstance(mode, ScreenSyncMode)
    assert mode.monitor == 2 and mode.saturation == 1.5
    s = load_settings(path)
    assert s.screen["monitor"] == 2 and "bogus" not in s.screen
    assert s.last_mode["kind"] == "screen"


def test_screen_transition_time_range(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    assert api.start_screen_sync({"transition_s": 5.0})["ok"] is True
    assert engine.current.transition_s == 5.0
    assert load_settings(path).screen["transition_s"] == 5.0
    assert api.start_screen_sync({"transition_s": 0.3})["ok"] is True
    for bad in (0.2, 5.1, "slow", True, None):
        r = api.start_screen_sync({"transition_s": bad})
        assert r["ok"] is False and "过渡时间" in r["error"]
    # the old key is no longer a screen option
    api.start_screen_sync({"smoothing": 1.0})
    assert "smoothing" not in load_settings(path).screen


def test_region_screen_sync_needs_no_calibration(loop, tmp_path):
    # region = 6 vertical screen columns; the calibration endpoints still work but
    # no mode uses them any more
    api, engine, _ = make_api(loop, tmp_path)
    assert api.start_screen_sync({"mode": "region"})["ok"] is True
    assert engine.current.mode == "region"
    points = [[0.2 + i * 0.04, 0.05] for i in range(N)]
    assert api.save_calibration(points)["ok"] is True
    assert api.save_calibration([[0.1, 0.1]])["ok"] is False


@pytest.mark.parametrize("mode", ["dominant", "fade", "projection", "region"])
def test_screen_modes_accepted(loop, tmp_path, mode):
    api, engine, path = make_api(loop, tmp_path)
    assert api.start_screen_sync({"mode": mode, "tempo": 70})["ok"] is True
    assert engine.current.mode == mode and engine.current.tempo == 70
    s = load_settings(path).screen
    assert s["mode"] == mode and s["tempo"] == 70


@pytest.mark.parametrize("opts,word", [({"mode": "bogus"}, "模式"), ({"mode": 3}, "模式"),
                                       ({"tempo": 101}, "节奏"), ({"tempo": -1}, "节奏"),
                                       ({"tempo": "fast"}, "节奏"), ({"tempo": True}, "节奏")])
def test_screen_mode_and_tempo_validation(loop, tmp_path, opts, word):
    api, engine, _ = make_api(loop, tmp_path)
    r = api.start_screen_sync(opts)
    assert r["ok"] is False and word in r["error"]
    assert engine.calls == []


def test_start_music(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    r = api.start_music({"sensitivity": 1.5})
    assert r["ok"] is True
    mode = engine.current
    assert isinstance(mode, MusicMode) and mode.sensitivity == 1.5
    s = load_settings(path)
    assert s.music["sensitivity"] == 1.5 and s.last_mode["kind"] == "music"


def test_calibration_light_lights_one_segment(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.start_effect("flow1", 50)
    assert api.calibration_light(3)["ok"] is True
    packets, key = engine.oneshots[-1]
    assert key == "calibration"
    assert segment_packet((255, 255, 255), 1 << 3) in packets
    assert segment_packet((0, 0, 0), ((1 << N) - 1) & ~(1 << 3)) in packets
    assert api.calibration_light(N)["ok"] is False


def test_set_autostart_uses_injected_registry(loop, tmp_path):
    state = {"on": False}
    api, _, path = make_api(loop, tmp_path,
                            set_autostart=lambda on: state.update(on=on),
                            is_autostart=lambda: state["on"])
    r = api.set_autostart(True)
    assert r == {"ok": True, "autostart": True}
    assert load_settings(path).autostart is True
    assert api.get_state()["autostart"] is True


# ---- restore / push / exposure ----------------------------------------------

@pytest.mark.parametrize("last_mode, kind", [
    ({"kind": "effect", "args": {"effect": "aurora", "speed_pct": 20}}, EffectMode),
    ({"kind": "preset", "args": {"name": "pink blue"}}, EffectMode),
    ({"kind": "screen", "args": {}}, ScreenSyncMode),
    ({"kind": "music", "args": {"sensitivity": 2.0}}, MusicMode),
])
def test_restore_last_mode_kinds(loop, tmp_path, last_mode, kind):
    api, engine, _ = make_api(loop, tmp_path, settings=Settings(last_mode=last_mode))
    assert api.restore_last_mode() is True
    assert isinstance(engine.current, kind)


@pytest.mark.parametrize("last_mode", [
    None,
    {"kind": "teleport"},
    {"kind": "effect", "args": {"effect": "gone", "speed_pct": 20}},
    {"kind": "preset", "args": {"name": "deleted"}},
    {"kind": "effect", "args": "garbage"},
    "garbage",
    {"kind": "effect", "args": {"effect": "flow1", "bogus": 1}},
])
def test_restore_ignores_invalid_last_mode(loop, tmp_path, last_mode):
    api, engine, _ = make_api(loop, tmp_path, settings=Settings(last_mode=last_mode))
    assert api.restore_last_mode() is False
    assert engine.current is None


def test_frame_pushes_are_throttled_status_is_immediate(loop, tmp_path):
    pushed = []
    clock = {"t": 100.0}
    api, engine, _ = make_api(loop, tmp_path, push=pushed.append, clock=lambda: clock["t"])
    for _ in range(5):
        api.on_frame([(9, 9, 9)] * N)
    assert len(pushed) == 1
    clock["t"] += 0.11
    api.on_frame([(8, 8, 8)] * N)
    assert len(pushed) == 2 and pushed[-1]["frame"][0] == "#080808"
    engine.link.status = "busy_elsewhere"
    api.on_status("busy_elsewhere", BUSY_MESSAGE)
    assert len(pushed) == 3
    assert pushed[-1]["status"] == "busy_elsewhere"
    assert pushed[-1]["message"] == BUSY_MESSAGE


def test_push_failures_do_not_break_frames(loop, tmp_path):
    def boom(state):
        raise RuntimeError("window gone")
    api, _, _ = make_api(loop, tmp_path, push=boom)
    api.on_frame([(1, 1, 1)] * N)  # must not raise
    api.on_status("connected", None)


JS_METHODS = {
    "get_state", "set_power", "set_brightness", "set_segments", "fill", "generate_palette",
    "save_palette", "delete_palette", "set_active_palette", "list_effects", "start_effect",
    "list_presets", "save_preset", "delete_preset", "apply_preset", "list_scenes",
    "apply_scene", "list_monitors", "start_screen_sync", "start_music", "stop_mode",
    "calibration_light", "save_calibration", "set_autostart", "set_power_sync",
    "set_transition", "get_color_cal", "set_color_cal", "reset_color_cal", "test_color",
    "end_color_test", "get_language", "set_language", "scan_devices", "select_device",
    "forget_device",
}


def test_only_js_methods_exposed_to_pywebview(loop, tmp_path):
    # mirrors webview.util get_functions: public names, skip _serializable=False,
    # recurse into non-callable objects (which must not exist on Api)
    api, _, _ = make_api(loop, tmp_path)
    exposed = set()
    for name in dir(api):
        if name.startswith("_"):
            continue
        attr = getattr(api, name)
        if not getattr(attr, "_serializable", True):
            continue
        assert callable(attr), f"public non-callable attribute {name} would be walked"
        exposed.add(name)
    assert exposed == JS_METHODS


def test_unexpected_exception_returns_error(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)

    async def broken(*a, **k):
        raise RuntimeError("boom")
    engine.oneshot = broken
    r = api.fill("#ffffff")
    assert r["ok"] is False and isinstance(r["error"], str) and r["error"]


# ---- Task 12 additions: DIY editor 应用 (mix list) and aurora width in presets -------

def test_start_effect_accepts_mix_list_and_restores(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    r = api.start_effect(["rainbow1", "rainbow2"], 45, ["#f040b0", "#0a78c8"])
    assert r["ok"] is True
    assert isinstance(engine.current, EffectMode)
    assert engine.current.effect == ["rainbow1", "rainbow2"]
    lm = load_settings(path).last_mode
    assert lm == {"kind": "effect", "args": {"effect": ["rainbow1", "rainbow2"], "speed_pct": 45,
                                            "colors": ["#f040b0", "#0a78c8"], "width": None}}
    assert api.get_state()["mode"]["args"]["effect"] == ["rainbow1", "rainbow2"]
    # a one-element list is the simple (single effect) case
    assert api.start_effect(["aurora"], 50, ["#ff0000", "#0000ff"], 3)["ok"] is True
    assert engine.current.effect == "aurora" and engine.current.width == 3
    # the mix last_mode restores after a restart
    api2, engine2, _ = make_api(loop, tmp_path / "r", settings=Settings(last_mode=lm))
    assert api2.restore_last_mode() is True
    assert engine2.current.effect == ["rainbow1", "rainbow2"]


@pytest.mark.parametrize("bad", [[], ["flow1", "nope"], [1], "nope"])
def test_start_effect_rejects_bad_mix_lists(loop, tmp_path, bad):
    api, engine, _ = make_api(loop, tmp_path)
    r = api.start_effect(bad, 50)
    assert r["ok"] is False and "效果" in r["error"]
    assert engine.calls == []


def test_preset_width_is_saved_and_applied(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    assert api.save_preset("极光宽", ["aurora"], ["#ff0000", "#00ff00"], 50, 6)["ok"] is True
    assert load_settings(path).diy_presets["极光宽"] == {
        "effects": ["aurora"], "colors": ["#ff0000", "#00ff00"], "speed_pct": 50, "width": 6}
    assert next(p for p in api.list_presets() if p["name"] == "极光宽")["width"] == 6
    assert api.apply_preset("极光宽")["ok"] is True
    assert engine.current.width == 6
    assert api.save_preset("w", ["aurora"], ["#ff0000"], 50, 0)["ok"] is False
    # no width -> key omitted, apply uses the effect's default
    assert api.save_preset("nw", ["aurora"], ["#ff0000"], 50)["ok"] is True
    assert "width" not in load_settings(path).diy_presets["nw"]
    api.apply_preset("nw")
    assert engine.current.width is None


# ---- final review: shutdown deadline, settings-file errors ----------------------

def test_shutdown_has_one_total_deadline(loop, tmp_path):
    import time as _time
    api, engine, _ = make_api(loop, tmp_path)
    calls = []

    async def hang_stop():
        calls.append("stop")
        await asyncio.sleep(10)  # e.g. engine lock held by a BLE connect

    async def hang_close():
        calls.append("close")
        await asyncio.sleep(10)

    engine.stop = hang_stop
    engine.link.close = hang_close
    t0 = _time.monotonic()
    api.shutdown(timeout=0.3)
    assert _time.monotonic() - t0 < 0.6  # not 0.3 per step, let alone 10 s each
    assert calls[0] == "stop"


def test_shutdown_default_deadline_is_short():
    import inspect
    from govee_win11 import api as api_mod
    assert inspect.signature(Api.shutdown).parameters["timeout"].default == api_mod.SHUTDOWN_TIMEOUT
    assert api_mod.SHUTDOWN_TIMEOUT <= 3.0


def test_shutdown_closes_link_after_stopping_engine(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    order = []

    async def close():
        order.append("close")

    async def stop():
        order.append("stop")

    engine.stop, engine.link.close = stop, close
    api.shutdown()
    assert order == ["stop", "close"]


@pytest.fixture
def locked_settings(monkeypatch):
    """settings.json permanently locked (antivirus / indexer): every save fails."""
    import govee_win11.api as api_mod
    calls = []

    def boom(settings, path):
        calls.append(path)
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(api_mod, "save_settings", boom)
    monkeypatch.setattr(api_mod, "SAVE_RETRY_DELAY", 0.0)
    return calls


def test_brightness_is_sent_even_when_settings_save_fails(loop, tmp_path, locked_settings,
                                                          caplog):
    api, engine, _ = make_api(loop, tmp_path)
    r = api.set_brightness(40)
    assert r == {"ok": True, "brightness": 40}
    assert engine.link.transient[-1] == ([brightness_packet(40)], "brightness")
    assert api.get_state()["brightness"] == 40     # kept in memory
    assert len(locked_settings) == 3               # retried before giving up
    assert "could not save settings" in caplog.text


def test_brightness_sent_before_saving(loop, tmp_path, monkeypatch):
    import govee_win11.api as api_mod
    api, engine, _ = make_api(loop, tmp_path)
    seen = []
    monkeypatch.setattr(api_mod, "save_settings",
                        lambda s, p: seen.append(len(engine.link.transient)))
    api.set_brightness(10)
    assert seen == [1]


def test_mode_start_succeeds_when_settings_save_fails(loop, tmp_path, locked_settings):
    api, engine, _ = make_api(loop, tmp_path)
    r = api.start_effect("aurora", 50)
    assert r["ok"] is True
    assert engine.current is not None
    assert api.stop_mode()["ok"] is True
    assert api.save_palette("x", ["#ff0000"])["ok"] is True


def test_transient_save_failure_is_retried(loop, tmp_path, monkeypatch):
    import govee_win11.api as api_mod
    from govee_win11.config import save_settings as real_save
    fails = [PermissionError(5, "Access is denied")]

    def flaky(settings, path):
        if fails:
            raise fails.pop()
        real_save(settings, path)

    monkeypatch.setattr(api_mod, "save_settings", flaky)
    monkeypatch.setattr(api_mod, "SAVE_RETRY_DELAY", 0.0)
    api, engine, path = make_api(loop, tmp_path)
    assert api.set_brightness(55)["ok"] is True
    assert load_settings(path).brightness == 55


def test_set_window_visible_switches_linger_on_loop(loop, tmp_path):
    import time
    api, engine, _ = make_api(loop, tmp_path)
    api.set_window_visible(True)
    api.set_window_visible(False)
    for _ in range(50):
        if len(engine.link.linger) == 2:
            break
        time.sleep(0.02)
    assert engine.link.linger == [60.0, 3.0]


# ---- Task 15: single-effect DIY runs on the strip itself -----------------------

from govee_win11 import diy_format  # noqa: E402


def _rgb(h):
    return diy_format.hex_rgb(h)


def test_single_effect_uploads_diy_to_strip_without_effect_mode(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    r = api.start_effect("fade3", 40, ["#ff5000", "#f20d0d"])
    assert r["ok"] is True
    want = diy_format.diy_packets("fade3", [_rgb("#ff5000"), _rgb("#f20d0d")], 40, "a3")
    assert engine.oneshots[-1] == (want, "diy")
    assert engine.paced is True
    assert not any(c[0] == "run" for c in engine.calls)
    lm = load_settings(path).last_mode
    assert lm == {"kind": "effect", "args": {"effect": "fade3", "speed_pct": 40,
                                            "colors": ["#ff5000", "#f20d0d"], "width": None}}
    st = api.get_state()
    json.dumps(st)
    assert st["mode"]["device"] is True and st["mode"]["kind"] == "effect"
    assert st["mode"]["colors"] == ["#ff5000", "#f20d0d"]
    assert st["frame"][0] == "#ff5000" and st["frame"][-1] == "#f20d0d"


def test_one_element_list_and_app_ids_map(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    assert api.start_effect(["flash2"], 100, ["#ff0000"])["ok"] is True
    assert engine.oneshots[-1][0] == diy_format.diy_packets("blink2", [(255, 0, 0)], 100, "a3")
    assert api.start_effect("flow1", 100, ["#ff0000", "#00ff00"])["ok"] is True
    assert engine.oneshots[-1][0] == diy_format.diy_packets(
        "stream1", [(255, 0, 0), (0, 255, 0)], 100, "a3")


def test_pc_rendered_cases_keep_effect_mode(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    assert api.start_effect("aurora", 50, ["#ff0000"])["ok"] is True
    assert isinstance(engine.current, EffectMode)
    assert api.start_effect(["fade1", "fade2"], 50, ["#ff0000"])["ok"] is True
    assert engine.current.effect == ["fade1", "fade2"]
    nine = ["#%02x0000" % (10 * i) for i in range(1, 10)]
    assert api.start_effect("fade1", 50, nine)["ok"] is True
    assert isinstance(engine.current, EffectMode)
    assert not engine.oneshots
    st = api.get_state()
    assert "device" not in st["mode"]


def test_device_effect_replaced_by_other_modes(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    api.start_effect("fade3", 40, ["#ff5000"])
    assert api.get_state()["mode"]["device"] is True
    api.set_segments(["#ff0000"] * N)
    assert api.get_state()["mode"] is None
    api.start_effect("fade3", 40, ["#ff5000"])
    api.start_effect("aurora", 40, ["#ff5000"])
    assert "device" not in api.get_state()["mode"]
    api.start_effect("fade3", 40, ["#ff5000"])
    api.stop_mode()
    assert api.get_state()["mode"] is None


def test_device_effect_busy_error(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    engine.oneshot_result = False
    engine.link.status = "busy_elsewhere"
    r = api.start_effect("fade3", 40, ["#ff5000"])
    assert r["ok"] is False and r["error"] == BUSY_MESSAGE
    assert api.get_state()["mode"] is None
    assert load_settings(path).last_mode is None


def test_apply_builtin_presets(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    assert api.apply_preset("orange blur")["ok"] is True
    assert engine.oneshots[-1][0] == diy_format.diy_packets(
        "fade3", [_rgb("#ff5000"), _rgb("#f20d0d")], 40, "a3")
    assert load_settings(path).last_mode == {"kind": "preset", "args": {"name": "orange blur"}}
    # a mix preset stays PC-rendered
    assert api.apply_preset("pink yellow")["ok"] is True
    assert isinstance(engine.current, EffectMode)


def test_restore_device_last_mode_reuploads(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path, settings=Settings(
        last_mode={"kind": "preset", "args": {"name": "yellow blue"}}))
    assert api.restore_last_mode() is True
    assert engine.oneshots and engine.paced is True


def test_device_speed_is_passed_through_unscaled(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    api.start_effect("marquee1", 7, ["#ff0000"])
    assert engine.oneshots[-1][0] == diy_format.diy_packets("marquee1", [(255, 0, 0)], 7, "a3")
    assert api.start_effect("marquee1", 101, ["#ff0000"])["ok"] is False


def test_stop_freezes_strip_run_diy_to_shown_colours(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.apply_preset("orange blur")
    shown = [_rgb(h) for h in api.get_state()["frame"]]
    assert api.stop_mode()["ok"] is True
    pk, key = engine.oneshots[-1]
    assert key == "segments" and engine.paced_calls[-1] is False
    assert pk == api._segments_packets(shown)
    st = api.get_state()
    assert st["mode"] is None and [_rgb(h) for h in st["frame"]] == shown
    assert load_settings(path).last_mode is None
    n = len(engine.oneshots)
    api.stop_mode()  # nothing running on the strip: no further upload
    assert len(engine.oneshots) == n


def test_pc_paths_for_big_single_preset_and_aurora_preset(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    nine = ["#%02x0000" % (10 * i) for i in range(1, 10)]
    assert api.save_preset("big", ["fade1"], nine, 50)["ok"] is True
    assert api.save_preset("au", ["aurora"], ["#ff0000"], 50)["ok"] is True
    for name in ("big", "au"):
        assert api.apply_preset(name)["ok"] is True
        assert isinstance(engine.current, EffectMode)
    assert not engine.oneshots


def test_list_effects_flags_device_capable(loop, tmp_path):
    api, _, _ = make_api(loop, tmp_path)
    flags = {e["id"]: e["device"] for e in api.list_effects()}
    assert flags["fade3"] is True and flags["flow1"] is True and flags["aurora"] is False


# ---- Task 17: power sync (shutdown / sleep / login) ---------------------------------

def test_set_power_sync_persists_and_is_reported(loop, tmp_path):
    api, _, path = make_api(loop, tmp_path)
    assert api.power_sync_enabled() is True          # default on
    assert api.get_state()["settings"]["power_sync"] is True
    r = api.set_power_sync(False)
    assert r == {"ok": True, "power_sync": False}
    assert api.power_sync_enabled() is False
    assert load_settings(path).power_sync is False
    assert api.set_power_sync(True)["power_sync"] is True


def test_power_off_keep_mode_stops_mode_but_keeps_last_mode(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.start_effect("flow1", 50)
    lm = load_settings(path).last_mode
    assert api.power_off_keep_mode(1.0) is True
    assert engine.oneshots[-1] == ([power_packet(False)], "power")  # oneshot stops the mode
    assert load_settings(path).last_mode == lm
    assert api._settings.last_mode == lm
    assert api.get_state()["power"] is False
    assert api.get_state()["mode"] is None


def test_power_off_keep_mode_keeps_device_diy_last_mode(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.apply_preset("orange blur")                  # runs on the strip itself
    lm = load_settings(path).last_mode
    assert lm["kind"] == "preset"
    api.power_off_keep_mode(1.0)
    assert load_settings(path).last_mode == lm
    assert api.get_state()["mode"] is None


def test_power_on_keep_mode_then_restore(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.start_effect("aurora", 50)                   # PC-rendered
    api.power_off_keep_mode(1.0)
    assert api.power_on_keep_mode(1.0) is True
    assert engine.oneshots[-1] == ([power_packet(True)], "power")
    assert api.get_state()["power"] is True
    assert api.restore_last_mode() is True
    assert isinstance(engine.current, EffectMode)


def test_power_keep_mode_failures_return_false(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    engine.oneshot_result = False
    assert api.power_off_keep_mode(1.0) is False
    assert api.power_on_keep_mode(1.0) is False


def test_power_off_keep_mode_is_bounded(loop, tmp_path):
    import time as _time
    api, engine, _ = make_api(loop, tmp_path)

    release = threading.Event()

    async def hang(packets, key=None, paced=False):
        while not release.is_set():  # e.g. BLE connect that never finishes
            await asyncio.sleep(0.01)
        return True

    engine.oneshot = hang
    t0 = _time.monotonic()
    assert api.power_off_keep_mode(0.2) is False
    assert _time.monotonic() - t0 < 0.6
    release.set()
    _time.sleep(0.05)


# ---- Task 17: 过渡方式（实验） ---------------------------------------------------------

@pytest.fixture
def linear_mix():
    from govee_win11 import effects as fx
    yield
    fx.set_mix("linear")


def test_transition_defaults_applied_at_start(loop, tmp_path, linear_mix):
    from govee_win11 import effects as fx
    api, engine, _ = make_api(loop, tmp_path)
    assert engine.min_segment_interval == pytest.approx(0.2)
    assert fx.get_mix() == "linear"
    assert api.get_state()["settings"]["transition"] == {"rate": 5, "mix": "linear"}
    s = Settings(transition={"rate": 8, "mix": "perceptual"})
    api, engine, _ = make_api(loop, tmp_path, settings=s)
    assert engine.min_segment_interval == pytest.approx(1 / 8)
    assert fx.get_mix() == "perceptual"


def test_set_transition_applies_live_and_persists(loop, tmp_path, linear_mix):
    from govee_win11 import effects as fx
    api, engine, path = make_api(loop, tmp_path)
    api.start_effect("aurora", 50)
    running = engine.current
    r = api.set_transition({"rate": 10, "mix": "perceptual"})
    assert r == {"ok": True, "transition": {"rate": 10, "mix": "perceptual"}}
    assert engine.min_segment_interval == pytest.approx(0.1)
    assert fx.get_mix() == "perceptual"
    assert engine.current is running                     # no restart needed
    assert load_settings(path).transition == {"rate": 10, "mix": "perceptual"}
    # partial update keeps the other key
    assert api.set_transition({"rate": 3})["transition"] == {"rate": 3, "mix": "perceptual"}
    assert engine.min_segment_interval == pytest.approx(1 / 3)


@pytest.mark.parametrize("opts,word", [({"rate": 7}, "刷新次数"), ({"rate": "5"}, "刷新次数"),
                                       ({"rate": True}, "刷新次数"), ({"mix": "hsv"}, "混合方式"),
                                       ("fast", "过渡方式"), ({"mix": 1}, "混合方式")])
def test_set_transition_validation(loop, tmp_path, linear_mix, opts, word):
    api, engine, path = make_api(loop, tmp_path)
    r = api.set_transition(opts)
    assert r["ok"] is False and word in r["error"]
    assert engine.min_segment_interval == pytest.approx(0.2)


# ---- Task 18: 颜色校准（白平衡） -------------------------------------------------------

from govee_win11 import protocol as _protocol  # noqa: E402
from govee_win11.api import ColorTestMode  # noqa: E402


def test_color_cal_applied_at_start(loop, tmp_path):
    make_api(loop, tmp_path, settings=Settings(color_cal={"r": 90, "g": 80, "b": 70}))
    assert _protocol.get_color_cal() == (90, 80, 70)
    api, _, _ = make_api(loop, tmp_path / "b")
    assert _protocol.get_color_cal() == (100, 100, 100)
    assert api.get_color_cal() == {"ok": True, "color_cal": {"r": 100, "g": 100, "b": 100},
                                   "testing": False}


def test_set_color_cal_live_persists_and_recolors(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.start_effect("aurora", 50)
    running = engine.current
    r = api.set_color_cal({"r": 95, "g": 80.4})
    assert r == {"ok": True, "color_cal": {"r": 95, "g": 80, "b": 100}}
    assert _protocol.get_color_cal() == (95, 80, 100)
    assert engine.recolors == 1 and engine.current is running   # live, no restart
    assert load_settings(path).color_cal == {"r": 95, "g": 80, "b": 100}
    assert api.get_state()["settings"]["color_cal"] == {"r": 95, "g": 80, "b": 100}
    assert api.reset_color_cal()["color_cal"] == {"r": 100, "g": 100, "b": 100}
    assert _protocol.get_color_cal() == (100, 100, 100)


@pytest.mark.parametrize("bad", [None, "90", {}, {"x": 1}, {"r": 101}, {"g": -1},
                                 {"b": [50]}, {"r": True}, {"r": float("nan")}])
def test_set_color_cal_validation(loop, tmp_path, bad):
    api, engine, path = make_api(loop, tmp_path)
    r = api.set_color_cal(bad)
    assert r["ok"] is False and ("颜色校准" in r["error"])
    assert _protocol.get_color_cal() == (100, 100, 100) and engine.recolors == 0


def test_set_color_cal_changes_sent_bytes_but_not_preview(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    api.set_color_cal({"r": 100, "g": 50, "b": 0})
    api.fill("#ffffff")
    packets, _ = engine.oneshots[-1]
    assert packets[0][4:7] == bytes([255, 128, 0])               # wire: calibrated
    assert set(api.get_state()["frame"]) == {"#ffffff"}            # preview: intended


def test_device_diy_upload_uses_calibration_without_reupload_on_change(loop, tmp_path):
    from govee_win11 import diy_format
    api, engine, _ = make_api(loop, tmp_path)
    api.set_color_cal({"r": 100, "g": 80, "b": 60})
    api.start_effect("fade3", 50, colors=["#ffffff"])
    packets, key = engine.oneshots[-1]
    assert key == "diy"
    _protocol.set_color_cal(100, 100, 100)
    assert packets == diy_format.diy_packets("fade3", [(255, 204, 153)], 50, "a3")
    api._apply_color_cal()
    n = len(engine.oneshots)
    api.set_color_cal({"g": 70})                                   # DIY running on device
    assert len(engine.oneshots) == n                               # not re-uploaded
    assert api.get_state()["mode"]["device"] is True


def test_test_color_streams_and_keeps_last_mode(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.start_effect("aurora", 50)
    lm = load_settings(path).last_mode
    r = api.test_color("#FFD7A0")
    assert r["ok"] is True and r["color"] == "#ffd7a0"
    mode = engine.current
    assert isinstance(mode, ColorTestMode)
    assert mode.next_frame(0.0) == [(0xFF, 0xD7, 0xA0)] * N
    assert load_settings(path).last_mode == lm
    st = api.get_state()
    assert st["color_test"] is True and st["mode"]["kind"] == "color_test"
    assert st["frame"] == ["#ffd7a0"] * N
    # another test colour reuses the running stream (planner + rate cap), no restart
    runs = sum(1 for c in engine.calls if c[0] == "run")
    api.test_color("#808080")
    assert engine.current is mode and mode.color == (128, 128, 128)
    assert sum(1 for c in engine.calls if c[0] == "run") == runs
    # moving a slider while testing -> recolor through the engine
    api.set_color_cal({"b": 90})
    assert engine.recolors == 1


def test_end_color_test_restores_previous_mode(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.start_effect("aurora", 50)
    api.test_color("#ffffff")
    r = api.end_color_test()
    assert r == {"ok": True, "restored": True}
    assert isinstance(engine.current, EffectMode) and engine.current.effect == "aurora"
    assert api.get_state()["color_test"] is False


def test_end_color_test_with_nothing_running_leaves_the_colour(loop, tmp_path):
    api, engine, path = make_api(loop, tmp_path)
    api.test_color("#e0ac8a")
    r = api.end_color_test()
    assert r == {"ok": True, "restored": False}
    assert engine.current is None and engine.calls[-1] == ("stop",)
    assert not engine.oneshots                                     # nothing re-sent
    st = api.get_state()
    assert st["mode"] is None and st["frame"] == ["#e0ac8a"] * N
    assert st["settings"]["last_mode"] is None


def test_end_color_test_when_not_testing_is_harmless(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    assert api.end_color_test() == {"ok": True, "restored": False}
    api.test_color("#ff0000")
    api.start_music()                                              # another mode took over
    current = engine.current
    assert api.end_color_test() == {"ok": True, "restored": False}
    assert engine.current is current


@pytest.mark.parametrize("bad", ["white", "#12345", 7, None])
def test_test_color_validation(loop, tmp_path, bad):
    api, engine, _ = make_api(loop, tmp_path)
    r = api.test_color(bad)
    assert r["ok"] is False and "颜色格式不正确" in r["error"]
    assert engine.current is None


def test_bt_unavailable_messages(loop, tmp_path):
    from govee_win11.link import BT_UNAVAILABLE_MESSAGE
    pushed = []
    api, engine, _ = make_api(loop, tmp_path, push=pushed.append)
    engine.link.status = "bt_unavailable"
    api.on_status("bt_unavailable", BT_UNAVAILABLE_MESSAGE)
    assert pushed[-1]["status"] == "bt_unavailable"
    assert pushed[-1]["message"] == BT_UNAVAILABLE_MESSAGE
    engine.oneshot_result = False
    assert api.set_power(True) == {"ok": False, "error": BT_UNAVAILABLE_MESSAGE}
    r = api.start_effect("aurora", 50)                 # mode keeps retrying; warn
    assert r["ok"] is True and r["warning"] == BT_UNAVAILABLE_MESSAGE
    assert BT_UNAVAILABLE_MESSAGE != BUSY_MESSAGE


def test_mode_generation_tracks_user_mode_changes_not_power_sync(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    g = api.mode_generation()
    api.power_on_keep_mode(1.0)                 # PowerSync's own power-on: unchanged
    assert api.mode_generation() == g
    api.start_effect("aurora", 50)
    assert api.mode_generation() == g + 1
    api.fill("#ff0000")
    api.stop_mode()
    assert api.mode_generation() == g + 3


def test_call_timeout_reports_the_link_problem(loop, tmp_path, monkeypatch):
    from govee_win11 import api as api_mod
    from govee_win11.link import BT_UNAVAILABLE_MESSAGE
    api, engine, _ = make_api(loop, tmp_path)

    async def hang(packets, key=None, paced=False):
        await asyncio.sleep(5)

    engine.oneshot = hang
    monkeypatch.setattr(api_mod, "ONESHOT_TIMEOUT", 0.05)
    engine.link.status = "bt_unavailable"
    assert api.set_power(True) == {"ok": False, "error": BT_UNAVAILABLE_MESSAGE}
    engine.link.status = "connecting"
    assert api.set_power(True) == {"ok": False, "error": BUSY_MESSAGE}


# ---- 设备 (discovery / choose / forget) --------------------------------------------

from govee_win11.discovery import FoundDevice  # noqa: E402

DEV1 = FoundDevice("Govee_H6125_0001", "AA:BB:CC:DD:EE:01", -50)
DEV2 = FoundDevice("ihoment_H6125_0002", "AA:BB:CC:DD:EE:02", -75)


class AddressLink(FakeLink):
    def __init__(self):
        super().__init__()
        self.addresses = []

    async def set_address(self, address):
        self.addresses.append(address)


def make_device_api(loop, tmp_path, results=None, **kw):
    async def discover():
        r = results.pop(0) if results else []
        if isinstance(r, BaseException):
            raise r
        return r
    api, engine, path = make_api(loop, tmp_path, discover=discover, **kw)
    engine.link = AddressLink()
    return api, engine, path


def test_state_has_no_device_by_default(loop, tmp_path):
    api, _, _ = make_api(loop, tmp_path)
    s = api.get_state()
    assert s["device"] is None and s["device_others"] == []
    assert s["settings"]["device_address"] is None


def test_auto_discovered_device_is_saved_and_others_listed(loop, tmp_path):
    pushed = []
    api, _, path = make_device_api(loop, tmp_path, push=pushed.append)
    api.on_device_found(DEV1, [DEV2])
    saved = load_settings(path)
    assert (saved.device_address, saved.device_name) == (DEV1.address, DEV1.name)
    s = pushed[-1]
    assert s["device"] == {"address": DEV1.address, "name": DEV1.name}
    assert s["device_others"] == [DEV2.to_dict()]


def test_scan_devices_lists_found_strips(loop, tmp_path):
    api, _, _ = make_device_api(loop, tmp_path, results=[[DEV1, DEV2]])
    r = api.scan_devices()
    assert r == {"ok": True, "devices": [DEV1.to_dict(), DEV2.to_dict()], "current": None}


def test_scan_devices_bluetooth_off(loop, tmp_path):
    from bleak.exc import BleakError
    api, _, _ = make_device_api(loop, tmp_path,
                                results=[BleakError("Bluetooth radio is not powered on")])
    r = api.scan_devices()
    assert r["ok"] is False and r["error"].startswith("电脑蓝牙不可用")


def test_scan_unavailable_without_discover(loop, tmp_path):
    api, _, _ = make_api(loop, tmp_path)
    assert api.scan_devices() == {"ok": False, "error": "无法搜索设备"}


def test_select_device_saves_and_switches_the_link(loop, tmp_path):
    api, engine, path = make_device_api(loop, tmp_path, results=[[DEV1, DEV2]])
    api.on_device_found(DEV1, [DEV2])
    api.scan_devices()
    r = api.select_device("aa:bb:cc:dd:ee:02")
    assert r["ok"] is True
    assert r["device"] == {"address": DEV2.address, "name": DEV2.name}  # name from the scan
    assert engine.link.addresses == [DEV2.address]
    saved = load_settings(path)
    assert (saved.device_address, saved.device_name) == (DEV2.address, DEV2.name)
    assert api.get_state()["device_others"] == []


def test_select_device_rejects_bad_address(loop, tmp_path):
    api, engine, path = make_device_api(loop, tmp_path)
    r = api.select_device("not-an-address")
    assert r == {"ok": False, "error": "设备地址格式不正确：not-an-address"}
    assert engine.link.addresses == [] and not path.exists()


def test_forget_device_clears_it_and_rediscovers(loop, tmp_path):
    api, engine, path = make_device_api(
        loop, tmp_path, settings=Settings(device_address=DEV1.address, device_name=DEV1.name))
    assert api.get_state()["device"] == {"address": DEV1.address, "name": DEV1.name}
    r = api.forget_device()
    assert r["ok"] is True
    assert engine.link.addresses == [None]
    saved = load_settings(path)
    assert saved.device_address is None and saved.device_name is None
    assert api.get_state()["device"] is None


def test_not_found_status_has_a_message(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    engine.link.status = "not_found"
    assert api.get_state()["message"] == \
        "没有找到 H6125 灯带，请确认灯带通电、手机 Govee App 已断开"


class _BleClient:
    def __init__(self, address):
        self.address = address
        self.is_connected = False

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def write_gatt_char(self, char, data, response=False):
        pass


def _real_link_api(loop, tmp_path, discover, address=None, settings=None):
    from govee_win11.link import DeviceLink
    api, engine, path = make_api(loop, tmp_path, settings=settings)
    link = DeviceLink(address, client_factory=_BleClient, linger_s=0.05, discover=discover,
                      on_device=api.on_device_found)
    engine.link = link
    return api, link, path


def _blocked_discovery(result):
    gate, started = asyncio.Event(), threading.Event()

    async def discover():
        started.set()
        await gate.wait()
        return list(result)
    return discover, gate, started


def _race(loop, link, gate, started, command):
    """Run ``command`` (Api call, blocking) while a discovery holds the link lock."""
    send = asyncio.run_coroutine_threadsafe(link.send_transient([b"\x01"]), loop)
    assert started.wait(2)
    out = []
    t = threading.Thread(target=lambda: out.append(command()))
    t.start()
    t.join(0.2)  # now waiting for the link lock
    loop.call_soon_threadsafe(gate.set)
    t.join(5)
    send.result(5)
    return out[0]


def test_select_device_during_discovery_keeps_the_users_choice(loop, tmp_path):
    discover, gate, started = _blocked_discovery([DEV1])
    api, link, path = _real_link_api(loop, tmp_path, discover)
    r = _race(loop, link, gate, started, lambda: api.select_device(DEV2.address, DEV2.name))
    assert r["ok"] is True
    assert link.address == DEV2.address
    saved = load_settings(path)
    assert (saved.device_address, saved.device_name) == (DEV2.address, DEV2.name)
    assert api.get_state()["device"] == {"address": DEV2.address, "name": DEV2.name}


def test_forget_device_during_discovery_leaves_settings_and_link_empty(loop, tmp_path):
    discover, gate, started = _blocked_discovery([DEV1])
    api, link, path = _real_link_api(loop, tmp_path, discover)
    r = _race(loop, link, gate, started, api.forget_device)
    assert r["ok"] is True
    assert link.address is None
    assert load_settings(path).device_address is None
    assert api.get_state()["device"] is None


def test_forget_on_a_held_link_saves_the_rediscovered_strip(loop, tmp_path):
    async def discover():
        return [DEV2, DEV1]
    api, link, path = _real_link_api(
        loop, tmp_path, discover, address=DEV1.address,
        settings=Settings(device_address=DEV1.address, device_name=DEV1.name))
    asyncio.run_coroutine_threadsafe(link.hold(), loop).result(2)
    r = api.forget_device()
    assert r["ok"] is True and link.address == DEV2.address
    saved = load_settings(path)
    assert (saved.device_address, saved.device_name) == (DEV2.address, DEV2.name)
    assert api.get_state()["device_others"] == [DEV1.to_dict()]
    asyncio.run_coroutine_threadsafe(link.close(), loop).result(2)


def test_stale_saved_device_suggests_choosing_another(loop, tmp_path):
    api, engine, _ = make_api(loop, tmp_path)
    engine.link.status = "busy_elsewhere"
    engine.link.device_stale = True
    s = api.get_state()
    assert s["device_stale"] is True
    assert s["message"] == BUSY_MESSAGE + " 如果换了灯带，请在「设置 → 设备」里选择另一条。"
    engine.link.device_stale = False
    assert api.get_state()["message"] == BUSY_MESSAGE
