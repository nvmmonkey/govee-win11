import asyncio
import logging
import threading
import time

import pytest

from govee_win11.engine import Engine
from govee_win11.profile import DeviceProfile
from govee_win11.protocol import segment_packet

N = 14
FULL = (1 << N) - 1


class FakeLink:
    """Mirrors the real DeviceLink: hold() sets held before awaiting connect."""

    def __init__(self, send_results=None, hold_delay=0.0, transient_delay=0.0):
        self.calls: list = []
        self.sent: list[list[bytes]] = []
        self.send_results = list(send_results or [])
        self.transient: list = []
        self.held = False
        self.hold_delay = hold_delay
        self.transient_delay = transient_delay

    async def hold(self):
        self.calls.append("hold")
        self.held = True
        if self.hold_delay:
            await asyncio.sleep(self.hold_delay)

    async def release(self):
        self.calls.append("release")
        self.held = False

    async def send(self, packets):
        self.calls.append("send")
        self.sent.append(list(packets))
        return self.send_results.pop(0) if self.send_results else True

    async def send_transient(self, packets, key=None):
        self.calls.append("transient")
        if self.transient_delay:
            await asyncio.sleep(self.transient_delay)
        self.transient.append((list(packets), key))
        return True


class FakeMode:
    """Frame k: segment 0 changes every frame, the rest stay one colour."""

    def __init__(self, fps=200.0, fail_on=(), open_s=0.0, close_s=0.0, log=None):
        self.fps = fps
        self.fail_on = set(fail_on)
        self.open_s = open_s
        self.close_s = close_s
        self.log = log if log is not None else []
        self.calls = 0
        self.ts: list[float] = []
        self.opened = 0
        self.closed = 0
        self.threads: set = set()

    def open(self):
        self.threads.add(threading.get_ident())
        self.log.append(("open", id(self)))
        time.sleep(self.open_s)
        self.opened += 1

    def next_frame(self, t):
        self.threads.add(threading.get_ident())
        self.calls += 1
        self.ts.append(t)
        if self.calls in self.fail_on:
            raise RuntimeError("boom")
        k = self.calls
        return [((k * 40) % 256, 0, 0)] + [(0, 0, 200)] * (N - 1)

    def close(self):
        self.threads.add(threading.get_ident())
        time.sleep(self.close_s)
        self.closed += 1
        self.log.append(("closed", id(self)))


def profile(pps=2000):
    return DeviceProfile(segment_count=N, max_packets_per_s=pps)


async def wait_for(cond, timeout=1.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.002)


def masks(packets):
    return [p[12] | (p[13] << 8) for p in packets]


async def test_run_holds_and_streams():
    link, mode, frames = FakeLink(), FakeMode(), []
    eng = Engine(link, profile(), on_frame=frames.append)
    await eng.run(mode)
    assert eng.current is mode
    assert mode.opened == 1
    assert link.calls[0] == "hold"
    await wait_for(lambda: len(link.sent) >= 3)
    # first frame is complete, later frames only carry segment 0
    assert sum(masks(link.sent[0])) == FULL
    assert masks(link.sent[1]) == [1]
    assert len(frames) >= 3 and len(frames[0]) == N
    assert mode.ts[0] < 0.05 and mode.ts == sorted(mode.ts)
    await eng.stop()
    assert mode.closed == 1
    assert link.calls[-1] == "release"
    assert eng.current is None


async def test_mode_runs_on_single_thread():
    link, mode = FakeLink(), FakeMode()
    eng = Engine(link, profile())
    await eng.run(mode)
    await wait_for(lambda: mode.calls >= 3)
    await eng.stop()
    assert len(mode.threads) == 1
    assert threading.get_ident() not in mode.threads


async def test_switching_modes_closes_previous():
    link, m1, m2 = FakeLink(), FakeMode(), FakeMode()
    eng = Engine(link, profile())
    await eng.run(m1)
    await wait_for(lambda: m1.calls >= 1)
    await eng.run(m2)
    assert m1.closed == 1
    assert m2.opened == 1 and m2.closed == 0
    assert eng.current is m2
    calls_after_switch = m1.calls
    await wait_for(lambda: m2.calls >= 2)
    assert m1.calls == calls_after_switch
    await eng.stop()
    assert m1.closed == 1 and m2.closed == 1


