import colorsys
import threading
from collections import Counter

import numpy as np
import pytest

from govee_win11 import screen
from govee_win11.screen import (
    ScreenSyncMode,
    Smoother,
    adjust,
    distribute,
    dominant_colors,
    region_for_point,
)

RED = (255, 0, 0)
BLUE = (0, 0, 255)
DEFAULTS = {"monitor": 1, "saturation": 2.0, "transition_s": 1.5, "gain": [1, 1, 1], "mode": "dominant"}


def _raw(m):
    """No temporal smoothing (tests of colour extraction, not of transitions)."""
    m.tau = 0.0
    return m


def _img(h=60, w=200, color=(0, 0, 0)):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :] = color
    return img


def _close(a, b, tol=12):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _sat(rgb):
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))[1]


# ---- dominant_colors -------------------------------------------------------

def test_two_color_image_proportions():
    img = _img()
    img[:, :140] = RED
    img[:, 140:] = BLUE
    res = dominant_colors(img)
    assert len(res) == 2
    (c1, p1), (c2, p2) = res
    assert _close(c1, RED) and _close(c2, BLUE)
    assert p1 == pytest.approx(0.7, abs=0.05)
    assert p2 == pytest.approx(0.3, abs=0.05)
    assert sum(p for _, p in res) == pytest.approx(1.0)


def test_all_black_returns_black():
    assert dominant_colors(_img()) == [((0, 0, 0), 1.0)]


def test_nearly_black_returns_black():
    img = _img()
    img[0, :3] = (255, 255, 0)  # well under 2% lit pixels
    assert dominant_colors(img) == [((0, 0, 0), 1.0)]


def test_dominant_colors_sorted_and_deterministic():
    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, size=(80, 120, 3), dtype=np.uint8)
    a = dominant_colors(img, k=5, seed=0)
    b = dominant_colors(img, k=5, seed=0)
    assert a == b
    props = [p for _, p in a]
    assert props == sorted(props, reverse=True)
    assert sum(props) == pytest.approx(1.0)
    assert len(a) <= 5
    for c, _ in a:
        assert all(isinstance(v, int) and 0 <= v <= 255 for v in c)


def test_saturated_blue_is_not_treated_as_dark():
    # pure blue has luma ~0.07; it must still count as a lit pixel
    res = dominant_colors(_img(color=BLUE))
    assert len(res) == 1 and _close(res[0][0], BLUE)


# ---- distribute ------------------------------------------------------------

def test_distribute_counts():
    a, b, c = (255, 0, 0), (0, 255, 0), (0, 0, 255)
    frame = distribute([(a, 0.5), (b, 0.3), (c, 0.2)], 14)
    assert len(frame) == 14
    counts = Counter(frame)
    assert counts[a] == 7 and counts[b] == 4 and counts[c] == 3


def test_distribute_sorted_by_hue_contiguous():
    blue, red, green = (0, 0, 255), (255, 0, 0), (0, 255, 0)
    frame = distribute([(blue, 0.5), (red, 0.3), (green, 0.2)], 10)
    # contiguous blocks ordered red (0deg) -> green (120) -> blue (240)
    assert frame == [red] * 3 + [green] * 2 + [blue] * 5


def test_distribute_more_colors_than_segments():
    colors = [((i * 40, 0, 0), p) for i, p in enumerate([0.3, 0.25, 0.2, 0.15, 0.1])]
    frame = distribute(colors, 3)
    assert len(frame) == 3
    assert set(frame) == {(0, 0, 0), (40, 0, 0), (80, 0, 0)}


def test_distribute_tiny_share_still_gets_a_segment():
    a, b = (255, 0, 0), (0, 0, 255)
    frame = distribute([(a, 0.99), (b, 0.01)], 14)
    counts = Counter(frame)
    assert counts[b] == 1 and counts[a] == 13


# ---- adjust ----------------------------------------------------------------

def test_saturation_boost_and_gain():
    base = (200, 120, 120)
    boosted = adjust(base, 1.5, (1.0, 1.0, 1.0))
    assert _sat(boosted) > _sat(base)
    assert max(boosted) == pytest.approx(200, abs=1)  # value preserved
    assert adjust(base, 1.0, (1.0, 1.0, 1.0)) == base
    assert adjust((100, 100, 100), 1.0, (1.0, 0.5, 2.0)) == (100, 50, 200)
    assert adjust((200, 200, 200), 1.0, (2.0, 1.0, 1.0)) == (255, 200, 200)
    assert adjust((128, 128, 128), 3.0, (1.0, 1.0, 1.0)) == (128, 128, 128)  # gray stays gray
    assert adjust((200, 120, 120), 0.0, (1.0, 1.0, 1.0)) == (200, 200, 200)


# ---- vibrance ---------------------------------------------------------------

def test_default_saturation_is_vivid():
    assert screen.DEFAULT_SETTINGS["saturation"] == screen.DEFAULT_SATURATION == 2.0


def test_vibrance_lifts_pastel_more_than_saturated_and_keeps_grey():
    pastel, skin, vivid = (169, 200, 220), (223, 172, 140), (230, 40, 30)
    b = screen.DEFAULT_SATURATION
    lift = {c: _sat(adjust(c, b, (1, 1, 1))) / _sat(c) for c in (pastel, skin, vivid)}
    assert lift[pastel] > lift[skin] > lift[vivid] > 1.0
    assert _sat(adjust(pastel, b, (1, 1, 1))) >= 0.4       # pastel clearly coloured
    out = adjust(vivid, b, (1, 1, 1))
    assert _sat(out) < 0.97 and min(out) > 0                # no clipping to a pure primary
    for grey in [(128, 128, 130), (120, 128, 135), (200, 200, 198)]:  # all below GREY_SATURATION
        assert adjust(grey, 3.0, (1, 1, 1)) == grey
    # continuous just above the grey threshold (no sudden colour out of near-grey)
    s0 = screen.GREY_SATURATION
    lo = colorsys.hsv_to_rgb(0.6, s0 + 0.005, 0.8)
    near = adjust(tuple(round(c * 255) for c in lo), b, (1, 1, 1))
    assert _sat(near) < s0 + 0.03


# ---- Smoother: time-constant crossfade in linear light -------------------------

def _lin_frac(out, a, b, ch):
    from govee_win11.effects import _LIN
    return (_LIN[out[ch]] - _LIN[a[ch]]) / (_LIN[b[ch]] - _LIN[a[ch]])


def _run_smoother(tau, a, b, fps, seconds):
    s = Smoother(tau)
    s.step([a], 1 / fps)
    out = None
    for _ in range(int(round(seconds * fps))):
        out = s.step([b], 1 / fps)[0]
    return out


