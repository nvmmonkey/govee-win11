"""What the real strip shows vs what the mode wants (final review critical 1).

Every test drives the real Engine + planner with the measured device profile
(15 segments, 25 packets/s) on *virtual time*, against a fake strip that applies
each segment packet to its own state exactly like the device does. Error = mean over
frames and segments of the max-channel difference between the strip and the target.
"""
import asyncio
import colorsys

import numpy as np
import pytest

from govee_win11.config import _default_diy_presets, _default_palettes
from govee_win11.effects import EffectMode, speed_from_pct
from govee_win11.engine import Engine
from govee_win11.profile import load_profile
from govee_win11.screen import ScreenSyncMode

PROFILE = load_profile()
N = PROFILE.segment_count
PPS = PROFILE.max_packets_per_s


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


PAL = [_rgb(h) for h in _default_palettes()["默认"]]


class VirtualTime:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    async def sleep(self, d):
        self.t += d
        await asyncio.sleep(0)


class FakeStrip:
    """DeviceLink stand-in: decodes segment packets onto a 15-segment state."""

    def __init__(self, vt, n=N, reverse=False):
        self.vt, self.n, self.reverse = vt, n, reverse
        self.state = [None] * n
        self.sends: list[tuple[float, int]] = []  # (virtual time, packet count)

    async def hold(self):
        pass

    async def release(self):
        pass

    async def send(self, packets):
        for p in packets:
            assert p[:4] == bytes([0x33, 0x05, 0x15, 0x01])
            rgb, mask = (p[4], p[5], p[6]), p[12] | (p[13] << 8)
            for bit in range(self.n):
                if mask >> bit & 1:
                    self.state[self.n - 1 - bit if self.reverse else bit] = rgb
        self.sends.append((self.vt.t, len(packets)))
        return True

    def packets_between(self, a, b):
        return sum(c for t, c in self.sends if a <= t < b)


class Recorder:
    """Wraps a mode; after each frame records (t, target, strip state)."""

    def __init__(self, mode, strip):
        self.mode, self.strip = mode, strip
        self.fps = mode.fps
        self.log: list = []
        self._pending = None

    def open(self):
        self.mode.open()

    def close(self):
        self.mode.close()

    def next_frame(self, t):
        # the previous frame's packets have landed by now: score it
        if self._pending is not None:
            self.log.append((*self._pending, list(self.strip.state)))
        frame = self.mode.next_frame(t)
        self._pending = (t, frame)
        return frame


def _err(shown, target):
    return sum(max(abs(a - b) for a, b in zip(s, c)) for s, c in zip(shown, target)) / len(target)


async def _run(mode, seconds, reverse=False):
    vt = VirtualTime()
    strip = FakeStrip(vt, reverse=reverse)
    rec = Recorder(mode, strip)
    pushed = []
    profile = PROFILE if not reverse else type(PROFILE)(**{**PROFILE.__dict__,
                                                            "bit_order": "start_at_end"})
    eng = Engine(strip, profile, on_frame=pushed.append, clock=vt.clock, sleep=vt.sleep)
    await eng.run(rec)
    while vt.t < seconds:
        await asyncio.sleep(0.0005)
    await eng.stop()
    return rec, strip, pushed, vt


def _score(rec, warm=2.0):
    rows = [(t, f, s) for t, f, s in rec.log if t >= warm]
    errs = [_err(s, f) for _, f, s in rows]
    distinct = [len(set(s)) for _, _, s in rows]
    return sum(errs) / len(errs), sum(distinct) / len(distinct)


def _preset(name):
    p = _default_diy_presets()[name]
    return EffectMode(p["effects"], [_rgb(c) for c in p["colors"]],
                      speed_from_pct(p["speed_pct"]), N)


# old planner (dfb76b3) measured with the same harness: aurora 66, pink blue 42,
# pink yellow 45, rainbow1 73, flow1 53, fade2 34 -- always 2 distinct colours shown
CONTINUOUS = {
    #  name: (mode factory, max mean error, min mean distinct colours on the strip)
    "aurora": (lambda: EffectMode("aurora", PAL, speed_from_pct(50), N), 18, 10),
    "pink blue": (lambda: _preset("pink blue"), 17, 6),
    "pink yellow": (lambda: _preset("pink yellow"), 22, 4),
    "rainbow1": (lambda: EffectMode("rainbow1", PAL, speed_from_pct(50), N), 45, 6),
    "flow1": (lambda: EffectMode("flow1", PAL, speed_from_pct(50), N), 33, 6),
    "fade2": (lambda: EffectMode("fade2", PAL, speed_from_pct(50), N), 22, 6),
}