async def test_oneshot_stops_continuous_mode():
    link, mode = FakeLink(), FakeMode()
    eng = Engine(link, profile())
    await eng.run(mode)
    await wait_for(lambda: mode.calls >= 1)
    pkt = segment_packet((1, 2, 3), FULL)
    assert await eng.oneshot([pkt], key="color") is True
    assert mode.closed == 1
    assert eng.current is None
    assert link.transient == [([pkt], "color")]
    assert link.calls.index("release") < link.calls.index("transient")


async def test_engine_survives_send_failure():
    link, mode = FakeLink(send_results=[False, False, True]), FakeMode()
    eng = Engine(link, profile())
    await eng.run(mode)
    await wait_for(lambda: len(link.sent) >= 4)
    await eng.stop()
    for i in range(3):
        assert sum(masks(link.sent[i])) == FULL, f"send {i} should be a full frame"
    assert masks(link.sent[3]) == [1]


async def test_send_failure_mid_stream_resends_full_frame():
    link, mode = FakeLink(send_results=[True, True, False, True]), FakeMode()
    eng = Engine(link, profile())
    await eng.run(mode)
    await wait_for(lambda: len(link.sent) >= 5)
    await eng.stop()
    assert masks(link.sent[1]) == [1]
    assert masks(link.sent[2]) == [1]          # this one fails
    assert sum(masks(link.sent[3])) == FULL    # so the next send is complete
    assert masks(link.sent[4]) == [1]


async def test_mode_exception_skips_frame(caplog):
    link, mode, frames = FakeLink(), FakeMode(fail_on={2}), []
    eng = Engine(link, profile(), on_frame=frames.append)
    with caplog.at_level(logging.ERROR):
        await eng.run(mode)
        # segment 0 changes every frame: its 2nd command waits for the 0.2 s rate cap
        await wait_for(lambda: mode.calls >= 4 and len(link.sent) >= 2)
        await eng.stop()
    assert len(frames) == mode.calls - 1
    assert len(link.sent) >= 2
    assert "boom" in caplog.text


async def test_on_frame_exception_is_ignored():
    def bad(frame):
        raise ValueError("ui gone")

    link, mode = FakeLink(), FakeMode()
    eng = Engine(link, profile(), on_frame=bad)
    await eng.run(mode)
    await wait_for(lambda: len(link.sent) >= 2)
    await eng.stop()


async def test_packet_budget_follows_fps():
    # 20 packets/s at 10 fps -> 2 packets per frame
    link = FakeLink()

    class Rainbow(FakeMode):
        def next_frame(self, t):
            super().next_frame(t)
            return [((i * 18) % 256, (i * 70) % 256, 255 - i * 18) for i in range(N)]

    mode = Rainbow(fps=10)
    eng = Engine(link, profile(pps=20))
    await eng.run(mode)
    await wait_for(lambda: len(link.sent) >= 1)
    await eng.stop()
    assert len(link.sent[0]) == 2
    assert sum(masks(link.sent[0])) == FULL


async def test_frames_paced_by_fps():
    link, mode = FakeLink(), FakeMode(fps=50)
    eng = Engine(link, profile())
    await eng.run(mode)
    await asyncio.sleep(0.2)
    await eng.stop()
    # ~10 frames in 0.2 s; generous bounds for a loaded CI box
    assert 5 <= mode.calls <= 13
    gaps = [b - a for a, b in zip(mode.ts, mode.ts[1:])]
    assert all(g > 0.01 for g in gaps)


