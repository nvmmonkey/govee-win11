"""Music-reactive mode: analyse what the PC is playing (WASAPI loopback).

Capture interface (injectable for tests): a zero-arg callable
``capture() -> np.ndarray | None`` returning the mono float32 samples that
arrived since the previous call (None if nothing new) and raising ``OSError``
when the device fails (e.g. headphones plugged/unplugged).  Optionally it has
``open()`` / ``close()`` methods and a ``rate`` attribute, used by MusicMode.
``LoopbackCapture`` is the real implementation (PyAudioWPatch, loopback of the
default output device).

MusicMode satisfies the engine's ContinuousMode protocol structurally.

Designed for the strip's own fade: every segment command makes the H6125 firmware fade
to the new colour and a segment re-commanded faster than ~5/s stalls mid-fade (the
engine caps each segment at 5/s). So instead of re-colouring the whole strip at 20 fps,
the strip changes only on events, at most 4 per second in total:
- beat -> next palette colour at full brightness (beats >= BEAT_GAP apart); strong
  beats light it from the centre outward over 3 frames (ends RIPPLE_SPAN later);
- between beats the brightness follows the loudness, but only every LEVEL_INTERVAL
  and only when it moved by LEVEL_STEP, never below MIN_LEVEL while music plays;
- silence (no sound for IDLE_AFTER) -> the palette colour, dimmed to IDLE_LEVEL.
The firmware fade then makes each change smooth.
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Callable

import numpy as np

from govee_win11.protocol import RGB

Frame = list[RGB]

RETRY_INTERVAL = 2.0
MIN_LEVEL = 0.35       # brightness floor while music plays (never near-black)
IDLE_LEVEL = 0.15      # silence / capture failure: dim palette colour
IDLE_AFTER = 2.0       # s without sound -> idle
SILENCE_RMS = 1e-4     # below this (full scale) a block counts as silence
BEAT_GAP = 0.25        # s between beats: <= 4 colour changes/s, inside the 5/s cap
LEVEL_INTERVAL = 0.25  # s between brightness updates (and after a colour change)
LEVEL_STEP = 0.15      # brightness moves smaller than this (of full scale) are not sent
STRONG_BEAT = 0.6      # bass >= this x the recent bass peak -> centre-outward ripple
RIPPLE_SPAN = 0.1      # s from the centre segments to the ends (3 frames at 20 fps)


class Analyzer:
    """Block-driven (time derived from sample count, so deterministic)."""

    def __init__(self, rate: int, sensitivity: float = 1.0):
        self.rate = rate
        self.sensitivity = max(sensitivity, 0.1)
        self._peak_vol = 1e-3
        self._peak_bass = 1e-6
        self._now = 0.0
        self._last_beat = -1e9
        self._volume = 0.0
        self.rms = 0.0       # of the last block (full scale), for silence detection
        self.strong = False  # last beat was strong (bass near the recent peak)
        self._history: collections.deque[tuple[float, float]] = collections.deque()

    def feed(self, block: np.ndarray) -> tuple[float, bool]:
        block = np.asarray(block, dtype=np.float32)
        if block.size == 0 or not np.all(np.isfinite(block)):
            return self._volume, False
        dt = block.size / self.rate
        self._now += dt
        decay = 0.95 ** dt
        rms = float(np.sqrt(np.mean(block ** 2)))
        self._peak_vol = max(self._peak_vol * decay, rms, 1e-3)
        volume = min(rms / self._peak_vol, 1.0)

        spec = np.abs(np.fft.rfft(block * np.hanning(block.size)))
        freqs = np.fft.rfftfreq(block.size, 1.0 / self.rate)
        sel = (freqs >= 20) & (freqs <= 150)
        bass = float(np.sqrt(np.sum(spec[sel] ** 2))) / block.size if sel.any() else 0.0
        self._peak_bass = max(self._peak_bass * decay, bass, 1e-6)

        while self._history and self._history[0][0] < self._now - 1.0:
            self._history.popleft()
        avg = (sum(b for _, b in self._history) / len(self._history)) if self._history else 0.0
        beat = (
            bass > 1.4 / self.sensitivity * avg
            and bass > 0.1 * self._peak_bass
            and bass > 1e-4
            and self._now - self._last_beat > BEAT_GAP
        )
        if beat:
            self._last_beat = self._now
            self.strong = bass >= STRONG_BEAT * self._peak_bass
        self._history.append((self._now, bass))
        self.rms = rms
        self._volume = volume
        return volume, bool(beat)


class MusicMode:
    fps = 20.0  # sampling rate for low beat latency; frames only change on events

    def __init__(self, n: int, palette: list[RGB], sensitivity: float = 1.0,
                 capture=None, clock: Callable[[], float] = time.monotonic):
        self.n = n
        self.palette = list(palette) or [(255, 255, 255)]
        self.sensitivity = sensitivity
        self._capture = capture
        self._clock = clock
        self._analyzer: Analyzer | None = None
        self._failed = False
        self._retry_at = 0.0
        self._reset()

    def _reset(self) -> None:
        self._idx = 0
        self._volume = 0.0
        self._level = MIN_LEVEL       # brightness the strip shows (between beats)
        self._changed_at = -1e9       # t of the last colour/brightness change
        self._last_sound = -1e9       # t of the last non-silent block
        self._beat_t = -1e9           # t of the last beat
        self._ripple_from: RGB | None = None  # colour before a rippling beat

    def open(self) -> None:
        self._reset()
        if self._capture is None:
            self._capture = LoopbackCapture()
        self._try_open()

    def _try_open(self) -> bool:
        try:
            opener = getattr(self._capture, "open", None)
            if opener:
                opener()
            self._failed = False
            return True
        except OSError:
            self._fail()
            return False

    def _fail(self) -> None:
        self._failed = True
        self._volume = 0.0
        self._retry_at = self._clock() + RETRY_INTERVAL
        self._close_capture()

    def _close_capture(self) -> None:
        closer = getattr(self._capture, "close", None)
        if closer:
            try:
                closer()
            except Exception:
                pass

    def close(self) -> None:
        self._close_capture()

    def _idle(self) -> Frame:
        self._level = MIN_LEVEL  # music resumes at the floor, then follows loudness
        return [self._color(IDLE_LEVEL)] * self.n

    def _color(self, level: float, idx: int | None = None) -> RGB:
        r, g, b = self.palette[(self._idx if idx is None else idx) % len(self.palette)]
        return (round(r * level), round(g * level), round(b * level))

    def next_frame(self, t: float) -> Frame:
        if self._capture is None:
            self._capture = LoopbackCapture()
        if self._failed:
            if self._clock() < self._retry_at or not self._try_open():
                return self._idle()
        try:
            block = self._capture()
        except OSError:
            self._fail()
            return self._idle()
        beat = False
        if block is not None and np.size(block):
            if self._analyzer is None:
                self._analyzer = Analyzer(getattr(self._capture, "rate", 44100), self.sensitivity)
            self._volume, beat = self._analyzer.feed(block)
            if self._analyzer.rms > SILENCE_RMS:
                self._last_sound = t
        if t - self._last_sound >= IDLE_AFTER:
            return self._idle()
        if beat:
            before = self._color(self._level)
            self._idx = (self._idx + 1) % len(self.palette)
            self._level = 1.0
            self._beat_t = t
            strong = self._analyzer is not None and self._analyzer.strong and self.n > 2
            self._ripple_from = before if strong else None
            # the ends of a ripple are commanded RIPPLE_SPAN later: count from there
            self._changed_at = t + (RIPPLE_SPAN if strong else 0.0)
        else:
            target = MIN_LEVEL + (1.0 - MIN_LEVEL) * self._volume
            if (t - self._changed_at >= LEVEL_INTERVAL - 1e-9
                    and abs(target - self._level) >= LEVEL_STEP):
                self._level = target
                self._changed_at = t
        now = self._color(self._level)
        if self._ripple_from is not None:
            reach = (t - self._beat_t) / RIPPLE_SPAN  # 0 -> centre only, 1 -> whole strip
            if reach < 1.0 - 1e-9:
                mid = (self.n - 1) / 2
                return [now if abs(i - mid) / mid <= reach + 1e-9 or abs(i - mid) <= 1
                        else self._ripple_from for i in range(self.n)]
            self._ripple_from = None
        return [now] * self.n


class LoopbackCapture:
    """WASAPI loopback of the default output device via PyAudioWPatch."""

    MAX_SECONDS = 0.5

    def __init__(self):
        self.rate = 44100
        self._pa = None
        self._stream = None
        self._lock = threading.Lock()
        self._buf: list[np.ndarray] = []
        self._channels = 2

    def open(self) -> None:
        self.close()
        try:
            import pyaudiowpatch as pyaudio
            pa = pyaudio.PyAudio()
            self._pa = pa
            dev = pa.get_default_wasapi_loopback()
            self.rate = int(dev["defaultSampleRate"])
            self._channels = max(int(dev["maxInputChannels"]), 1)
            self._stream = pa.open(
                format=pyaudio.paFloat32, channels=self._channels, rate=self.rate,
                input=True, input_device_index=dev["index"],
                frames_per_buffer=1024, stream_callback=self._on_audio)
        except Exception as e:
            self.close()
            raise OSError(str(e)) from e

    def _on_audio(self, data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio
        x = np.frombuffer(data, dtype=np.float32)
        if self._channels > 1:
            x = x[: x.size // self._channels * self._channels]
            x = x.reshape(-1, self._channels).mean(axis=1)
        with self._lock:
            self._buf.append(x.copy())
            total = sum(b.size for b in self._buf)
            while total > self.rate * self.MAX_SECONDS and len(self._buf) > 1:
                total -= self._buf.pop(0).size
        return (None, pyaudio.paContinue)

    def __call__(self) -> np.ndarray | None:
        if self._stream is None:
            raise OSError("capture not open")
        if not self._stream.is_active():
            raise OSError("stream stopped")
        with self._lock:
            if not self._buf:
                return None
            out = np.concatenate(self._buf)
            self._buf = []
        return out

    def close(self) -> None:
        stream, pa = self._stream, self._pa
        self._stream = self._pa = None
        self._buf = []
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:
            pass
        try:
            if pa is not None:
                pa.terminate()
        except Exception:
            pass