@pytest.mark.parametrize("name", list(CONTINUOUS))
async def test_gradients_reach_the_strip(name):
    make, max_err, min_distinct = CONTINUOUS[name]
    rec, strip, pushed, vt = await _run(make(), 14.0)
    err, distinct = _score(rec)
    assert err <= max_err, f"{name}: mean error {err:.1f}"
    assert distinct >= min_distinct, f"{name}: only {distinct:.1f} colours shown"
    # never above the measured packet rate (one small burst allowance)
    sent = strip.packets_between(2.0, vt.t)
    assert sent <= PPS * (vt.t - 2.0) + 3
    # and the budget is actually used -- unless the strip is already in sync (band
    # effects show only palette colours + edge beads, so a few packets keep it exact)
    assert sent >= 0.9 * PPS * (vt.t - 2.0) or err <= 6


def _stripes(cols, h=60, w=200):
    img = np.zeros((h, w, 3), np.uint8)
    for j, c in enumerate(cols):
        img[:, j * w // len(cols):(j + 1) * w // len(cols)] = c
    return img


FIVE = [(255, 0, 0), (255, 220, 0), (0, 200, 0), (0, 0, 255), (220, 0, 220)]


def _dominant5():
    img = _stripes(FIVE)
    return ScreenSyncMode(N, {"mode": "dominant"}, None, grab=lambda: img)


def _region():
    w = 300
    img = np.zeros((60, w, 3), np.uint8)
    for x in range(w):
        r, g, b = colorsys.hsv_to_rgb(x / w * 0.9, 1, 1)
        img[:, x] = (r * 255, g * 255, b * 255)
    calib = [[(i + 0.5) / N, 0.95] for i in range(N)]
    return ScreenSyncMode(N, {"mode": "region", "smoothing": 0}, calib, grab=lambda: img)


# region: 6 screen columns -> 6 column colours + 3 overlap beads where a segment straddles
# two columns (15 / 6 = 2.5 segments per column); was 11 with the per-segment calibration
@pytest.mark.parametrize("make,colours", [(_dominant5, 5), (_region, 9)])
async def test_static_screen_converges_then_goes_quiet(make, colours):
    rec, strip, pushed, vt = await _run(make(), 3.0)
    target = rec.log[-1][1]
    # converged: the strip shows the screen's colours (within the 6-step threshold)
    assert max(max(abs(a - b) for a, b in zip(s, c)) for s, c in zip(strip.state, target)) <= 6
    assert len(set(strip.state)) >= colours
    # within ~one strip refresh at 25 packets/s
    conv = next(t for t, f, s in rec.log
                if max(max(abs(a - b) for a, b in zip(x, y)) for x, y in zip(s, f)) <= 6)
    assert conv <= N / PPS + 0.2
    # and then nothing is resent
    assert strip.packets_between(1.5, vt.t) == 0


async def test_static_five_colours_are_exact_not_blended():
    rec, strip, pushed, vt = await _run(_dominant5(), 2.0)
    shown = set(strip.state)
    target = set(rec.log[-1][1])
    assert shown == target
    assert (230, 97, 0) not in shown  # the old red+yellow average


async def test_preview_gets_what_the_strip_shows():
    rec, strip, pushed, vt = await _run(EffectMode("aurora", PAL, 1.0, N), 3.0)
    # each push is exactly what the strip showed after that frame's packets
    assert all(p == s for p, (_, _, s) in zip(pushed, rec.log))
    assert pushed[-1] == strip.state
    # and not the intended frame (that one is smoother than 25 packets/s allow)
    assert any(p != f for p, (_, f, _) in zip(pushed[1:], rec.log[1:]))


async def test_reverse_bit_order_strip_matches_shown():
    rec, strip, pushed, vt = await _run(_dominant5(), 2.0, reverse=True)
    assert pushed[-1] == strip.state
    assert set(strip.state) == set(rec.log[-1][1])