def test_transition_time_maps_to_time_constant():
    # 过渡时间 T = time for a change to be ~95 % there: tau = T / 3
    assert screen.transition_tau(1.5) == pytest.approx(0.5)
    assert screen.transition_tau(5.0) == pytest.approx(5.0 / 3)
    assert screen.transition_tau(0.3) == pytest.approx(0.1)
    # scene cuts follow the same slider: fade time max(0.3, T / 2)
    assert screen.cut_fade(1.5) == pytest.approx(0.75)
    assert screen.cut_fade(5.0) == pytest.approx(2.5)
    assert screen.cut_fade(0.3) == pytest.approx(0.3)
    assert screen.cut_fade(0.5) == pytest.approx(0.3)
    assert screen.DEFAULT_SETTINGS["transition_s"] == screen.DEFAULT_TRANSITION == 1.5
    assert "smoothing" not in screen.DEFAULT_SETTINGS
    assert (screen.TRANSITION_MIN, screen.TRANSITION_MAX) == (0.3, 5.0)


def _run_transition(T, a, b, fps, seconds):
    s = Smoother.for_transition(T)
    s.step([a], 1 / fps)
    out = None
    for _ in range(int(round(seconds * fps))):
        out = s.step([b], 1 / fps)[0]
    return out


def test_longer_transition_time_is_slower():
    a, b = (200, 60, 60), (200, 100, 60)  # a moderate change (no scene cut)
    for T in (0.3, 1.5, 3.0, 5.0):
        assert _lin_frac(_run_transition(T, a, b, 10, T), a, b, 1) >= 0.93    # ~95 % at T
        assert _lin_frac(_run_transition(T, a, b, 10, T / 3), a, b, 1) < 0.7
    # 5 s is perceptibly longer than the default: at 1.5 s still well short
    assert _lin_frac(_run_transition(5.0, a, b, 10, 1.5), a, b, 1) < 0.65


def test_scene_cut_fade_follows_transition_time():
    a, b = (255, 0, 0), (0, 0, 255)
    # long setting softens cuts too: 2.5 s
    assert _blue_share(_run_transition(5.0, a, b, 10, 0.8)) < 0.9
    assert _blue_share(_run_transition(5.0, a, b, 10, 2.5)) >= 0.9
    # default: ~0.75 s, as before
    assert _blue_share(_run_transition(1.5, a, b, 10, 0.4)) < 0.9
    assert _blue_share(_run_transition(1.5, a, b, 10, 0.8)) >= 0.9
    # shortest: 0.3 s
    assert _blue_share(_run_transition(0.3, a, b, 30, 0.1)) < 0.9
    assert _blue_share(_run_transition(0.3, a, b, 30, 0.3)) >= 0.9


def test_mode_uses_transition_setting():
    m = ScreenSyncMode(4, {**DEFAULTS, "transition_s": 4.2}, None, grab=lambda: _img())
    assert m.transition_s == pytest.approx(4.2) and m.tau == pytest.approx(1.4)
    m.open()
    assert m.smoother.tau == pytest.approx(1.4)
    assert m.smoother.cut_tau == pytest.approx(2.1 / 3)
    assert m.smoother.cut_hold == pytest.approx(2.1)
    m.close()
    assert ScreenSyncMode(4, {"transition_s": 99}, None).transition_s == 5.0
    assert ScreenSyncMode(4, {"transition_s": 0}, None).transition_s == 0.3
    assert ScreenSyncMode(4, {"transition_s": "x"}, None).transition_s == 1.5


def test_smoothing_is_fps_independent_and_settles_in_1_5_s():
    tau = screen.transition_tau(screen.DEFAULT_TRANSITION)
    a, b = (200, 60, 60), (200, 100, 60)  # a moderate change (no scene cut)
    one = _run_smoother(tau, a, b, 10, 0.1)
    assert _lin_frac(one, a, b, 1) < 0.5                    # not in one frame
    at10 = _run_smoother(tau, a, b, 10, 1.5)
    at30 = _run_smoother(tau, a, b, 30, 1.5)
    assert _lin_frac(at10, a, b, 1) >= 0.9
    assert _close(at10, at30, tol=1)                        # same curve at any frame rate
    assert _close(_run_smoother(tau, a, b, 10, 0.6), _run_smoother(tau, a, b, 25, 0.6), tol=1)


def _blue_share(px):
    """Red light fading out, blue fading in (peak-preserved): the share of blue light."""
    from govee_win11.effects import _LIN
    x = _LIN[px[0]] / _LIN[px[2]]  # = old / new light
    return 1 / (1 + x)


def test_scene_cut_fades_in_about_0_7_s_never_jumps():
    tau = screen.transition_tau(screen.DEFAULT_TRANSITION)
    a, b = (255, 0, 0), (0, 0, 255)
    for fps in (10, 30):
        assert _blue_share(_run_smoother(tau, a, b, fps, 0.1)) < 0.5
        assert _blue_share(_run_smoother(tau, a, b, fps, 0.4)) < 0.9
        assert _blue_share(_run_smoother(tau, a, b, fps, 0.8)) >= 0.9
    # with the slowest setting a cut still completes within ~0.8 s
    assert _blue_share(_run_smoother(1.5, a, b, 10, 0.8)) >= 0.9


def test_crossfade_is_additive_light_not_hue_interpolation():
    s = Smoother(0.5)
    s.step([(255, 0, 0)], 0.1)
    mids = [s.step([(0, 0, 255)], 0.1)[0] for _ in range(10)]
    for px in mids:
        assert px[1] == 0                       # red + blue light: never via green/yellow
        assert max(px) >= 250                   # peak-preserving overlap: no dim dip


def test_smoothing_zero_tau_is_passthrough():
    s = Smoother(0.0)
    s.step([(255, 0, 0)], 0.1)
    assert s.step([(0, 0, 255)], 0.1) == [(0, 0, 255)]


def test_smoother_resets_on_length_change():
    s = Smoother(1.0)
    s.step([(255, 255, 255)] * 3, 0.1)
    assert s.step([(0, 0, 0)] * 5, 0.1) == [(0, 0, 0)] * 5


def test_mode_uses_frame_time_not_frame_count():
    def run(fps):
        st = {"c": (200, 60, 60)}
        m = ScreenSyncMode(4, DEFAULTS, None, grab=lambda: _img(color=st["c"]))
        m.open()
        m.next_frame(0.0)
        st["c"] = (200, 100, 60)
        out = None
        for k in range(1, int(round(0.3 * fps)) + 1):
            out = m.next_frame(k / fps)
        m.close()
        return out[0]
    assert _close(run(10), run(20), tol=2)


# ---- region_for_point ------------------------------------------------------

def _valid_rect(r):
    x0, y0, x1, y1 = r
    assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1