async def test_crashed_loop_closes_mode_once(caplog):
    class Crashy(FakeMode):
        @property
        def fps(self):
            if self.calls >= 2:
                raise RuntimeError("fps exploded")
            return 200.0

        @fps.setter
        def fps(self, v):
            pass

    link, mode = FakeLink(), Crashy()
    eng = Engine(link, profile())
    with caplog.at_level(logging.ERROR):
        await eng.run(mode)
        await wait_for(lambda: mode.closed == 1)
    assert "fps exploded" in caplog.text
    assert "release" in link.calls
    assert eng.current is None
    await eng.stop()
    assert mode.closed == 1


async def test_stop_without_mode_is_noop():
    link = FakeLink()
    eng = Engine(link, profile())
    await eng.stop()
    assert link.calls == []


async def test_effect_mode_runs_through_engine():
    from govee_win11.effects import EffectMode

    link, frames = FakeLink(), []
    eng = Engine(link, DeviceProfile(segment_count=15, max_packets_per_s=20),
                 on_frame=frames.append)
    await eng.run(EffectMode("rainbow1", [], 1.0, 15))
    await wait_for(lambda: len(link.sent) >= 1)
    await eng.stop()
    assert len(frames[0]) == 15
    assert len(link.sent[0]) == 1  # 20 pps / 12 fps -> 1 packet per frame


async def cancel_soon(coro, after=0.05):
    task = asyncio.create_task(coro)
    await asyncio.sleep(after)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_cancelled_during_hold_releases_link():
    link, mode = FakeLink(hold_delay=1.0), FakeMode()
    eng = Engine(link, profile())
    await cancel_soon(eng.run(mode))
    await wait_for(lambda: mode.closed == 1)
    assert link.held is False
    assert mode.closed == 1
    assert eng.current is None
    await eng.stop()
    assert mode.closed == 1


async def test_run_cancelled_during_open_still_closes_mode():
    link, mode = FakeLink(), FakeMode(open_s=0.15)
    eng = Engine(link, profile())
    await cancel_soon(eng.run(mode))
    await wait_for(lambda: mode.closed == 1)
    assert mode.opened == 1  # close ran after open finished, on the same thread
    assert len(mode.threads) == 1
    assert "hold" not in link.calls
    assert link.held is False
    assert eng.current is None


async def test_stop_cancelled_during_cleanup_still_releases():
    link, mode = FakeLink(), FakeMode(close_s=0.15)
    eng = Engine(link, profile())
    await eng.run(mode)
    await wait_for(lambda: mode.calls >= 1)
    await cancel_soon(eng.stop())
    await wait_for(lambda: mode.closed == 1)
    await wait_for(lambda: not link.held)
    assert eng.current is None
    await eng.stop()
    assert mode.closed == 1


async def test_oneshot_cancelled_during_cleanup_still_releases():
    link, mode = FakeLink(), FakeMode(close_s=0.15)
    eng = Engine(link, profile())
    await eng.run(mode)
    await wait_for(lambda: mode.calls >= 1)
    await cancel_soon(eng.oneshot([segment_packet((1, 1, 1), FULL)]))
    await wait_for(lambda: mode.closed == 1)
    await wait_for(lambda: not link.held)


async def test_stop_during_crash_cleanup_releases_and_waits_for_close():
    class Crashy(FakeMode):
        @property
        def fps(self):
            if self.calls >= 1:
                raise RuntimeError("fps exploded")
            return 200.0

        @fps.setter
        def fps(self, v):
            pass

    link, mode = FakeLink(), Crashy(close_s=0.15)
    eng = Engine(link, profile())
    await eng.run(mode)
    await wait_for(lambda: mode.calls >= 1)
    await asyncio.sleep(0.03)  # crash cleanup is now inside the slow close()
    await eng.stop()
    assert mode.closed == 1  # stop() waited for the in-flight cleanup
    assert link.held is False


async def test_switch_closes_old_mode_before_opening_new():
    order: list = []
    link = FakeLink()
    m1, m2 = FakeMode(close_s=0.05, log=order), FakeMode(log=order)
    eng = Engine(link, profile())
    await eng.run(m1)
    await wait_for(lambda: m1.calls >= 1)
    await eng.run(m2)
    assert order.index(("closed", id(m1))) < order.index(("open", id(m2)))
    assert link.held is True
    await eng.stop()
    assert link.held is False


