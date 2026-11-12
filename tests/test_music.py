import asyncio

import numpy as np

from govee_win11 import music
from govee_win11.engine import Engine
from govee_win11.music import Analyzer, MusicMode
from govee_win11.profile import DeviceProfile

RATE = 44100
BLOCK = 1024


def blocks(signal):
    for i in range(0, len(signal) - BLOCK + 1, BLOCK):
        yield signal[i:i + BLOCK].astype(np.float32)


def test_silence_no_beats():
    a = Analyzer(RATE)
    vols, beats = [], 0
    for b in blocks(np.zeros(RATE * 3)):
        v, beat = a.feed(b)
        vols.append(v)
        beats += beat
    assert max(vols) < 0.05
    assert beats == 0


def test_bass_pulses_detected():
    t = np.arange(RATE * 4) / RATE
    sig = np.sin(2 * np.pi * 60 * t) * 0.8
    sig[(t % 0.5) >= 0.1] = 0.0
    a = Analyzer(RATE)
    beats = sum(a.feed(b)[1] for b in blocks(sig))
    assert 7 <= beats <= 9


def test_nan_block_ignored():
    a = Analyzer(RATE)
    t = np.arange(BLOCK) / RATE
    good = (np.sin(2 * np.pi * 60 * t) * 0.5).astype(np.float32)
    a.feed(good)
    bad = good.copy()
    bad[5] = np.nan
    assert a.feed(bad)[1] is False
    assert a.feed(np.array([], dtype=np.float32))[1] is False
    v, _ = a.feed(good)
    assert np.isfinite(v) and 0.0 <= v <= 1.0


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_capture_error_is_idle():
    clock = Clock()
    calls = []

    def capture():
        calls.append(clock.t)
        raise OSError("device gone")

    m = MusicMode(4, [(255, 0, 0)], 1.0, capture=capture, clock=clock)
    m.open()
    f = m.next_frame(0.0)
    # idle: the palette colour, dimmed (not off, not bright)
    assert len(f) == 4 and all(0 < px[0] <= music.IDLE_LEVEL * 255 + 1 for px in f)
    n = len(calls)
    clock.t = 1.0
    m.next_frame(1.0)
    assert len(calls) == n  # too soon to retry
    clock.t = 2.1
    m.next_frame(2.1)
    assert len(calls) == n + 1
    m.close()


def test_beat_advances_palette():
    t = np.arange(BLOCK) / RATE
    loud = (np.sin(2 * np.pi * 60 * t) * 0.8).astype(np.float32)
    quiet = np.zeros(BLOCK, dtype=np.float32)
    seq = iter([quiet] * 5 + [loud])
    pal = [(255, 0, 0), (0, 255, 0)]
    m = MusicMode(3, pal, 1.0, capture=lambda: next(seq, None), clock=Clock())
    m.open()
    for i in range(5):
        m.next_frame(i * 0.05)
    f = m.next_frame(0.25)
    assert f[1] == (0, 255, 0)  # centre segment: advanced to second color, full brightness
    f = m.next_frame(0.25 + music.RIPPLE_SPAN)
    assert f == [(0, 255, 0)] * 3  # the ends follow within the ripple


def test_empty_palette_white():
    m = MusicMode(2, [], 1.0, capture=lambda: None, clock=Clock())
    f = m.next_frame(0.0)
    assert f[0][0] == f[0][1] == f[0][2]


# ---- music through the strip's rate cap (Task 16 B2) ---------------------------

PAL = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]


def kick_track(seconds, peak=0.5, bpm=120, rate=RATE):
    """Kick drum: 60 Hz sine bursts with a fast decay, one per beat."""
    t = np.arange(int(seconds * rate)) / rate
    phase = t % (60.0 / bpm)
    env = np.exp(-phase / 0.04) * (phase < 0.15)
    return (np.sin(2 * np.pi * 60 * t) * env * peak).astype(np.float32)


class TimedCapture:
    """Loopback stand-in: hands out the samples that 'arrived' up to clock()."""

    def __init__(self, signal, clock, rate=RATE):
        self.signal, self.clock, self.rate, self.pos = signal, clock, rate, 0

    def __call__(self):
        end = min(len(self.signal), int(self.clock() * self.rate))
        if end <= self.pos:
            return None
        out, self.pos = self.signal[self.pos:end], end
        return out