def test_region_for_point_desk_maps_to_bottom():
    r = region_for_point(0.5, 0.9)
    _valid_rect(r)
    x0, y0, x1, y1 = r
    assert y0 == pytest.approx(0.85) and y1 == pytest.approx(1.0)
    assert x1 - x0 == pytest.approx(0.2)
    assert (x0 + x1) / 2 == pytest.approx(0.5)
    # desk point far left clamps inside the screen, keeping 20% width
    x0, y0, x1, y1 = region_for_point(0.0, 1.0)
    assert x0 == pytest.approx(0.0) and x1 == pytest.approx(0.2)


def test_region_for_point_monitor_left_edge():
    # x=0.16 is just inside the monitor's left edge, y=0.35 is its vertical middle
    r = region_for_point(0.16, 0.35)
    _valid_rect(r)
    x0, y0, x1, y1 = r
    assert x0 == pytest.approx(0.0) and x1 == pytest.approx(0.1)
    assert y1 - y0 == pytest.approx(0.2)
    assert (y0 + y1) / 2 == pytest.approx(0.5)


def test_region_for_point_other_edges():
    x0, y0, x1, y1 = region_for_point(0.84, 0.35)  # right
    assert (x0, x1) == pytest.approx((0.9, 1.0))
    x0, y0, x1, y1 = region_for_point(0.5, 0.01)  # top
    assert (y0, y1) == pytest.approx((0.0, 0.1))
    assert (x0 + x1) / 2 == pytest.approx(0.5)
    x0, y0, x1, y1 = region_for_point(0.3, 0.69)  # bottom
    assert (y0, y1) == pytest.approx((0.9, 1.0))
    # top-left corner: strip clamped inside the screen
    x0, y0, x1, y1 = region_for_point(0.15, 0.0)
    _valid_rect((x0, y0, x1, y1))


def test_region_for_point_outside_monitor_uses_nearest_edge():
    x0, y0, x1, y1 = region_for_point(0.02, 0.35)  # beside the monitor, left
    assert (x0, x1) == pytest.approx((0.0, 0.1))
    _valid_rect(region_for_point(0.5, 0.75))  # gap between monitor and desk
    _valid_rect(region_for_point(1.0, 0.0))


# ---- ScreenSyncMode --------------------------------------------------------

def test_mode_with_fake_grab():
    img = _img(144, 344)
    img[:, :200] = RED
    img[:, 200:] = BLUE
    m = ScreenSyncMode(14, DEFAULTS, None, grab=lambda: img)
    assert m.fps == 10
    m.open()
    try:
        f = m.next_frame(0.0)
        assert len(f) == 14
        assert all(len(px) == 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in px) for px in f)
        reds = sum(1 for px in f if px[0] > 200 and px[2] < 50)
        blues = sum(1 for px in f if px[2] > 200 and px[0] < 50)
        assert reds >= 7 and blues >= 5
    finally:
        m.close()


def test_dark_image_dims_output():
    bright = _img(color=(0, 200, 0))
    dark = _img()
    dark[:6, :] = (0, 200, 0)  # 10% lit, 90% black
    out = {}
    for name, img in (("bright", bright), ("dark", dark)):
        m = ScreenSyncMode(10, {**DEFAULTS, "transition_s": 0.45}, None, grab=lambda img=img: img)
        m.open()
        out[name] = m.next_frame(0)
        m.close()
    assert max(px[1] for px in out["bright"]) >= 190
    assert max(px[1] for px in out["dark"]) <= 0.2 * 200
    assert max(px[1] for px in out["dark"]) > 0


def test_black_capture_gives_black_frame():
    # DRM-protected video captures as black: strip just goes dark
    m = ScreenSyncMode(14, {**DEFAULTS, "transition_s": 0.45}, None, grab=lambda: _img())
    m.open()
    assert m.next_frame(0) == [(0, 0, 0)] * 14
    m.close()


def test_grab_failure_fades_to_black_without_raising():
    calls = {"n": 0}

    def grab():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("capture failed")
        return _img(color=(255, 255, 255))

    m = ScreenSyncMode(5, {**DEFAULTS, "transition_s": 1.4}, None, grab=grab)
    m.open()
    first = m.next_frame(0)
    second = m.next_frame(0.1)
    assert len(second) == 5
    assert second[0][0] < first[0][0]
    m.close()


def test_missing_settings_keys_use_defaults():
    m = ScreenSyncMode(14, {}, None, grab=lambda: _img(color=RED))
    assert m.monitor == 1 and m.saturation == 2.0 and m.mode == "dominant"
    assert m.gain == (1.0, 1.0, 1.0)
    assert m.transition_s == 1.5 and m.tau == pytest.approx(0.5)


def _drift_frame(i, kind):
    yy, xx = np.mgrid[0:42, 0:99].astype(float)
    if kind == "blobs":  # slowly drifting colour field, like an animated wallpaper
        r = 128 + 100 * np.sin(xx / 15 + i * 0.05)
        g = 128 + 100 * np.sin(yy / 9 + xx / 30 - i * 0.04)
        b = 128 + 100 * np.cos((xx + yy) / 20 + i * 0.03)
    else:  # muted dusk scene: near-grey colours with unstable hue
        base = 90 + 40 * np.sin(xx / 25 + i * 0.05)
        r = base + 12 * np.sin(yy / 7 + i * 0.07)
        g = base + 6 * np.cos(xx / 11)
        b = base + 10 * np.sin((xx - yy) / 13 - i * 0.05)
    return np.clip(np.stack([r, g, b], -1), 0, 255).astype(np.uint8)


def _max_step(a, b):
    return max(max(abs(x - y) for x, y in zip(p, q)) for p, q in zip(a, b))


# Bounds from measurements over 20 start phases (0..380 step 20, 200 frames
# each) of the synthetic fields, counting segment-steps > 30/255 after
# smoothing 0.6:
#   blobs: pre-fix (k-means re-seeded + hue re-sort every frame) 519-688,
#          mean 605; this implementation 88-156, mean 119 (phases 0/160/320
#          used here: 109/130/133).
#   muted: pre-fix 5-15, mean 7.5 (phase 140 used here: 15); this
#          implementation 0-5, mean 0.5 (phase 140: 0).
# Caps leave ~25% headroom over the worst observed phase and sit far below
# the pre-fix behaviour; the mean bound is what catches a regression.
DRIFT_PHASES = {"blobs": [0, 160, 320], "muted": [140]}
DRIFT_BOUNDS = {"blobs": (200, 150), "muted": (10, 3)}  # (per-phase cap, mean cap)