async def test_oneshot_holds_lock_until_sent():
    link, mode = FakeLink(transient_delay=0.1), FakeMode()
    eng = Engine(link, profile())
    one = asyncio.create_task(eng.oneshot([segment_packet((1, 1, 1), FULL)]))
    await asyncio.sleep(0.02)
    run = asyncio.create_task(eng.run(mode))
    await asyncio.sleep(0.03)
    assert mode.opened == 0  # run() waits for the one-shot to finish
    assert await one is True
    await run
    assert mode.opened == 1
    assert link.calls.index("transient") < link.calls.index("hold")
    await eng.stop()


class _VirtualTime:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    async def sleep(self, d):
        self.t += d
        await asyncio.sleep(0)


@pytest.mark.parametrize("fps", [10.0, 12.0, 20.0])
async def test_token_bucket_uses_the_full_packet_rate(fps):
    # old budget int(25 / fps): 2/frame at 10-12 fps (20-24 pps), 1/frame at 20 fps (20 pps)
    vt = _VirtualTime()

    class Busy(FakeMode):
        def next_frame(self, t):
            super().next_frame(t)
            k = self.calls
            return [((i * 18 + k * 50) % 256, (i * 70 + k * 30) % 256, (k * 90) % 256)
                    for i in range(N)]

    link = FakeLink()
    sent_at = []
    orig = link.send

    async def send(packets):
        sent_at.append((vt.t, len(packets)))
        return await orig(packets)

    link.send = send
    # the bucket alone: with the per-segment rate cap (tested separately below) a frame
    # can come where every changed segment is held back, so a few tokens overflow
    eng = Engine(link, profile(pps=25), clock=vt.clock, sleep=vt.sleep, min_segment_interval=0.0)
    await eng.run(Busy(fps=fps))
    while vt.t < 10.0:
        await asyncio.sleep(0.0005)
    await eng.stop()
    total = sum(c for t, c in sent_at if t < 10.0)
    assert 25 * 10 - 3 <= total <= 25 * 10 + 3
    assert max(c for _, c in sent_at) <= max(3, int(25 / fps) + 1)
    # never more than 25 packets in any 1 s window (+ the 3-packet burst allowance)
    for t0, _ in sent_at:
        assert sum(c for t, c in sent_at if t0 <= t < t0 + 1.0) <= 25 + 3


async def test_on_frame_gets_the_planned_shown_frame():
    # 2 packets/frame for a 14-colour frame: the push must be the merged frame that the
    # strip really shows, not the 14-colour intended one
    link, pushed = FakeLink(), []

    class Rainbow(FakeMode):
        def next_frame(self, t):
            super().next_frame(t)
            return [((i * 18) % 256, (i * 70) % 256, 255 - i * 18) for i in range(N)]

    eng = Engine(link, profile(pps=20), on_frame=pushed.append)
    mode = Rainbow(fps=10)
    await eng.run(mode)
    await wait_for(lambda: len(pushed) >= 1)
    await eng.stop()
    assert len(set(pushed[0])) == 2  # first frame: whole strip in 2 merged blocks


async def test_oneshot_paced_flag_reaches_link_only_when_set():
    calls = []

    class L:
        status = "connected"

        async def send_transient(self, packets, key=None, **kw):
            calls.append(kw)
            return True

    eng = Engine(L(), profile())
    await eng.oneshot([b"a"], "k")
    await eng.oneshot([b"a", b"b"], "k", paced=True)
    assert calls == [{}, {"paced": True}]


# ---- per-segment rate cap: the strip fades each command itself (measured: 10/s stalls) ----

def _segment_command_times(sent_at_packets, n=N):
    """[(time, packets)] -> per segment, the times it received a colour command."""
    per = [[] for _ in range(n)]
    for t, packets in sent_at_packets:
        for m in masks(packets):
            for i in range(n):
                if m >> i & 1:
                    per[i].append(t)
    return per


