"""Runs one continuous mode at a time and streams its frames to the strip."""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from .frames import MIN_SEGMENT_INTERVAL, Frame, plan_frame
from .link import DeviceLink
from .profile import DeviceProfile

log = logging.getLogger(__name__)


class ContinuousMode(Protocol):
    fps: float

    def open(self) -> None: ...

    def next_frame(self, t: float) -> Frame: ...

    def close(self) -> None: ...


def _noop(frame: Frame) -> None:
    pass


@dataclass(eq=False)
class _Run:
    """One started mode: its private thread, loop task and (single) teardown."""
    mode: ContinuousMode
    executor: ThreadPoolExecutor
    held: bool = False
    task: asyncio.Task | None = None
    teardown: asyncio.Task | None = None


BURST_PACKETS = 3.0  # packet tokens that may pile up while idle (one short burst)


class Engine:
    """``on_frame`` receives what the strip shows after each frame's packets (the
    planner's ``shown``), not the mode's intended frame, so the UI preview is honest.
    ``clock``/``sleep`` are injectable so tests can run the loop on virtual time."""

    def __init__(self, link: DeviceLink, profile: DeviceProfile,
                 on_frame: Callable[[Frame], None] = _noop, *,
                 clock: Callable[[], float] = time.perf_counter,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 min_segment_interval: float = MIN_SEGMENT_INTERVAL):
        self.link = link
        self.min_segment_interval = min_segment_interval
        self.profile = profile
        self.on_frame = on_frame
        self._clock = clock
        self._sleep = sleep
        self.current: ContinuousMode | None = None
        self._run: _Run | None = None
        self._lock = asyncio.Lock()
        self._recolor = False  # set by recolor() (any thread), consumed by the frame loop

    # ---- public ----
    def recolor(self) -> None:
        """The colour calibration changed (protocol.calibrate): re-send every segment of
        the running mode once, through the planner, so its packet budget and the
        per-segment rate cap still hold. Safe from any thread; no-op when idle."""
        self._recolor = True

    async def run(self, mode: ContinuousMode) -> None:
        """Stop the old mode, open ``mode``, hold the link and start streaming.
        Returns once streaming has started (the loop is a background task)."""
        async with self._lock:
            await self._stop_locked()
            loop = asyncio.get_running_loop()
            # one dedicated thread per run: open/next_frame/close share it (mss needs that)
            r = _Run(mode, ThreadPoolExecutor(max_workers=1, thread_name_prefix="engine-mode"))
            try:
                await loop.run_in_executor(r.executor, mode.open)
                r.held = True  # hold() marks the link held before it awaits the connect
                await self.link.hold()
            except BaseException:
                # failed or cancelled: close the mode (queued after a still-running
                # open() on its thread) and release the link if we took it
                await asyncio.shield(self._ensure_teardown(r))
                raise
            r.task = asyncio.create_task(self._stream(r), name="engine-loop")
            r.task.add_done_callback(lambda t: self._loop_done(r, t))
            self._run = r
            self.current = mode

    async def stop(self) -> None:
        """Stop the running mode (``mode.close()``) and release the link."""
        async with self._lock:
            await self._stop_locked()

    async def oneshot(self, packets: list[bytes], key: str | None = None,
                      paced: bool = False) -> bool:
        """Stop any continuous mode, then send a one-off command (``paced``: multi-packet
        upload with gaps, see DeviceLink.send_transient)."""
        async with self._lock:
            await self._stop_locked()
            if paced:
                return await self.link.send_transient(packets, key=key, paced=True)
            return await self.link.send_transient(packets, key=key)

    # ---- internals ----
    async def _stop_locked(self) -> None:
        r, self._run = self._run, None
        if r is None:
            return
        if r.task is not None:
            r.task.cancel()
        # shielded: if our caller is cancelled the cleanup still runs to the end
        await asyncio.shield(self._ensure_teardown(r))

    def _loop_done(self, r: _Run, task: asyncio.Task) -> None:
        # runs even when the task was cancelled before it ever started
        if not task.cancelled() and task.exception() is not None:
            log.error("engine loop crashed; stopping mode", exc_info=task.exception())
        self._ensure_teardown(r)

    def _ensure_teardown(self, r: _Run) -> asyncio.Task:
        """Start r's teardown once; every caller awaits the same task."""
        if r.teardown is None:
            r.teardown = asyncio.ensure_future(self._teardown(r))
        return r.teardown

    async def _teardown(self, r: _Run) -> None:
        if r.task is not None and not r.task.done():
            await asyncio.wait([r.task])  # no link.send after release
        if self.current is r.mode:
            self.current = None
        if r.held:
            try:
                await self.link.release()
            except Exception:
                log.exception("link.release() failed")
        try:
            await asyncio.get_running_loop().run_in_executor(r.executor, r.mode.close)
        except Exception:
            log.exception("mode.close() failed")
        finally:
            r.executor.shutdown(wait=False)

    async def _stream(self, r: _Run) -> None:
        """Frame loop. Packets are paced by a token bucket filled at
        ``max_packets_per_s`` (so e.g. 10 fps gets 2.5 packets per frame on average,
        not int(25/10) = 2), holding at most ``max(BURST_PACKETS, one frame's share)``.
        Each segment is re-commanded at most once per ``min_segment_interval`` on
        average (the firmware fades every command; see frames.MIN_SEGMENT_INTERVAL),
        with half a frame period of slack for timer jitter, counted from when the
        packets go out (read after ``next_frame``, whose duration varies)."""
        mode, executor = r.mode, r.executor
        loop = asyncio.get_running_loop()
        reverse = self.profile.bit_order == "start_at_end"
        pps = float(self.profile.max_packets_per_s)
        prev: Frame | None = None
        sent_at: list[float] = []  # per segment: clock time of its last command
        stale: set[int] = set()    # segments to re-send after a calibration change
        self._recolor = False
        start = self._clock()
        deadline = start
        tokens: float | None = None
        last = start
        while True:
            fps = float(mode.fps)
            period = 1.0 / fps
            cap = max(BURST_PACKETS, pps / fps)
            now = self._clock()
            if tokens is None:
                tokens = min(cap, max(1.0, pps / fps))
            else:
                tokens = min(cap, tokens + (now - last) * pps)
            last = now
            t = now - start
            try:
                frame = await loop.run_in_executor(executor, mode.next_frame, t)
            except Exception:
                log.exception("next_frame failed; skipping frame")
                frame = None
            if frame is not None:
                budget = int(tokens + 1e-6)  # float drift: 2.9999 is 3 tokens
                if len(sent_at) != len(frame):
                    sent_at = [float("-inf")] * len(frame)
                if self._recolor:
                    self._recolor = False
                    stale = set(range(len(frame)))
                if budget >= 1:
                    packets, shown = plan_frame(frame, prev, budget, reverse=reverse,
                                                now=self._clock(), sent_at=sent_at,
                                                min_interval=self.min_segment_interval,
                                                slack=0.5 * period, stale=stale)
                else:
                    packets, shown = [], (prev if prev is not None else frame)
                try:
                    self.on_frame(shown)
                except Exception:
                    log.exception("on_frame callback failed")
                if packets:
                    tokens -= len(packets)
                    try:
                        ok = await self.link.send(packets)
                    except Exception:
                        log.exception("link.send failed")
                        ok = False
                    # on failure forget what the strip shows (partial write, phone took
                    # over...): once the link is back the whole strip is covered again
                    prev = shown if ok else None
            deadline += period
            now = self._clock()
            if now > deadline + period:
                deadline = now  # fell behind (slow grab / link): don't burst to catch up
            await self._sleep(max(0.0, deadline - now))