@pytest.mark.parametrize("kind", ["blobs", "muted"])
def test_slowly_changing_image_does_not_flicker(kind):
    # content moves <= ~5/255 per frame; the strip must follow without jumping.
    # What may remain is a boundary segment changing hands now and then and,
    # rarely, one re-sort of the block order, both faded by the smoother.
    per_phase = []
    for phase in DRIFT_PHASES[kind]:
        state = {"i": 0}
        m = ScreenSyncMode(14, DEFAULTS, None, grab=lambda: _drift_frame(state["i"] + phase, kind))
        m.open()
        frames = []
        for i in range(200):
            state["i"] = i
            frames.append(m.next_frame(i / 10))
        m.close()
        arr = np.array(frames, dtype=int)
        seg_steps = np.abs(np.diff(arr, axis=0)).max(axis=2)  # (frames-1, segments)
        per_phase.append(int((seg_steps > 30).sum()))
    cap, mean_cap = DRIFT_BOUNDS[kind]
    assert max(per_phase) <= cap, per_phase
    assert np.mean(per_phase) <= mean_cap, per_phase


def test_noisy_static_image_is_steady():
    # capture noise alone must not move the strip once it has settled
    base = _drift_frame(40, "blobs").astype(int)
    rng = np.random.default_rng(3)
    m = ScreenSyncMode(14, DEFAULTS, None, grab=lambda: np.clip(base + rng.integers(-3, 4, base.shape), 0, 255).astype(np.uint8))
    m.open()
    frames = [m.next_frame(i / 10) for i in range(100)]
    m.close()
    steps = [_max_step(a, b) for a, b in zip(frames[11:], frames[10:-1])]
    assert max(steps) <= 10, max(steps)


def test_open_resets_tracking_state():
    imgs = [_drift_frame(0, "blobs"), _drift_frame(100, "blobs")]
    m = ScreenSyncMode(14, DEFAULTS, None, grab=lambda: imgs[0])
    m.open()
    for _ in range(5):
        m.next_frame(0)
    m.close()
    imgs[0] = imgs[1]
    m.open()
    restarted = m.next_frame(0)
    m.close()
    fresh = ScreenSyncMode(14, DEFAULTS, None, grab=lambda: imgs[1])
    fresh.open()
    assert restarted == fresh.next_frame(0)


def test_distribute_grey_hue_is_stable():
    red = (200, 60, 60)
    a = distribute([((120, 120, 121), 0.5), (red, 0.5)], 4)
    b = distribute([((121, 120, 120), 0.5), (red, 0.5)], 4)
    assert [px == red for px in a] == [px == red for px in b]


# ---- mss integration (faked, never touches the real screen) ----------------

class _FakeShot:
    def __init__(self, w, h):
        bgra = np.zeros((h, w, 4), dtype=np.uint8)
        bgra[..., 2] = 255  # red in BGRA
        self.raw = bgra.tobytes()
        self.width, self.height = w, h

    @property
    def bgra(self):
        return self.raw


class _FakeSct:
    instances = []

    def __init__(self):
        self.monitors = [
            {"left": 0, "top": 0, "width": 640, "height": 240},
            {"left": 0, "top": 0, "width": 320, "height": 240, "is_primary": True},
            {"left": 320, "top": 0, "width": 320, "height": 200},
        ]
        self.grabbed = []
        self.closed = False
        self.thread = threading.get_ident()
        _FakeSct.instances.append(self)

    def grab(self, mon):
        assert threading.get_ident() == self.thread
        self.grabbed.append(mon)
        return _FakeShot(mon["width"], mon["height"])

    def close(self):
        self.closed = True


@pytest.fixture
def fake_mss(monkeypatch):
    _FakeSct.instances = []
    monkeypatch.setattr(screen, "_new_sct", lambda: _FakeSct())
    return _FakeSct


def test_list_monitors(fake_mss):
    mons = screen.list_monitors()
    assert mons == [
        {"index": 1, "width": 320, "height": 240, "primary": True},
        {"index": 2, "width": 320, "height": 200, "primary": False},
    ]
    assert fake_mss.instances[0].closed


def test_mss_capture_and_monitor_fallback(fake_mss):
    m = ScreenSyncMode(6, {**DEFAULTS, "monitor": 7, "transition_s": 0.45}, None)
    m.open()
    f = m.next_frame(0)
    assert all(_close(px, RED) for px in f)
    sct = fake_mss.instances[0]
    assert sct.grabbed[0] == sct.monitors[1]  # index 7 does not exist -> monitor 1
    m.close()
    assert sct.closed


def test_mss_selected_monitor(fake_mss):
    m = ScreenSyncMode(6, {**DEFAULTS, "monitor": 2}, None)
    m.open()
    m.next_frame(0)
    assert fake_mss.instances[0].grabbed[0]["left"] == 320
    m.close()


def test_mss_next_frame_on_other_thread_creates_own_instance(fake_mss):
    m = ScreenSyncMode(6, {**DEFAULTS, "transition_s": 0.45}, None)
    m.open()
    result = {}

    def worker():
        result["frame"] = m.next_frame(0)
        result["frame2"] = m.next_frame(0.1)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert len(result["frame"]) == 6 and all(_close(px, RED) for px in result["frame"])
    assert len(fake_mss.instances) == 2  # one for open(), one for the worker thread only
    m.close()
    assert all(s.closed for s in fake_mss.instances)


def test_zero_segments():
    m = ScreenSyncMode(0, DEFAULTS, None, grab=lambda: _drift_frame(0, "blobs"))
    m.open()
    assert m.next_frame(0) == []
    m.close()


_SCENES = {
    "bluegreen": ([(20, 60, 200), (30, 160, 90), (10, 120, 180), (60, 200, 200), (20, 90, 40)], [0.3, 0.25, 0.2, 0.15, 0.1]),
    "warm": ([(220, 40, 30), (240, 180, 30), (250, 250, 240), (150, 40, 160), (60, 60, 220)], [0.3, 0.25, 0.2, 0.15, 0.1]),
    "two": ([(220, 40, 30), (30, 40, 220)], [0.6, 0.4]),
    "five": ([(220, 40, 30), (30, 200, 40), (30, 40, 220), (240, 230, 40), (200, 200, 200)], [0.2] * 5),
}


def _patches(name, rng):
    cols, shares = _SCENES[name]
    img = np.zeros((42, 99, 3), float)
    x0 = 0
    for c, sh in zip(cols, shares):
        x1 = x0 + int(round(sh * 99))
        img[:, x0:x1] = c
        x0 = x1
    img[:, x0:] = cols[-1]
    return np.clip(img + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)