async def _run_virtual(mode, seconds, pps=25):
    vt = _VirtualTime()
    link = FakeLink()
    log = []
    orig = link.send

    async def send(packets):
        log.append((vt.t, list(packets)))
        return await orig(packets)

    link.send = send
    eng = Engine(link, profile(pps=pps), clock=vt.clock, sleep=vt.sleep)
    await eng.run(mode)
    while vt.t < seconds:
        await asyncio.sleep(0.0005)
    await eng.stop()
    return log, link


async def test_whole_strip_flipping_at_20fps_is_capped_to_5_per_segment():
    class Flip(FakeMode):
        def next_frame(self, t):
            super().next_frame(t)
            if t >= 3.0:  # then settles on its final colour
                return [(0, 255, 0)] * N
            return [(255, 0, 0) if self.calls % 2 else (0, 0, 255)] * N

    log, _ = await _run_virtual(Flip(fps=20), 4.0)
    per = _segment_command_times(log)
    for times in per:
        gaps = [b - a for a, b in zip(times, times[1:])]
        # half a frame period of slack absorbs timer jitter (see frames.plan_frame)
        assert min(gaps) >= 0.2 - 0.5 / 20 - 1e-6, min(gaps)
        for t0 in times:
            assert sum(1 for t in times if t0 <= t < t0 + 1.0 - 1e-6) <= 5
    # nothing lost: the strip ends on the last frame's colour
    last = {}
    for t, packets in log:
        for p in packets:
            m = p[12] | (p[13] << 8)
            for i in range(N):
                if m >> i & 1:
                    last[i] = (p[4], p[5], p[6])
    assert all(last[i] == (0, 255, 0) for i in range(N))


async def test_slow_changes_are_not_delayed_by_the_cap():
    class Slow(FakeMode):
        def next_frame(self, t):
            super().next_frame(t)
            k = int(t / 0.5)
            return [((k * 70) % 256, 100, 200 - (k * 30) % 200)] * N

    mode = Slow(fps=10)
    log, _ = await _run_virtual(mode, 3.0)
    # every change (every 0.5 s) goes out in the frame it first appears in
    change_times = sorted({round(t, 6) for t in mode.ts if t > 0 and int(t / 0.5) != int((t - 0.1 + 1e-9) / 0.5)})
    sent_times = [round(t, 6) for t, _ in log]
    for ct in change_times:
        assert any(abs(st - ct) < 1e-6 for st in sent_times), (ct, sent_times)



class _JitterTime(_VirtualTime):
    """Virtual clock whose sleeps overshoot by 0-8 ms, like the Windows timer."""

    def __init__(self, seed=0):
        super().__init__()
        import random
        self._rng = random.Random(seed)

    async def sleep(self, d):
        self.t += d + self._rng.uniform(0.0, 0.008)
        await asyncio.sleep(0)


@pytest.mark.parametrize("fps", [10.0, 12.0])
async def test_rate_cap_reaches_5_per_s_at_10_and_12_fps_with_jitter(fps):
    class Flip(FakeMode):
        def next_frame(self, t):
            super().next_frame(t)
            k = self.calls  # a new colour every frame
            return [((k * 40) % 256, 0, 255 - (k * 40) % 256)] * N

    vt = _JitterTime()
    link, log = FakeLink(), []
    orig = link.send

    async def send(packets):
        log.append((vt.t, list(packets)))
        return await orig(packets)

    link.send = send
    eng = Engine(link, profile(pps=25), clock=vt.clock, sleep=vt.sleep)
    await eng.run(Flip(fps=fps))
    while vt.t < 10.0:
        await asyncio.sleep(0.0005)
    await eng.stop()
    for times in _segment_command_times(log):
        times = [t for t in times if t < 10.0]
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert min(gaps) >= 0.2 - 0.5 / fps - 1e-6
        assert len(times) <= 10 * 5 + 1                  # never above 5/s on average
        assert len(times) >= 10 * 4.6                    # and no longer stuck at ~4/s