def run_offline(signal, seconds, fps=20.0, pal=PAL, n=15):
    clock = Clock()
    m = MusicMode(n, pal, 1.0, capture=TimedCapture(signal, clock), clock=clock)
    m.open()
    frames = []
    for k in range(int(seconds * fps)):
        clock.t = k / fps
        frames.append((clock.t, m.next_frame(clock.t)))
    m.close()
    return frames


def hue_index(px, pal=PAL):
    """Which palette colour a (dimmed) pixel shows."""
    def d(c):
        m = max(px) or 1
        return sum((a / m - b / 255) ** 2 for a, b in zip(px, c))
    return min(range(len(pal)), key=lambda j: d(pal[j]))


def colour_changes(frames, seg=7, start=0.5):
    idx = [(t, hue_index(f[seg])) for t, f in frames if t >= start]
    return sum(1 for (_, a), (_, b) in zip(idx, idx[1:]) if a != b)


def test_kick_drum_changes_colour_twice_a_second():
    frames = run_offline(kick_track(6.5), 6.5)
    ch = colour_changes(frames)
    assert 10 <= ch <= 13, ch  # 120 BPM over 6 s -> ~12


def test_quiet_rhythmic_signal_still_drives_beats():
    # real loopback peaks measured today were only ~0.02 full scale
    frames = run_offline(kick_track(6.5, peak=0.02), 6.5)
    assert colour_changes(frames) >= 9


def test_strip_never_near_black_while_music_plays():
    frames = run_offline(kick_track(6.5), 6.5)
    assert all(max(px) >= music.MIN_LEVEL * 255 - 1 for t, f in frames if t >= 0.5 for px in f)


def test_brightness_updates_at_most_4_per_second_and_only_on_big_moves():
    # loud mid-range tone (no bass -> no beats) whose loudness wobbles fast
    t = np.arange(int(6 * RATE)) / RATE
    amp = 0.05 + 0.15 * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))
    sig = (np.sin(2 * np.pi * 1000 * t) * amp).astype(np.float32)
    frames = run_offline(sig, 6.0, pal=[(255, 255, 255)])
    levels = [f[0][0] for t_, f in frames if t_ >= 0.5]
    changes = [i for i in range(1, len(levels)) if levels[i] != levels[i - 1]]
    assert len(changes) <= 4 * 5.5 + 1
    assert all(abs(levels[i] - levels[i - 1]) >= 0.15 * 255 - 2 for i in changes)
    assert min(levels) >= music.MIN_LEVEL * 255 - 1


def test_silence_goes_idle_dim():
    sig = np.concatenate([kick_track(2.0), np.zeros(int(4 * RATE), np.float32)])
    frames = run_offline(sig, 6.0)
    late = [f for t, f in frames if t >= 2.0 + music.IDLE_AFTER + 0.1]
    assert late and all(0 < max(px) <= music.IDLE_LEVEL * 255 + 1 for f in late for px in f)
    # and before the 2 s are up it still shows the music floor (last kick ends ~1.65 s)
    mid = [f for t, f in frames if 1.8 <= t < 1.65 + music.IDLE_AFTER - 0.1]
    assert mid
    assert all(max(px) >= music.MIN_LEVEL * 255 - 1 for f in mid for px in f)


class _VT:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    async def sleep(self, d):
        self.t += d
        await asyncio.sleep(0)


class _Link:
    def __init__(self, vt):
        self.vt, self.log = vt, []

    async def hold(self):
        pass

    async def release(self):
        pass

    async def send(self, packets):
        self.log.append((self.vt.t, list(packets)))
        return True


async def test_music_through_engine_respects_the_segment_rate_cap():
    vt = _VT()
    link = _Link(vt)
    mode = MusicMode(15, PAL, 1.0, capture=TimedCapture(kick_track(6.5), vt.clock), clock=vt.clock)
    eng = Engine(link, DeviceProfile(segment_count=15, max_packets_per_s=25),
                 clock=vt.clock, sleep=vt.sleep)
    await eng.run(mode)
    while vt.t < 6.0:
        await asyncio.sleep(0.0005)
    await eng.stop()
    per = [[] for _ in range(15)]
    for t, packets in link.log:
        for p in packets:
            mask = p[12] | (p[13] << 8)
            for i in range(15):
                if mask >> i & 1:
                    per[i].append(t)
    for times in per:
        assert all(b - a >= 0.2 - 1e-6 for a, b in zip(times, times[1:]))
    # and the strip visibly follows: the centre segment gets ~2 colour commands/s or more
    assert len(per[7]) >= 2 * 5