def _hue_inversions(frame):
    """How badly the blocks miss a gradient: the hue-wheel distance the
    chromatic blocks travel (either direction, backward steps < 0.05 = ties)
    minus that of the best order (largest hue gap at the strip ends), in
    units of 0.1 turn, ignoring excess below 0.1."""
    hues = []
    for c in frame:
        h, sat, _v = colorsys.rgb_to_hsv(*(x / 255 for x in c))
        if sat >= 0.15 and (not hues or hues[-1] != h):
            hues.append(h)
    if len(hues) < 3:
        return 0

    def turn(seq):
        return sum(0 if (b - a) % 1 > 0.95 else (b - a) % 1 for a, b in zip(seq, seq[1:]))

    srt = sorted(hues)
    best = 1 - max((srt[(j + 1) % len(srt)] - srt[j]) % 1 for j in range(len(srt)))
    excess = min(turn(hues), turn(hues[::-1])) - best
    return 0 if excess <= 0.1 else int(excess / 0.1)


@pytest.mark.parametrize("a,b", [("bluegreen", "warm"), ("warm", "bluegreen"), ("two", "five")])
def test_hard_scene_cut_recovers_quickly(a, b):
    # Wallpaper playlists cut between scenes; warm-started k-means must not
    # stay stuck on the old scene's colours.
    rng = np.random.default_rng(1)
    scene = {"name": a}
    m = _raw(ScreenSyncMode(20, {**DEFAULTS, "transition_s": 0.45, "saturation": 1.0}, None, grab=lambda: _patches(scene["name"], rng)))
    m.open()
    for i in range(20):
        m.next_frame(i / 10)
    scene["name"] = b
    for i in range(6):  # detection runs every 3rd frame
        f = m.next_frame(i / 10)
    m.close()
    blocks = np.array(sorted(set(f)), float)
    for true in _SCENES[b][0]:
        assert np.sqrt(((blocks - true) ** 2).sum(axis=1)).min() <= 25, (true, sorted(set(f)))
    assert _hue_inversions(f) == 0, f


@pytest.mark.parametrize("a,b", [("bluegreen", "warm"), ("warm", "bluegreen"), ("two", "five"), ("bluegreen", "five")])
@pytest.mark.parametrize("fade", [0, 20])
def test_block_order_follows_scene_change(a, b, fade):
    # after a cut or a crossfade, the gradient order must settle again
    rng = np.random.default_rng(2)
    st = {"w": 0.0}

    def grab():
        img_a = _patches(a, rng).astype(float)
        return ((1 - st["w"]) * img_a + st["w"] * _patches(b, rng)).astype(np.uint8)

    m = _raw(ScreenSyncMode(20, {**DEFAULTS, "transition_s": 0.45, "saturation": 1.0}, None, grab=grab))
    m.open()
    for i in range(20):
        m.next_frame(0)
    for k in range(fade):
        st["w"] = (k + 1) / (fade + 1)
        m.next_frame(0)
    st["w"] = 1.0
    for i in range(60):
        f = m.next_frame(0)
    m.close()
    blocks = np.array(sorted(set(f)), float)
    for true in _SCENES[b][0]:
        assert np.sqrt(((blocks - true) ** 2).sum(axis=1)).min() <= 25, (true, sorted(set(f)))
    assert _hue_inversions(f) == 0, f


_CUT_PAIRS = [(a, b) for a in _SCENES for b in _SCENES if a != b]


@pytest.mark.parametrize("idx", range(len(_CUT_PAIRS)), ids=[f"{a}-{b}" for a, b in _CUT_PAIRS])
def test_hard_cut_restores_block_order_within_a_few_frames(idx):
    # Warm k-means often finds the new scene's colours by itself, so the
    # re-seed detector does not fire; the block order must still be put back
    # into gradient order right away, not after the 5 s drift delay. The
    # scene length before the cut varies so the cut falls on each phase of
    # the every-3rd-frame re-seed check.
    a, b = _CUT_PAIRS[idx]
    rng = np.random.default_rng(idx)
    scene = {"name": a}
    m = _raw(ScreenSyncMode(20, {**DEFAULTS, "transition_s": 0.45, "saturation": 1.0}, None, grab=lambda: _patches(scene["name"], rng)))
    m.open()
    for _ in range(10 + idx % 3):
        m.next_frame(0)
    scene["name"] = b
    frames = [m.next_frame(0) for _ in range(8)]
    m.close()
    bad = [i + 1 for i, f in enumerate(frames) if _hue_inversions(f) > 0]
    assert not bad or max(bad) <= 2, (bad, frames[-1])
    blocks = np.array(sorted(set(frames[4])), float)  # colours by the 5th frame
    for true in _SCENES[b][0]:
        assert np.sqrt(((blocks - true) ** 2).sum(axis=1)).min() <= 25, (true, sorted(set(frames[4])))


def test_vanished_colour_does_not_keep_order_flagged():
    # 5 colours -> 4 (one leaves): its cluster goes empty but keeps its old
    # centre. That stale centre must not take part in the re-sort, or the new
    # order is not the narrowest one, stays flagged out of order and is
    # "re-sorted" every 5 s forever, each time dropping the count hysteresis.
    def scene(hues):
        img = np.zeros((42, 99, 3), np.uint8)
        w = 99 // len(hues)
        for b, h in enumerate(hues):
            img[:, b * w:(b + 1) * w if b < len(hues) - 1 else 99] = _hsv255(h, 0.9, 0.9)
        return img

    st = {"hues": [0.0, 0.3, 0.4, 0.5, 0.75]}
    m = ScreenSyncMode(14, DEFAULTS, None, grab=lambda: scene(st["hues"]))
    m.open()
    for _ in range(20):
        m.next_frame(0)
    st["hues"] = [0.0, 0.3, 0.4, 0.5]
    for i in range(30):
        f = m.next_frame(0)
        if i >= 3:
            assert m._unordered_frames == 0, i
    m.close()
    assert _hue_inversions(f) == 0, f


def _hsv255(h, s_, v):
    return tuple(int(round(c * 255)) for c in colorsys.hsv_to_rgb(h % 1.0, s_, v))


def test_out_of_order_is_cyclic():
    key = lambda h: screen._hue_key(_hsv255(h, 0.9, 0.9))
    red, green, blue = key(0.0), key(1 / 3), key(2 / 3)
    assert not screen._out_of_order([red, green, blue])
    assert not screen._out_of_order([red, blue])  # big forward gap is fine
    assert not screen._out_of_order([green, blue, red])  # rotation = still a gradient
    assert not screen._out_of_order([key(0.88), key(0.92), key(0.05)])  # across the 0.9 cut
    assert not screen._out_of_order([key(0.5), key(0.49), key(0.6)])  # within the margin
    assert not screen._out_of_order([red, blue, green])  # backwards via magenta, cyan
    assert not screen._out_of_order([blue, green, red])  # reversed gradient
    assert screen._out_of_order([key(0.1), key(0.5), key(0.3)])
    # an arc of blues/greens with the big jump mid-strip is not a gradient
    assert screen._out_of_order([key(0.56), key(0.63), key(0.41), key(0.38), key(0.5)])
    assert not screen._out_of_order([key(0.38), key(0.41), key(0.5), key(0.56), key(0.63)])
    grey = screen._hue_key((128, 128, 130))
    assert not screen._out_of_order([grey, red, grey, green, blue])  # greys don't break it