async def test_send_time_stamps_the_segment_not_the_frame_start():
    # next_frame time varies (slow screen grab): the cap must count from the moment the
    # packets go out, or a slow frame followed by a fast one re-commands too soon
    vt = _VirtualTime()

    class Slow(FakeMode):
        def next_frame(self, t):
            super().next_frame(t)
            k = self.calls
            vt.t += 0.12 if k % 4 == 1 else 0.0       # every 4th grab takes 0.12 s
            return [((k * 40) % 256, 0, 255 - (k * 40) % 256)] * N  # new colour each frame

    link, log = FakeLink(), []
    orig = link.send

    async def send(packets):
        log.append((vt.t, list(packets)))
        return await orig(packets)

    link.send = send
    eng = Engine(link, profile(pps=25), clock=vt.clock, sleep=vt.sleep)
    await eng.run(Slow(fps=20))
    while vt.t < 5.0:
        await asyncio.sleep(0.0005)
    await eng.stop()
    for times in _segment_command_times(log):
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert min(gaps) >= 0.2 - 0.5 / 20 - 1e-6, min(gaps)


# ---- Task 17: 每段每秒刷新次数 is adjustable, live --------------------------------------

async def test_segment_rate_follows_live_interval_change():
    class Flip(FakeMode):
        def next_frame(self, t):
            super().next_frame(t)
            # a new colour every frame (3-cycle: never back on the shown one at 10/s)
            return [[(255, 0, 0), (0, 255, 0), (0, 0, 255)][self.calls % 3]] * N

    vt = _VirtualTime()
    link = FakeLink()
    log = []
    orig = link.send

    async def send(packets):
        log.append((vt.t, list(packets)))
        return await orig(packets)

    link.send = send
    eng = Engine(link, profile(pps=25), clock=vt.clock, sleep=vt.sleep)
    assert eng.min_segment_interval == pytest.approx(0.2)   # default 5/s
    await eng.run(Flip(fps=20))
    while vt.t < 2.0:
        await asyncio.sleep(0.0005)
    eng.min_segment_interval = 1 / 10                     # user picks 10/s while running
    while vt.t < 4.0:
        await asyncio.sleep(0.0005)
    await eng.stop()
    per = _segment_command_times(log)
    for times in per:
        before = sum(1 for t in times if 1.0 <= t < 2.0)
        after = sum(1 for t in times if 3.0 <= t < 4.0)
        assert before <= 5
        assert 8 <= after <= 10


# ---- Task 18: 颜色校准 changes re-send the running mode through the planner ----------

async def test_recolor_resends_static_frame_once_with_new_calibration():
    from govee_win11 import protocol

    class Static(FakeMode):
        def next_frame(self, t):
            super().next_frame(t)
            return [(255, 255, 255)] * N

    vt = _VirtualTime()
    link = FakeLink()
    log = []
    orig = link.send

    async def send(packets):
        log.append((vt.t, list(packets)))
        return await orig(packets)

    link.send = send
    eng = Engine(link, profile(pps=25), clock=vt.clock, sleep=vt.sleep)
    eng.recolor()                                     # idle: harmless
    await eng.run(Static(fps=20))
    while vt.t < 1.0:
        await asyncio.sleep(0.0005)
    assert len(log) == 1                              # static content: sent once
    protocol.set_color_cal(100, 80, 60)
    eng.recolor()
    while vt.t < 2.0:
        await asyncio.sleep(0.0005)
    eng.recolor()                                     # a second change shortly after...
    eng.recolor()
    while vt.t < 2.1:
        await asyncio.sleep(0.0005)
    await eng.stop()
    expected = segment_packet((255, 255, 255), FULL)  # built at the current gains
    protocol.set_color_cal(100, 100, 100)
    assert len(log) == 3
    assert log[1][1] == [expected] and log[2][1] == [expected]
    assert log[1][1][0] != segment_packet((255, 255, 255), FULL)
    for times in _segment_command_times(log):         # ...still obeys the rate cap
        assert all(b - a >= 0.2 - 0.5 / 20 - 1e-6 for a, b in zip(times, times[1:]))