def test_gradient_order_puts_widest_gap_at_the_ends():
    key = lambda h: screen._hue_key(_hsv255(h, 0.9, 0.9))
    hues = [0.9, 0.05, 0.15, 0.55, 0.7]  # widest gap 0.15 -> 0.55
    order = screen._gradient_order([key(h) for h in hues])
    assert [hues[i] for i in order] == [0.55, 0.7, 0.9, 0.05, 0.15]


def _run_counting_resorts(n, grab_for_frame, frames):
    st = {"i": 0}
    m = ScreenSyncMode(n, DEFAULTS, None, grab=lambda: grab_for_frame(st["i"]))
    m.open()
    out, resorts = [], 0
    for i in range(frames):
        st["i"] = i
        before = list(m._order)
        out.append(m.next_frame(i / 10))
        if i > 0 and m._order != before:
            resorts += 1
    m.close()
    steps = np.abs(np.diff(np.array(out, int), axis=0)).max(axis=2)
    return resorts, steps


@pytest.mark.parametrize("period", [100, 300])  # full hue cycle in 10 s / 30 s
def test_rotating_hue_bands_never_reshuffle(period):
    # RGB-wave wallpaper: 5 hue bands, all hues rotating; a colour crossing the
    # sort's cut point must not count as out of order.
    def grab(i):
        img = np.zeros((42, 99, 3), np.uint8)
        for b in range(5):
            img[:, b * 20:(b + 1) * 20 if b < 4 else 99] = _hsv255(b / 5 + i / period, 0.9, 0.9)
        return img

    resorts, steps = _run_counting_resorts(14, grab, 200)  # 4x the re-sort delay
    assert resorts == 0
    assert steps.max() <= 20  # content itself moves <= ~12/255 per frame


@pytest.mark.parametrize("amp", [0.02, 0.04])
def test_magenta_swaying_across_cut_is_steady(amp):
    rng = np.random.default_rng(0)

    def grab(i):
        hues = [0.9 + amp * np.sin(2 * np.pi * i / 200), 0.05, 0.15, 0.55, 0.7]
        img = np.zeros((42, 99, 3), float)
        for b, h in enumerate(hues):
            img[:, b * 20:(b + 1) * 20 if b < 4 else 99] = _hsv255(h, 0.8, 0.9)
        return np.clip(img + rng.normal(0, 4, img.shape), 0, 255).astype(np.uint8)

    resorts, steps = _run_counting_resorts(14, grab, 200)
    assert resorts == 0
    assert steps.max() <= 10


def test_static_two_colour_scene_is_not_resorted():
    img = _img(color=(30, 40, 220))
    img[:, :120] = (220, 40, 30)
    m = ScreenSyncMode(14, DEFAULTS, None, grab=lambda: img)
    m.open()
    for i in range(100):
        m.next_frame(i / 10)
        assert m._unordered_frames == 0
    m.close()


_YY, _XX = np.mgrid[0:42, 0:99].astype(float)


def _video_shots(t, speed, shot_len=30):
    # Video wallpaper: a hard shot change every `shot_len` frames, fast motion
    # of 4 coloured layers within each shot (palette moves ~20-45 RGB/frame).
    r = np.random.default_rng(t // shot_len)
    hues, ph = r.random(4), r.random(4) * 6.28
    img = np.zeros((42, 99, 3))
    for j in range(4):
        a = (0.5 + 0.5 * np.sin(_XX / 12 + _YY / (8 + j) + speed * 0.2 * t * (1 + j * 0.3) + ph[j])) ** 3
        img += np.array(_hsv255(hues[j], 0.8, 0.9), float) * a[..., None] / 1.6
    return img


def _order_changes(n, grab_for_frame, frames):
    rng = np.random.default_rng(0)
    st = {"i": 0}
    m = ScreenSyncMode(n, DEFAULTS, None, grab=lambda: np.clip(
        grab_for_frame(st["i"]) + rng.normal(0, 2, (42, 99, 3)), 0, 255).astype(np.uint8))
    m.open()
    changed = []
    for i in range(frames):
        st["i"] = i
        before = list(m._order)
        m.next_frame(i / 10)
        changed.append(i > 0 and m._order != before)
    m.close()
    return changed


def test_video_cuts_resort_once_without_cascade():
    # Fast motion keeps the warm clusters swapping roles, so the order is often
    # "out of gradient order" right after a cut too. A cut may re-sort the
    # strip once; it must not keep re-sorting it on every following frame.
    # (454a7fb: 3-6 re-sorts in the first 10 frames of a shot, 47 in total.)
    changed = _order_changes(15, lambda t: _video_shots(t, 6), 210)
    first_frames = [sum(changed[k:k + 10]) for k in range(30, 210, 30)]
    per_shot = [sum(changed[k:k + 30]) for k in range(30, 210, 30)]
    assert max(first_frames) <= 1, first_frames
    assert sum(changed) <= len(per_shot) + 2, per_shot


def test_very_fast_drift_does_not_resort_every_cut_window():
    # Blobs drifting 20x faster than the drift test: the palette moves 40-70
    # RGB every frame, i.e. often above CUT_JUMP. That is motion, not a stream
    # of cuts, so the order must stay about as stable as at normal speed.
    # (454a7fb: 95 order changes in these 150 frames.)
    changed = _order_changes(15, lambda t: _drift_frame(20 * t, "blobs").astype(float), 150)
    assert sum(changed) <= 3


def test_requirements_pin_mss_with_bgra_raw_api():
    """screen.py relies on mss >= 10.2 (ScreenShot.raw / is_primary); pinned."""
    from pathlib import Path
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "mss>=10.2" in [line.strip() for line in req.splitlines()]


# ---- Task 16 modes: region (6 columns), fade, projection -------------------------

from govee_win11 import effects  # noqa: E402
from govee_win11.effects import _light, _overlap  # noqa: E402

GREEN = (0, 255, 0)


def _cols(colours, h=60, w=240):
    """Image cut into len(colours) equal vertical columns, left to right."""
    img = np.zeros((h, w, 3), np.uint8)
    k = len(colours)
    for j, c in enumerate(colours):
        img[:, j * w // k:(j + 1) * w // k] = c
    return img


def _run_mode(mode, grab, seconds, fps=10, n=15, **extra):
    m = ScreenSyncMode(n, {**DEFAULTS, "mode": mode, **extra}, None, grab=grab)
    m.open()
    out = [(k / fps, m.next_frame(k / fps)) for k in range(int(round(seconds * fps)) + 1)]
    m.close()
    return out


def _is(px, c, tol=12):
    return _close(px, c, tol)


def test_region_red_left_blue_right():
    img = _cols([RED, BLUE])
    m = _raw(ScreenSyncMode(15, {**DEFAULTS, "mode": "region"}, None, grab=lambda: img))
    m.open()
    f = m.next_frame(0)
    m.close()
    assert all(px[0] > 200 and px[2] < 60 for px in f[:6]), f
    assert all(px[2] > 200 and px[0] < 60 for px in f[9:]), f


def test_region_follows_screen_position_unlike_dominant():
    # red / blue alternating columns: region shows the alternation where it is on the
    # screen, dominant mode lays the two colours out as two contiguous blocks
    img = _cols([RED, BLUE] * 3)
    frames = {}
    for mode in ("dominant", "region"):
        m = _raw(ScreenSyncMode(15, {**DEFAULTS, "mode": mode}, None, grab=lambda: img))
        m.open()
        frames[mode] = m.next_frame(0)
        m.close()

    def boundaries(f):
        kinds = ["r" if px[0] > px[2] else "b" for px in f]
        return sum(1 for a, b in zip(kinds, kinds[1:]) if a != b)

    reg = frames["region"]
    assert _is(reg[0], RED) and _is(reg[3], BLUE) and _is(reg[5], RED) and _is(reg[14], BLUE)
    assert boundaries(reg) == 5
    assert boundaries(frames["dominant"]) == 1


def test_region_column_straddle_is_additive_light_overlap():
    six = [(255, 0, 0), (0, 200, 0), (0, 0, 255), (255, 200, 0), (0, 200, 200), (200, 0, 200)]
    img = _cols(six)
    m = _raw(ScreenSyncMode(15, {**DEFAULTS, "mode": "region", "saturation": 1.0}, None,
                            grab=lambda: img))
    m.open()
    f = m.next_frame(0)
    m.close()
    # 15 / 6 = 2.5 segments per column: segment 2 is half column 0, half column 1
    assert f[0] == f[1] == six[0] and f[3] == f[4] == six[1]
    assert f[2] == _light([(six[0], 0.5), (six[1], 0.5)])
    assert f[7] == _light([(six[2], 0.5), (six[3], 0.5)])
    assert f[12] == _light([(six[4], 0.5), (six[5], 0.5)])
    assert f[14] == six[5]


def test_region_bright_object_on_dark_background_is_not_muddy():
    img = np.zeros((60, 240, 3), np.uint8)
    img[:, :] = (35, 35, 38)              # dim grey background
    img[20:35, 5:35] = (230, 30, 20)      # small bright red object in column 0
    m = _raw(ScreenSyncMode(15, {**DEFAULTS, "mode": "region"}, None, grab=lambda: img))
    m.open()
    f = m.next_frame(0)
    m.close()
    px = f[0]
    assert _sat(px) > 0.8 and px[0] > 100, px   # the object's red, not a brown mean
    assert max(f[14]) < 60                       # a grey-only column stays dim grey


@pytest.mark.parametrize("rows", [16, 80])  # ~1.5 % / ~7.5 % of column 0 after subsampling
def test_region_tiny_saturated_speck_does_not_take_over_a_column(rows):
    # review repro: white page with a thin blue line -> segments 0-1 went pure blue at
    # full brightness. A non-largest cluster may only win with a real share of the column.
    img = np.full((1080, 1920, 3), 240, np.uint8)
    img[500:500 + rows, :320] = (0, 120, 215)   # blue line across column 0
    m = _raw(ScreenSyncMode(15, {**DEFAULTS, "mode": "region"}, None, grab=lambda: img))
    m.open()
    f = m.next_frame(0)
    m.close()
    for px in f[:2]:
        assert min(px) > 200 and _sat(px) < 0.15, f[:3]   # stays near-white / neutral


def test_region_needs_no_calibration():
    img = _img(color=RED)
    for calib in (None, [[0.5, 0.9]] * 3):
        m = ScreenSyncMode(4, {**DEFAULTS, "mode": "region", "transition_s": 0.45}, calib, grab=lambda: img)
        m.open()
        f = m.next_frame(0)
        m.close()
        assert len(f) == 4 and all(_close(px, RED) for px in f)


def test_unknown_mode_falls_back_to_dominant():
    assert ScreenSyncMode(4, {"mode": "bogus"}, None).mode == "dominant"
    for mode in ("dominant", "fade", "projection", "region"):
        assert ScreenSyncMode(4, {"mode": mode}, None).mode == mode


def _first(frames, pred, after=0.0):
    return next((t for t, f in frames if t >= after and pred(f)), None)


def test_fade_cycles_two_screen_colours_with_additive_crossfades():
    img = _cols([RED, RED, RED, BLUE, BLUE])   # red 60 %, blue 40 %
    frames = _run_mode("fade", lambda: img, 12.0)
    for t, f in frames:
        assert len(set(f)) == 1                   # whole strip one colour
        px = f[0]
        assert px[1] == 0, (t, px)                # red + blue light: never green / yellow
        if px[0] > 20 and px[2] > 20:             # a crossfade frame = the two lights overlapping
            assert any(_close(px, _overlap(RED, BLUE, w / 100), 2) for w in range(101)), px
    assert _is(frames[0][1][0], RED)              # starts on the biggest colour
    leave = _first(frames, lambda f: f[0][2] > 5)
    blue = _first(frames, lambda f: _is(f[0], BLUE, 3))
    back = _first(frames, lambda f: f[0][0] > 5, after=blue)
    assert leave == pytest.approx(screen.FADE_HOLD, abs=0.15)          # holds ~3 s
    assert blue - leave == pytest.approx(screen.FADE_XFADE, abs=0.25)  # fades ~1.5 s
    assert back - leave == pytest.approx(screen.FADE_HOLD + screen.FADE_XFADE, abs=0.25)


def test_fade_three_colours_and_greys_ignored():
    img = _cols([RED] * 5 + [GREEN] * 3 + [BLUE] * 2)
    frames = _run_mode("fade", lambda: img, 14.0)
    seen = [c for c in (RED, GREEN, BLUE) if any(_is(f[0], c, 3) for _, f in frames)]
    assert seen == [RED, GREEN, BLUE]
    grey = _cols([(128, 128, 128)] * 7 + [RED] * 3)  # 70 % grey UI, 30 % red
    frames = _run_mode("fade", lambda: grey, 10.0)
    assert all(_sat(f[0]) > 0.8 for _, f in frames)  # never fades to the grey


def test_fade_follows_the_screen_live_without_jumps():
    from govee_win11.effects import _LIN
    st = {"img": _cols([RED, RED, RED, BLUE, BLUE])}
    m = ScreenSyncMode(15, {**DEFAULTS, "mode": "fade"}, None, grab=lambda: st["img"])
    m.open()
    out = []
    for k in range(0, 30):
        if k == 10:
            st["img"] = _cols([GREEN, GREEN, GREEN, BLUE, BLUE])  # red turned green
        out.append((k / 10, m.next_frame(k / 10)[0]))
    m.close()
    # share of green light (peak-preserving red+green overlap): rises gradually
    share = [_LIN[px[1]] / (_LIN[px[0]] + _LIN[px[1]]) for _, px in out[10:]]
    assert share[0] < 0.5                                  # not a hard jump
    assert all(b >= a - 1e-9 for a, b in zip(share, share[1:]))
    assert share[8] >= 0.9                                 # in within ~0.8 s (a scene cut)
    assert _is(out[-1][1], GREEN, 20)                      # still in the first colour's hold
    assert all(px[2] < 10 for _, px in out)                # timing unchanged: no blue yet


def test_projection_alternates_groups_and_swaps():
    img = _cols([RED, RED, RED, BLUE, BLUE])
    frames = dict(_run_mode("projection", lambda: img, 9.0))
    f = frames[1.0]
    for i, px in enumerate(f):
        assert _is(px, RED if (i // 2) % 2 == 0 else BLUE), (i, f)   # A A B B A A ...
    g = frames[4.2]
    for i, px in enumerate(g):
        assert _is(px, BLUE if (i // 2) % 2 == 0 else RED), (i, g)   # swapped after ~4 s
    mid = frames[round(effects._PROJ_HOLD + effects._PROJ_XFADE / 2, 1)]
    for px in mid:  # mid-swap: every segment shows both lights overlapping (the bead)
        assert px[0] > 100 and px[2] > 100 and px[1] == 0, mid
    assert all(_is(px, frames[1.0][i]) for i, px in enumerate(frames[8.2]))  # and back


def test_projection_is_the_shared_diy_renderer():
    # screen projection = effects' 投影 with the two screen colours as palette, tempo 50
    # = speed 1, group 2 (Task 16 C: one renderer, not two)
    img = _cols([RED, RED, RED, BLUE, BLUE])
    for t, f in _run_mode("projection", lambda: img, 8.0):
        assert f == effects.render("projection", t, 15, [RED, BLUE]), t
    fast = _run_mode("projection", lambda: img, 4.0, tempo=100)
    for t, f in fast:
        assert f == effects.render("projection", t, 15, [RED, BLUE], speed=2.0), t


def test_projection_single_colour_is_dotted():
    img = _img(color=(0, 200, 120))
    frames = dict(_run_mode("projection", lambda: img, 2.0))
    f = frames[1.0]
    full = [px for i, px in enumerate(f) if (i // 2) % 2 == 0]
    dim = [px for i, px in enumerate(f) if (i // 2) % 2 == 1]
    assert all(_is(px, (0, 200, 120), 3) for px in full)
    for px in dim:
        assert max(px) / 200 == pytest.approx(0.45, abs=0.05), px
        assert abs(px[2] / px[1] - 0.6) < 0.1          # same hue, just dimmer


@pytest.mark.parametrize("tempo,factor", [(0, 2.0), (50, 1.0), (100, 0.5)])
def test_tempo_scales_hold_and_fade(tempo, factor):
    img = _cols([RED, RED, RED, BLUE, BLUE])
    frames = _run_mode("fade", lambda: img, 14.0, tempo=tempo)
    leave = _first(frames, lambda f: f[0][2] > 5)
    blue = _first(frames, lambda f: _is(f[0], BLUE, 3))
    assert leave == pytest.approx(screen.FADE_HOLD * factor, abs=0.15)
    assert blue - leave == pytest.approx(screen.FADE_XFADE * factor, abs=0.3)
    m = ScreenSyncMode(4, {"tempo": tempo}, None)
    assert m.tempo == tempo


@pytest.mark.parametrize("mode", ["dominant", "fade", "projection", "region"])
def test_every_mode_fades_to_dark_on_black_and_capture_errors(mode):
    st = {"img": _cols([RED, BLUE]), "fail": False}

    def grab():
        if st["fail"]:
            raise RuntimeError("capture failed")
        return st["img"]

    m = ScreenSyncMode(15, {**DEFAULTS, "mode": mode}, None, grab=grab)
    m.open()
    lit = m.next_frame(0.0)
    assert max(max(px) for px in lit) > 200
    st["img"] = _img(240, 240)  # DRM video / lock screen captures black
    first = m.next_frame(0.1)
    assert max(max(px) for px in first) > 0     # fades, does not cut to black
    for k in range(2, 40):
        f = m.next_frame(k / 10)
    assert f == [(0, 0, 0)] * 15
    st["img"], st["fail"] = _cols([RED, BLUE]), False
    for k in range(40, 60):
        m.next_frame(k / 10)
    st["fail"] = True
    out = [m.next_frame(k / 10) for k in range(60, 100)]
    assert out[-1] == [(0, 0, 0)] * 15
    m.close()


# ---- Task 17: 混合方式 also drives the screen crossfade -----------------------------------

def _xfade(steps=10):
    s = Smoother(0.5)
    s.step([(255, 0, 0)], 0.1)
    return [s.step([(0, 0, 255)], 0.1)[0] for _ in range(steps)]


def test_smoother_follows_mix_setting():
    from govee_win11 import effects as fx
    lin = _xfade()
    fx.set_mix("perceptual")
    try:
        per = _xfade()
        s = Smoother(0.5)
        assert s.step([(10, 20, 30)], 0.1) == [(10, 20, 30)]       # endpoint exact
        for _ in range(80):
            out = s.step([(200, 100, 50)], 0.1)
        assert out == [(200, 100, 50)]
    finally:
        fx.set_mix("linear")
    assert per != lin
    for px in per:
        assert px[1] == 0 and max(px) >= 250   # still red+blue light, no dim dip


def test_smoother_survives_live_mix_change():
    from govee_win11 import effects as fx
    s = Smoother(0.5)
    s.step([(255, 0, 0)], 0.1)
    a = s.step([(0, 0, 255)], 0.1)[0]
    fx.set_mix("perceptual")
    try:
        b = s.step([(0, 0, 255)], 0.1)[0]
    finally:
        fx.set_mix("linear")
    # continues from where it was (no jump back to red or straight to blue)
    assert b[2] >= a[2] and b[0] <= a[0] and b != (0, 0, 255)
