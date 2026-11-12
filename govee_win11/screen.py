"""Screen sync: capture, dominant-colour extraction and the four sync modes.

Pipeline per frame (``ScreenSyncMode.next_frame``):
grab monitor (mss) -> stride-subsample to ~96 px wide -> screen colours ->
vibrance/gain adjust -> time-constant crossfade in linear light (``Smoother``):
- dominant (默认): k-means colours laid out over the strip in proportion and
  gradient order;
- region (区域): the screen cut into 6 vertical columns, left to right onto
  segments 0..n-1; each column's dominant/most saturated colour, straddling
  segments are the two columns' lights overlapping;
- fade (淡入淡出): the whole strip shows one screen colour at a time, cycling
  through the top 2-3 with additive crossfades;
- projection (投影): the two top screen colours in fixed alternating groups that
  swap in place -- the 投影 DIY effect's renderer (``effects.render``).
Colours only ever mix as light overlap (effects.py); no hue is interpolated.

Black captures (DRM-protected video, lock screen) simply produce a dark strip;
capture errors are swallowed and the strip fades to black.
"""
from __future__ import annotations

import colorsys
import itertools
import logging
import math
import threading
from typing import Any, Callable

import numpy as np

from . import effects
from .effects import _LIN, _ease, _encode, _light, _overlap, _scale
from .protocol import RGB

Frame = list[RGB]

log = logging.getLogger(__name__)

_BLACK: RGB = (0, 0, 0)

DARK_THRESHOLD = 0.08  # pixel brightness (HSV value, 0-1) below this is "dark"
MIN_LIT_FRACTION = 0.02  # fewer lit pixels than this -> the scene is black
KMEANS_ITERS = 8
SAMPLE_WIDTH = 96  # subsampled capture width
DOMINANT_K = 5
RESEED_DIST2 = 40.0 ** 2  # empty cluster re-seeded only on a pixel this far (RGB, squared) from all centres
GREY_SATURATION = 0.15  # below this HSV saturation a colour sorts as grey
HUE_CUT = 0.9  # tie-break for where an evenly spaced gradient starts (pink-purple)
COUNT_HYSTERESIS = 0.75  # keep last frame's segment counts while quotas stay this close
RESEED_RATIO = 2.0  # warm k-means inertia this many times a fresh seed's -> scene cut, re-seed
RESEED_MIN_GAIN = 100.0  # ...and at least this much lower mean squared error (RGB^2)
RESEED_CHECK_EVERY = 3  # frames between those comparisons (~2 ms each); every frame while a cut window is open
ORDER_RESORT_FRAMES = 50  # block order out of gradient order this long (5 s) -> one re-sort
CUT_JUMP = 45.0  # palette moved this far (RGB) since last frame -> the scene changed, re-sort at once
CUT_RATIO = 2.0  # ...and this many times the recent palette motion (fast continuous motion is no cut)
JUMP_LEVEL_RATE = 0.3  # weight of the newest frame in that recent-motion average
CUT_WINDOW = 10  # frames after a cut in which an out-of-order strip is re-sorted at once (once per cut)
HUE_MARGIN = 0.05  # backward hue steps up to this count as ties (no flip-flop on near ties)
ORDER_SLACK = 0.1  # re-sort only if the order spans this much more of the hue wheel than the best one

# Calibration-diagram coordinate system (shared with the calibration wizard).
MON_X0, MON_X1 = 0.15, 0.85
MON_Y0, MON_Y1 = 0.0, 0.7
DESK_Y0 = 0.8
EDGE_BAND = 0.10  # depth of the screen-edge strip sampled for monitor points
EDGE_SPAN = 0.20  # length of that strip along the edge
DESK_BAND = 0.15  # height of the bottom-of-screen area sampled for desk points
DESK_SPAN = 0.20  # width of that area

# Vibrance strength (see ``adjust``). 2.0 measured on synthetic scenes (task-16 report):
# pastel s 0.23 -> 0.44, skin 0.37 -> 0.61, muddy brown 0.41 -> 0.64, dark-scene blue
# 0.43 -> 0.65, while already vivid colours only go 0.87 -> 0.93 (the old linear x1.3
# clipped those to s = 1, pure primaries) and greys stay grey.
DEFAULT_SATURATION = 2.0
VIBRANCE_RAMP = 0.1  # boost fades in over this much saturation above GREY_SATURATION
# 过渡时间 slider: seconds until a change is ~95 % there (3 time constants).
TRANSITION_MIN, TRANSITION_MAX = 0.3, 5.0
DEFAULT_TRANSITION = 1.5
CUT_FADE_MIN = 0.3  # s; a scene cut fades in over max(CUT_FADE_MIN, 过渡时间 / 2)
CUT_DIST = 60.0  # mean RGB distance between consecutive targets -> scene cut
CUT_TAU = 0.25   # Smoother default: time constant while a cut fades in (95 % after 0.75 s)
CUT_HOLD = 0.8   # Smoother default: s the faster time constant lasts after a cut
MAX_DT = 1.0     # s; longer frame gaps (stall, sleep) count as this

MODES = ("dominant", "fade", "projection", "region")
REGION_COLUMNS = 6
REGION_K = 3          # clusters per column
REGION_STICKY = 1.2   # score bonus for the cluster nearest last frame's pick (no flip-flop)
REGION_STICKY_DIST = 60.0
REGION_MIN_SHARE = 0.12  # a cluster other than the largest needs this much of the column
FADE_COLOURS = 3      # fade cycles through up to this many screen colours
FADE_HOLD = 3.0       # s each colour holds (tempo 50)
FADE_XFADE = 1.5      # s crossfade to the next one (tempo 50)
PALETTE_MIN_SHARE = 0.10  # 2nd/3rd colour needs this much of the lit area
GREY_DROP_SHARE = 0.10    # greys are ignored once a saturated colour covers this much
DISTINCT = 40.0       # RGB distance below which two screen colours count as one
PROJECTION_DIM = 0.45 ** 2.2  # single colour: other groups at ~45 % (sRGB) intensity

DEFAULT_SETTINGS: dict[str, Any] = {
    "monitor": 1,
    "saturation": DEFAULT_SATURATION,
    "transition_s": DEFAULT_TRANSITION,
    "gain": [1.0, 1.0, 1.0],
    "mode": "dominant",
    "tempo": 50,
}


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _clamp255(v: float) -> int:
    return max(0, min(255, int(round(v))))


# ---------------------------------------------------------------------------
# Colour extraction
# ---------------------------------------------------------------------------

def _brightness(pixels: np.ndarray) -> np.ndarray:
    """HSV value (max channel) in 0-1. Max channel, not luma, so saturated
    blue (luma ~0.07) is not mistaken for darkness."""
    return pixels.max(axis=-1).astype(np.float32) / 255.0


def _kmeans_pp(x: np.ndarray, k: int, seed: int) -> np.ndarray:
    """k-means++ init. Always k rows: if the image has fewer distinct colours
    than k, the spare rows duplicate the first centre and stay empty."""
    rng = np.random.default_rng(seed)
    n = len(x)
    centers = [x[rng.integers(n)]]
    d2 = ((x - centers[0]) ** 2).sum(axis=1)
    for _ in range(1, k):
        total = float(d2.sum())
        if total <= 0.0:  # fewer distinct colours than k
            break
        c = x[rng.choice(n, p=d2 / total)]
        centers.append(c)
        d2 = np.minimum(d2, ((x - c) ** 2).sum(axis=1))
    centers += [centers[0]] * (k - len(centers))
    return np.array(centers, dtype=np.float64)


def _kmeans(
    x: np.ndarray, k: int, seed: int, init: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Fixed-iteration Lloyd k-means. Returns (centers (k×3), labels).

    ``init`` warm-starts from given centres (e.g. the previous frame's), so a
    slowly changing image keeps the same clusters in the same slots; without
    it, k-means++ seeds the centres. A cluster that empties is re-seeded on
    the pixel farthest from every centre, if that pixel is far enough away.
    """
    if init is not None and np.shape(init) == (k, 3):
        c = np.array(init, dtype=np.float64)
    else:
        c = _kmeans_pp(x, k, seed)
    x2 = (x * x).sum(axis=1)
    rows = np.arange(len(x))

    def dists(cc: np.ndarray) -> np.ndarray:
        # squared distance minus |x|^2 (constant per pixel: same argmin)
        return (cc * cc).sum(axis=1)[None, :] - 2.0 * (x @ cc.T)

    prev_labels = None
    for _ in range(KMEANS_ITERS):
        d = dists(c)
        labels = d.argmin(axis=1)
        counts = np.bincount(labels, minlength=k)
        nonempty = counts > 0
        for ch in range(3):
            sums = np.bincount(labels, weights=x[:, ch], minlength=k)
            c[nonempty, ch] = sums[nonempty] / counts[nonempty]
        empty = np.flatnonzero(~nonempty)
        reseeded = False
        if len(empty):
            dmin = d[rows, labels] + x2
            for j in empty:
                far = int(dmin.argmax())
                if dmin[far] <= RESEED_DIST2:
                    break
                c[j] = x[far]
                reseeded = True
                dmin = np.minimum(dmin, ((x - c[j]) ** 2).sum(axis=1))
        if not reseeded and prev_labels is not None and np.array_equal(labels, prev_labels):
            # Converged: c was already the mean of these labels, so every
            # further iteration would repeat this one exactly.
            break
        prev_labels = labels
    return c, dists(c).argmin(axis=1)


def _inertia(x: np.ndarray, centers: np.ndarray) -> float:
    """Mean squared RGB distance from each pixel to its nearest centre."""
    d = (x * x).sum(axis=1)[:, None] - 2.0 * (x @ centers.T) + (centers * centers).sum(axis=1)[None, :]
    return float(max(0.0, d.min(axis=1).mean()))


def _lit_pixels(img: np.ndarray) -> tuple[np.ndarray | None, np.ndarray]:
    """(lit pixels as float N×3 or None if the scene counts as black,
    brightness of all pixels)."""
    pixels = np.asarray(img).reshape(-1, 3)
    v = _brightness(pixels)
    lit = pixels[v >= DARK_THRESHOLD]
    if len(lit) == 0 or len(lit) < MIN_LIT_FRACTION * len(pixels):
        return None, v
    return lit.astype(np.float64), v


def dominant_colors(img: np.ndarray, k: int = 5, seed: int = 0) -> list[tuple[RGB, float]]:
    """Dominant colours of an H×W×3 uint8 image as (colour, share), share
    descending and summing to 1. Dark pixels are ignored; a (nearly) black
    image yields ``[((0, 0, 0), 1.0)]``."""
    x, _v = _lit_pixels(img)
    if x is None:
        return [(_BLACK, 1.0)]
    centers, labels = _kmeans(x, max(1, k), seed)
    counts = np.bincount(labels, minlength=len(centers))
    total = float(counts.sum())
    out = [(_to_rgb(c), cnt / total) for c, cnt in zip(centers, counts) if cnt > 0]
    out.sort(key=lambda cp: cp[1], reverse=True)
    return out


def _to_rgb(c: np.ndarray) -> RGB:
    return (_clamp255(c[0]), _clamp255(c[1]), _clamp255(c[2]))


def _hue_key(rgb: RGB) -> tuple[int, float, float]:
    """Gradient sort key. Near-greys form one bucket (their hue is noise), and
    the hue circle is cut at pink-purple so common reds/oranges don't straddle
    the wrap point."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255.0 for c in rgb))
    if s < GREY_SATURATION:
        return (0, 0.0, v)
    return (1, (h - HUE_CUT) % 1.0, v)


def _turn(hues: list[float]) -> float:
    """How far round the hue wheel a block sequence travels going forward.
    Backward steps up to HUE_MARGIN count as ties."""
    total = 0.0
    for a, b in zip(hues, hues[1:]):
        step = (b - a) % 1.0
        total += 0.0 if step > 1.0 - HUE_MARGIN else step
    return total


def _gradient_order(keys: list[tuple[int, float, float]]) -> list[int]:
    """Indices of colours in gradient order: greys first (by value), then the
    chromatic colours round the hue wheel, starting just after the largest
    hue gap so the strip spans as little of the wheel as possible. Hue is
    circular; no fixed cut point is involved except as a tie-break."""
    greys = sorted((i for i, k in enumerate(keys) if k[0] == 0), key=lambda i: keys[i][2])
    chrom = sorted((i for i, k in enumerate(keys) if k[0] == 1), key=lambda i: (keys[i][1], keys[i][2]))
    m = len(chrom)
    if m > 1:
        gaps = [(keys[chrom[(j + 1) % m]][1] - keys[chrom[j]][1]) % 1.0 for j in range(m)]
        widest = max(gaps)
        # last widest gap in key order (the wrap gap wins ties) -> evenly
        # spaced colours start nearest the cut
        start = (max(j for j in range(m) if gaps[j] >= widest - 1e-6) + 1) % m
        chrom = chrom[start:] + chrom[:start]
    return greys + chrom


def _palette_jump(
    c0: np.ndarray, n0: np.ndarray, c1: np.ndarray, n1: np.ndarray
) -> float:
    """How far the palette moved between two frames (RGB units): the
    share-weighted mean distance from each colour of one frame to the nearest
    colour of the other, the larger of the two directions, so colours that
    appeared (new -> old) and colours that vanished (old -> new) both count.
    Cluster identity is ignored, so clusters swapping roles or a drifting
    palette score low (synthetic fast drift: <= ~29) while a hard cut scores
    high (>= ~64 on the test scenes)."""
    a, b = n0 > 0, n1 > 0
    if not a.any() or not b.any():
        return 0.0
    p, q = c0[a], c1[b]
    d = np.sqrt(((p[:, None, :] - q[None, :, :]) ** 2).sum(axis=2))
    w0 = n0[a] / n0[a].sum()
    w1 = n1[b] / n1[b].sum()
    return float(max((w0 * d.min(axis=1)).sum(), (w1 * d.min(axis=0)).sum()))


def _out_of_order(keys: list[tuple[int, float, float]]) -> bool:
    """True if a block sequence is clearly not a gradient.

    The strip is linear and hue is circular, so a gradient may run either way
    round the wheel and may start anywhere: what matters is how much of the
    wheel it spans compared with the best order (``_gradient_order``). A
    colour crossing any fixed cut point (hue-cycling wallpapers) is no
    inversion. Greys are ignored (they don't break a gradient).
    """
    hues = [k[1] for k in keys if k[0] == 1]
    if len(hues) < 3:  # one or two colours are always a gradient
        return False
    ordered = sorted(hues)
    best = 1.0 - max((ordered[(j + 1) % len(ordered)] - ordered[j]) % 1.0 for j in range(len(ordered)))
    turn = min(_turn(hues), _turn(hues[::-1]))
    return turn > best + ORDER_SLACK


def _allocate(shares: list[float], n: int, prev: list[int] | None = None) -> list[int]:
    """Segments per colour (len(shares) <= n): largest remainder, at least 1
    each.

    With ``prev`` (same colours, last frame) the counts are updated
    incrementally instead: nothing changes while every quota stays within
    COUNT_HYSTERESIS of its count (a quota hovering around x.5 does not make
    a boundary segment flip every frame), and otherwise single segments move
    from the most over- to the most under-allocated colour — one per frame
    unless some count is off by 1.5 or more — so a slow drift never
    reshuffles several block boundaries at once."""
    total = sum(max(p, 0.0) for p in shares)
    if total <= 0:
        quotas = [n / len(shares)] * len(shares)
    else:
        quotas = [max(p, 0.0) / total * n for p in shares]
    if prev is not None and len(prev) == len(quotas) and sum(prev) == n and min(prev) >= 1:
        counts = list(prev)
        moved = 0
        while True:
            dev = [q - c for q, c in zip(quotas, counts)]
            worst = max(abs(d) for d in dev)
            if worst < COUNT_HYSTERESIS or (moved and worst < 1.5):
                return counts
            r = max(range(len(dev)), key=lambda j: dev[j])
            donors = [j for j in range(len(dev)) if counts[j] > 1]
            if not donors:
                return counts
            d = min(donors, key=lambda j: dev[j])
            if dev[r] - dev[d] <= 1.0:  # a move would not reduce the error
                return counts
            counts[r] += 1
            counts[d] -= 1
            moved += 1
    counts = [max(1, math.floor(q)) for q in quotas]
    while sum(counts) < n:
        i = max(range(len(quotas)), key=lambda j: quotas[j] - counts[j])
        counts[i] += 1
    while sum(counts) > n:
        i = min((j for j in range(len(quotas)) if counts[j] > 1), key=lambda j: quotas[j] - counts[j])
        counts[i] -= 1
    return counts


def distribute(colors: list[tuple[RGB, float]], n: int) -> Frame:
    """Lay colours out over n segments in proportion to their share (largest
    remainder, at least one segment each; only the top n if more colours than
    segments), as contiguous blocks in gradient order (``_gradient_order``)."""
    if n <= 0:
        return []
    if not colors:
        return [_BLACK] * n
    top = sorted(colors, key=lambda cp: cp[1], reverse=True)[:n]
    counts = _allocate([p for _, p in top], n)
    order = _gradient_order([_hue_key(c) for c, _ in top])
    frame: Frame = []
    for i in order:
        frame.extend([top[i][0]] * counts[i])
    return frame


def _vibrance(s: float, amount: float) -> float:
    """HSV saturation after the boost. ``amount`` > 1: vibrance, ``s ** (1 / amount)``
    -- low saturations are lifted relatively more than high ones (x1.9 at 0.23, x1.07 at
    0.87 for amount 2) and nothing is pushed all the way to a pure primary. Greys below
    GREY_SATURATION are left alone (their hue is noise: no colour invented from grey);
    the boost fades in over VIBRANCE_RAMP above that. ``amount`` <= 1: plain scaling
    (desaturate; 0 = grey)."""
    amount = max(0.0, amount)
    if amount <= 1.0:
        return _clamp01(s * amount)
    w = _clamp01((s - GREY_SATURATION) / VIBRANCE_RAMP)
    return _clamp01(s + w * (s ** (1.0 / amount) - s))


def adjust(rgb: RGB, saturation: float, gain: tuple[float, float, float]) -> RGB:
    """Vibrance boost of HSV saturation (value kept, see ``_vibrance``), then
    per-channel gain; clamped."""
    r, g, b = (c / 255.0 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    s = _vibrance(s, saturation)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (
        _clamp255(r * 255.0 * gain[0]),
        _clamp255(g * 255.0 * gain[1]),
        _clamp255(b * 255.0 * gain[2]),
    )


def clamp_transition(value: Any) -> float:
    """过渡时间 setting -> seconds in [TRANSITION_MIN, TRANSITION_MAX] (junk -> default)."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TRANSITION
    if math.isnan(x):
        return DEFAULT_TRANSITION
    return min(TRANSITION_MAX, max(TRANSITION_MIN, x))


def transition_tau(seconds: float) -> float:
    """过渡时间 (95 % time) -> crossfade time constant: 1 - e^-3 = 95 %."""
    return seconds / 3.0


def cut_fade(seconds: float) -> float:
    """Fade-in time of a scene cut for a 过渡时间 setting: max(0.3, T / 2), so a long
    setting also softens cuts while short ones stay snappy."""
    return max(CUT_FADE_MIN, seconds / 2.0)


_LIN_ARR = np.array(_LIN)
_SRGB_ARR = np.arange(256) / 255.0


def _np_lin_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def _np_srgb_to_lin(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92, np.power((x + 0.055) / 1.055, 2.4))


class Smoother:
    """Time-constant crossfade toward the target, in linear light (or, with the
    perceptual 混合方式 -- ``effects.get_mix()``, read every step -- in sRGB).

    Each step the shown light moves ``1 - exp(-dt / tau)`` of the way to the target, so
    the curve depends on elapsed time only, not on the frame rate. The overlap is the
    same peak-preserving additive light as ``effects._light``: the linear-light sum is
    rescaled so its brightest channel follows the (equally smoothed) brightest channel
    of the lights -- red fading to blue passes through a full-brightness magenta, never
    a computed in-between hue and never a dim dip. A scene cut (mean target jump above
    CUT_DIST) fades in faster, with ``cut_tau`` for ``cut_hold`` seconds. ``tau`` 0 = no
    smoothing."""

    def __init__(self, tau: float, cut_tau: float = CUT_TAU, cut_hold: float = CUT_HOLD):
        self.tau = max(0.0, float(tau))
        self.cut_tau = max(1e-6, float(cut_tau))
        self.cut_hold = max(0.0, float(cut_hold))
        self._lin: np.ndarray | None = None
        self._peak: np.ndarray | None = None
        self._last: np.ndarray | None = None
        self._cut_left = 0.0
        self._space = effects.get_mix()  # space _lin / _peak are kept in

    @classmethod
    def for_transition(cls, seconds: float) -> "Smoother":
        """Smoother for a 过渡时间 of ``seconds`` (changes 95 % there after it, scene
        cuts after ``cut_fade(seconds)``)."""
        cut = cut_fade(seconds)
        return cls(transition_tau(seconds), transition_tau(cut), cut)

    def _follow_mix(self) -> bool:
        """Switch the state to the current mix space (live setting change): convert, so
        the crossfade continues from what is shown. True = linear light."""
        space = effects.get_mix()
        if space != self._space and self._lin is not None:
            conv = _np_lin_to_srgb if self._space == "linear" else _np_srgb_to_lin
            self._lin, self._peak = conv(self._lin), conv(self._peak)
        self._space = space
        return space == "linear"

    def step(self, target: Frame, dt: float) -> Frame:
        t = np.clip(np.asarray(target, dtype=np.int64).reshape(-1, 3), 0, 255)
        linear = self._follow_mix()
        lin = (_LIN_ARR if linear else _SRGB_ARR)[t]
        peak = lin.max(axis=1) if len(t) else np.zeros(0)
        if self._lin is None or self._lin.shape != lin.shape or self.tau <= 0.0:
            self._lin, self._peak, self._last = lin.copy(), peak.copy(), t
            return [(int(r), int(g), int(b)) for r, g, b in t]
        if len(t) and float(np.sqrt(((t - self._last) ** 2).sum(axis=1)).mean()) > CUT_DIST:
            self._cut_left = self.cut_hold
        dt = min(max(0.0, float(dt)), MAX_DT)
        tau = min(self.tau, self.cut_tau) if self._cut_left > 0.0 else self.tau
        self._cut_left -= dt
        a = 1.0 - math.exp(-dt / tau)
        self._lin += a * (lin - self._lin)
        self._peak += a * (peak - self._peak)
        self._last = t
        m = self._lin.max(axis=1)
        scale = np.divide(self._peak, m, out=np.zeros_like(m), where=m > 1e-12)
        out = self._lin * scale[:, None]
        if not linear:
            out = np.rint(np.clip(out, 0.0, 1.0) * 255.0).astype(np.int64)
            return [(int(r), int(g), int(b)) for r, g, b in out]
        return [(_encode(r), _encode(g), _encode(b)) for r, g, b in out]


# ---------------------------------------------------------------------------
# Region mode geometry
# ---------------------------------------------------------------------------

def _span(center: float, width: float) -> tuple[float, float]:
    lo = min(max(center - width / 2, 0.0), 1.0 - width)
    return lo, lo + width


def region_for_point(x: float, y: float) -> tuple[float, float, float, float]:
    """Calibration-diagram point -> normalised screen sample rect
    ``(left, top, right, bottom)`` in 0-1.

    Desk zone (y >= 0.8): bottom 15% of the screen, 20% wide, centred on x.
    Otherwise the point is clamped into the monitor rectangle and mapped to
    a 10%-deep strip along the nearest screen edge, 20% long, centred on it.
    """
    u = _clamp01((x - MON_X0) / (MON_X1 - MON_X0))
    if y >= DESK_Y0:
        x0, x1 = _span(u, DESK_SPAN)
        return (x0, 1.0 - DESK_BAND, x1, 1.0)
    v = _clamp01((y - MON_Y0) / (MON_Y1 - MON_Y0))
    edge = min((u, 0), (1.0 - u, 1), (v, 2), (1.0 - v, 3))[1]
    if edge == 0:  # left
        y0, y1 = _span(v, EDGE_SPAN)
        return (0.0, y0, EDGE_BAND, y1)
    if edge == 1:  # right
        y0, y1 = _span(v, EDGE_SPAN)
        return (1.0 - EDGE_BAND, y0, 1.0, y1)
    x0, x1 = _span(u, EDGE_SPAN)
    if edge == 2:  # top
        return (x0, 0.0, x1, EDGE_BAND)
    return (x0, 1.0 - EDGE_BAND, x1, 1.0)  # bottom


def _rect_mean(img: np.ndarray, rect: tuple[float, float, float, float]) -> RGB:
    h, w = img.shape[:2]
    x0, y0, x1, y1 = rect
    xa = min(int(x0 * w), w - 1)
    ya = min(int(y0 * h), h - 1)
    xb = min(w, max(xa + 1, math.ceil(x1 * w - 1e-9)))
    yb = min(h, max(ya + 1, math.ceil(y1 * h - 1e-9)))
    m = img[ya:yb, xa:xb].reshape(-1, 3).mean(axis=0)
    return (_clamp255(m[0]), _clamp255(m[1]), _clamp255(m[2]))


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def _new_sct() -> Any:
    import mss

    cls = getattr(mss, "MSS", None) or mss.mss  # mss>=10.2 deprecates mss.mss
    return cls()


def list_monitors() -> list[dict]:
    """Real monitors from mss: ``{"index", "width", "height", "primary"}``;
    index is the mss monitor index (1 = first real monitor)."""
    sct = _new_sct()
    try:
        mons = list(sct.monitors)[1:]
    finally:
        sct.close()
    has_flag = any("is_primary" in m for m in mons)
    return [
        {
            "index": i,
            "width": int(m["width"]),
            "height": int(m["height"]),
            "primary": bool(m.get("is_primary", False)) if has_flag else i == 1,
        }
        for i, m in enumerate(mons, start=1)
    ]


def _downsample(img: np.ndarray, width: int = SAMPLE_WIDTH) -> np.ndarray:
    step = max(1, img.shape[1] // width)
    return img[::step, ::step]


def _shot_to_rgb(shot: Any) -> np.ndarray:
    """mss ScreenShot (BGRA) -> subsampled H×W×3 RGB array."""
    buf = getattr(shot, "raw", None)
    if buf is None:
        buf = shot.bgra
    bgra = np.frombuffer(buf, dtype=np.uint8).reshape(shot.height, shot.width, 4)
    return np.ascontiguousarray(_downsample(bgra)[..., 2::-1])


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

class ScreenSyncMode:
    """Continuous mode (structural ContinuousMode) following the screen."""

    fps: float = 10.0

    def __init__(
        self,
        n: int,
        settings_screen: dict,
        calibration: list[list[float]] | None,
        grab: Callable[[], np.ndarray] | None = None,
    ):
        s = {**DEFAULT_SETTINGS, **(settings_screen or {})}
        self.n = n
        try:
            self.monitor = int(s["monitor"])
        except (TypeError, ValueError):
            self.monitor = 1
        try:
            self.saturation = float(s["saturation"])
        except (TypeError, ValueError):
            self.saturation = DEFAULT_SETTINGS["saturation"]
        try:
            gain = tuple(float(g) for g in s["gain"])
        except (TypeError, ValueError):
            gain = ()
        self.gain: tuple[float, float, float] = gain if len(gain) == 3 else (1.0, 1.0, 1.0)
        self.transition_s = clamp_transition(s["transition_s"])
        self.tau = transition_tau(self.transition_s)
        cut = cut_fade(self.transition_s)
        self.cut_tau, self.cut_hold = transition_tau(cut), cut
        self.smoother = self._new_smoother()
        self._last_t: float | None = None
        self.mode = s["mode"] if s["mode"] in MODES else "dominant"
        try:
            self.tempo = min(100.0, max(0.0, float(s["tempo"])))
        except (TypeError, ValueError):
            self.tempo = 50.0
        # 节奏 0..100 -> speed 0.5..2 (50 = 1): scales fade/projection hold and crossfade
        self.speed = 2.0 ** ((self.tempo - 50.0) / 50.0)
        self.group = effects.PROJECTION_GROUP
        # ``calibration`` is accepted for compatibility but no mode uses it any more:
        # region = 6 screen columns
        self._grab = grab
        self._sct_lock = threading.Lock()
        self._scts: dict[int, Any] = {}  # mss instances are per thread
        self._grab_failing = False
        self._reset_tracking()

    def _reset_tracking(self) -> None:
        # Dominant-mode cluster tracking: centres warm-start the next frame's
        # k-means; slot order (hue-sorted once, at seeding) is the block
        # layout order; last allocation feeds the count hysteresis.
        self._centers: np.ndarray | None = None
        self._counts: np.ndarray | None = None
        self._order: list[int] = []
        self._alloc: tuple[tuple[int, ...], list[int]] | None = None
        self._unordered_frames = 0
        self._cut_frames = 0  # frames left in the post-cut fast re-sort window
        self._jump_level = 0.0  # recent palette motion per frame (RGB, moving average)
        self._frames = 0
        # region: last picked cluster centre per column
        self._col_prev: list[np.ndarray | None] = [None] * REGION_COLUMNS
        # fade / projection: screen palette tracking, colour slots, sequencer
        self._pal_centers: np.ndarray | None = None
        self._pal_frames = 0
        self._slots: list[RGB] | None = None
        self._t0: float | None = None
        self._cur = 0
        self._nxt: int | None = None
        self._step_u = 0.0

    # -- lifecycle --------------------------------------------------------
    def _new_smoother(self) -> Smoother:
        return Smoother(self.tau, self.cut_tau, self.cut_hold)

    def open(self) -> None:
        self._reset_tracking()
        self.smoother = self._new_smoother()
        self._last_t = None
        if self._grab is None:
            self._get_sct()

    def close(self) -> None:
        with self._sct_lock:
            scts, self._scts = list(self._scts.values()), {}
        for sct in scts:
            try:
                sct.close()
            except Exception:  # pragma: no cover - best effort
                log.debug("mss close failed", exc_info=True)

    # -- capture ----------------------------------------------------------
    def _get_sct(self) -> Any:
        tid = threading.get_ident()
        with self._sct_lock:
            sct = self._scts.get(tid)
        if sct is None:
            sct = _new_sct()
            with self._sct_lock:
                self._scts[tid] = sct
        return sct

    def _drop_sct(self) -> None:
        with self._sct_lock:
            sct = self._scts.pop(threading.get_ident(), None)
        if sct is not None:
            try:
                sct.close()
            except Exception:  # pragma: no cover
                pass

    def _capture(self) -> np.ndarray:
        if self._grab is not None:
            return _downsample(np.asarray(self._grab())[..., :3])
        sct = self._get_sct()
        mons = sct.monitors
        idx = self.monitor if 1 <= self.monitor < len(mons) else 1
        idx = min(idx, len(mons) - 1)
        return _shot_to_rgb(sct.grab(mons[idx]))

    # -- frames -----------------------------------------------------------
    def _dominant_target(self, img: np.ndarray) -> Frame:
        """Dominant colours laid out over the strip, stable across frames:
        k-means is warm-started from the previous centres, each cluster keeps
        its slot, and blocks are laid out in a fixed slot order instead of
        being re-sorted by hue every frame. Scene cuts (warm result much worse
        than a fresh seed) re-seed; after a palette jump (any cut, re-seeded
        or not) an out-of-order strip is re-sorted at once, once per cut;
        otherwise a persistently non-gradient order is re-sorted once, after
        ORDER_RESORT_FRAMES."""
        x, v = _lit_pixels(img)
        if x is None or self.n <= 0:
            return [_BLACK] * self.n
        centers, labels = _kmeans(x, DOMINANT_K, 0, init=self._centers)
        counts = np.bincount(labels, minlength=DOMINANT_K)
        reseed = self._centers is None
        jump = 0.0
        if not reseed and self._counts is not None:
            jump = _palette_jump(self._centers, self._counts, centers, counts)
        # A cut is a jump that stands out from the recent motion: fast but
        # continuous motion (video, fast-moving wallpapers) moves the palette
        # a lot every frame and must not keep opening cut windows.
        if jump > CUT_JUMP and jump > CUT_RATIO * self._jump_level:
            self._cut_frames = CUT_WINDOW
        self._jump_level += JUMP_LEVEL_RATE * (jump - self._jump_level)
        self._frames += 1
        if not reseed and (self._cut_frames > 0 or self._frames % RESEED_CHECK_EVERY == 0):
            # Warm starts can get stuck in a bad local minimum after a hard
            # scene cut (wallpaper playlist, video wallpaper). Compare with a
            # fresh k-means++ run on the same pixels: during slow drift the
            # warm result is about as good (ratio <= ~1.1); after a cut it is
            # several times worse -> take the fresh clusters and re-sort.
            # Every 3rd frame, and every frame while a cut window is open.
            fresh, fresh_labels = _kmeans(x, DOMINANT_K, 0)
            warm_err, fresh_err = _inertia(x, centers), _inertia(x, fresh)
            if warm_err > RESEED_RATIO * fresh_err and warm_err - fresh_err > RESEED_MIN_GAIN:
                centers, labels, reseed = fresh, fresh_labels, True
                counts = np.bincount(labels, minlength=DOMINANT_K)
                self._cut_frames = CUT_WINDOW
        keys = [_hue_key(_to_rgb(c)) for c in centers]
        resort = reseed
        if not resort:
            # Clusters can drift (crossfades) or swap roles, so the fixed slot
            # order may stop being a gradient. During slow change, re-sort only
            # once that has persisted for a while: a single, smoothed
            # reshuffle, not flicker. Right after a cut (palette jump) the
            # screen changed anyway, so re-sort as soon as it is out of order;
            # warm k-means often finds the new colours by itself, so the
            # re-seed path above does not always fire on a cut.
            if _out_of_order([keys[j] for j in self._order if counts[j] > 0]):
                self._unordered_frames += 1
            else:
                self._unordered_frames = 0
            need = 1 if self._cut_frames > 0 else ORDER_RESORT_FRAMES
            resort = self._unordered_frames >= need
        if resort:
            # order the live clusters; empty (stale) ones go last, so they
            # cannot sit in the widest hue gap and spoil the order
            live = [j for j in range(DOMINANT_K) if counts[j] > 0]
            order = [live[i] for i in _gradient_order([keys[j] for j in live])]
            if not reseed:
                # A gradient reads the same either way round: keep the
                # direction that leaves the most segments on the same cluster.
                order = max((order, order[::-1]), key=lambda o: self._overlap(o, counts))
            else:
                self._alloc = None  # new clusters in the old slots
            self._order = order + [j for j in range(DOMINANT_K) if counts[j] == 0]
            self._unordered_frames = 0
            # One immediate re-sort per cut: the window closes, so clusters
            # still settling after the cut cannot re-sort the strip again
            # (they fall back to the ORDER_RESORT_FRAMES persistence).
            self._cut_frames = 0
        self._cut_frames = max(0, self._cut_frames - 1)
        self._counts = counts
        self._centers = centers
        active = self._active(self._order, counts)
        key = tuple(active)
        prev = self._alloc[1] if self._alloc is not None and self._alloc[0] == key else None
        seg = _allocate([float(counts[j]) for j in active], self.n, prev)
        self._alloc = (key, seg)
        # Dark scenes -> dim strip: scale by the light lost to the dropped
        # dark pixels (overall mean brightness / mean brightness of the lit
        # pixels the clusters were built from).
        scale = min(1.0, float(v.mean()) / float(_brightness(x).mean()))
        frame: Frame = []
        for j, cnt in zip(active, seg):
            frame.extend([_to_rgb(centers[j] * scale)] * cnt)
        return frame

    def _active(self, order: list[int], counts: np.ndarray) -> list[int]:
        """Non-empty clusters in ``order``; only the n largest if more than n."""
        active = [j for j in order if counts[j] > 0]
        if len(active) > self.n:
            keep = set(sorted(active, key=lambda j: -counts[j])[: self.n])
            active = [j for j in active if j in keep]
        return active

    def _layout(self, order: list[int], counts: np.ndarray) -> list[int]:
        """Cluster index per segment if laid out in ``order`` now."""
        active = self._active(order, counts)
        seg = _allocate([float(counts[j]) for j in active], self.n)
        return [j for j, c in zip(active, seg) for _ in range(c)]

    def _overlap(self, order: list[int], counts: np.ndarray) -> int:
        old = self._layout(self._order, counts)
        return sum(a == b for a, b in zip(old, self._layout(order, counts)))

    # -- region -----------------------------------------------------------
    def _column_colour(self, c: int, sub: np.ndarray) -> RGB:
        """Representative colour of one screen column: the cluster with the best
        share x (0.1 + saturation)^2 -- the dominant colour, but a bright saturated
        object beats a larger dull background (no muddy mean) -- dimmed (gently) by
        the dark area of the column. Only the largest cluster, or one covering at least
        REGION_MIN_SHARE, may win: a thin saturated line on a white page is a speck,
        not the column's colour."""
        x, v = _lit_pixels(sub)
        if x is None:
            self._col_prev[c] = None
            return _BLACK
        centers, labels = _kmeans(x, REGION_K, 0)
        counts = np.bincount(labels, minlength=REGION_K)
        total = float(counts.sum())
        prev = self._col_prev[c]
        largest = int(counts.argmax())
        best, best_score = largest, -1.0
        for j in range(REGION_K):
            if counts[j] == 0 or (j != largest and counts[j] / total < REGION_MIN_SHARE):
                continue
            sat = colorsys.rgb_to_hsv(*(float(ch) / 255.0 for ch in np.clip(centers[j], 0, 255)))[1]
            score = counts[j] / total * (0.1 + sat) ** 2
            if prev is not None and float(np.sqrt(((centers[j] - prev) ** 2).sum())) < REGION_STICKY_DIST:
                score *= REGION_STICKY
            if score > best_score:
                best, best_score = j, score
        self._col_prev[c] = centers[best]
        scale = math.sqrt(min(1.0, float(v.mean()) / float(_brightness(x).mean())))
        return _to_rgb(centers[best] * scale)

    def _region_target(self, img: np.ndarray | None) -> Frame:
        """6 vertical columns left -> right onto segments 0..n-1 (2.5 segments per
        column on 15); a segment straddling two columns shows both columns' lights
        overlapping, weighted by how much of it each covers."""
        if img is None or self.n <= 0:
            return [_BLACK] * self.n
        w = img.shape[1]
        cols = []
        for c in range(REGION_COLUMNS):
            x0 = c * w // REGION_COLUMNS
            x1 = max(x0 + 1, (c + 1) * w // REGION_COLUMNS)
            cols.append(adjust(self._column_colour(c, img[:, x0:x1]), self.saturation, self.gain))
        frame: Frame = []
        for i in range(self.n):
            a, b = i * REGION_COLUMNS / self.n, (i + 1) * REGION_COLUMNS / self.n
            lights = []
            for c in range(int(a), min(REGION_COLUMNS, math.ceil(b))):
                cover = min(b, c + 1) - max(a, c)
                if cover > 1e-9:
                    lights.append((cols[c], cover / (b - a)))
            frame.append(_light(lights))
        return frame

    # -- fade / projection ---------------------------------------------------
    def _screen_palette(self, img: np.ndarray | None, k: int) -> list[RGB]:
        """Up to k screen colours, biggest first (adjusted; [] = black screen).
        Near-greys are ignored once a saturated colour covers GREY_DROP_SHARE; a 2nd
        or 3rd colour needs PALETTE_MIN_SHARE and must differ from those before it.
        k-means is warm-started (stable colours) and re-seeded after a scene cut."""
        x, v = _lit_pixels(img) if img is not None else (None, None)
        if x is None:
            self._pal_centers = None
            return []
        centers, labels = _kmeans(x, DOMINANT_K, 0, init=self._pal_centers)
        self._pal_frames += 1
        if self._pal_centers is not None and self._pal_frames % RESEED_CHECK_EVERY == 0:
            fresh, fresh_labels = _kmeans(x, DOMINANT_K, 0)
            warm_err, fresh_err = _inertia(x, centers), _inertia(x, fresh)
            if warm_err > RESEED_RATIO * fresh_err and warm_err - fresh_err > RESEED_MIN_GAIN:
                centers, labels = fresh, fresh_labels
        self._pal_centers = centers
        counts = np.bincount(labels, minlength=DOMINANT_K)
        total = float(counts.sum())
        items = []
        for j in np.argsort(-counts, kind="stable"):
            if counts[j] == 0:
                continue
            sat = colorsys.rgb_to_hsv(*(float(ch) / 255.0 for ch in np.clip(centers[j], 0, 255)))[1]
            items.append((centers[j], counts[j] / total, sat >= GREY_SATURATION))
        if any(chrom and share >= GREY_DROP_SHARE for _, share, chrom in items):
            items = [it for it in items if it[2]]
        chosen: list[np.ndarray] = []
        for c, share, _ in items:
            if len(chosen) >= k or (chosen and share < PALETTE_MIN_SHARE):
                break
            if all(float(np.sqrt(((c - o) ** 2).sum())) >= DISTINCT for o in chosen):
                chosen.append(c)
        scale = min(1.0, float(v.mean()) / float(_brightness(x).mean()))
        return [adjust(_to_rgb(c * scale), self.saturation, self.gain) for c in chosen]

    def _fill_slots(self, cols: list[RGB], k: int) -> set[int]:
        """Put the screen colours into k stable slots (each goes to the slot whose
        colour is nearest), so a palette change just crossfades a slot to its new
        colour instead of reshuffling. Returns the slots in use."""
        if not cols:
            self._slots = [_BLACK] * k
            return set(range(k))
        if self._slots is None:
            self._slots = cols + [cols[0]] * (k - len(cols))
            return set(range(len(cols)))
        prev = self._slots

        def cost(perm):
            return sum(math.dist(cols[i], prev[p]) for i, p in enumerate(perm))

        perm = min(itertools.permutations(range(k), len(cols)), key=cost)
        slots = list(prev)
        for i, p in enumerate(perm):
            slots[p] = cols[i]
        self._slots = slots
        return set(perm)

    def _clock(self, t: float) -> float:
        """Effect clock: seconds since the mode started, scaled by the 节奏 speed."""
        if self._t0 is None:
            self._t0 = t
        return max(0.0, t - self._t0) * self.speed

    def _fade_frame(self, img: np.ndarray | None, t: float, dt: float) -> Frame:
        """Whole strip one screen colour; every FADE_HOLD the next screen colour
        crossfades in over FADE_XFADE (two lights exchanging intensity). The order
        (slots) and timing stay put while the screen's colours change."""
        active = self._fill_slots(self._screen_palette(img, FADE_COLOURS), FADE_COLOURS)
        shown = self.smoother.step(self._slots, dt)
        u = self._clock(t)
        local = u - self._step_u
        if self._nxt is None and self._cur not in active and local < FADE_HOLD:
            self._step_u, local = u - FADE_HOLD, FADE_HOLD  # its colour left: move on now
        if self._nxt is None and local >= FADE_HOLD:
            others = [(self._cur + d) % FADE_COLOURS for d in range(1, FADE_COLOURS)]
            others = [s for s in others if s in active]
            if others:
                self._nxt = others[0]
            else:  # one colour on screen: keep showing it
                if self._cur not in active:
                    self._cur = min(active)
                self._step_u, local = u, 0.0
        colour = shown[self._cur]
        if self._nxt is not None:
            w = (local - FADE_HOLD) / FADE_XFADE
            if w >= 1.0:
                self._cur, self._nxt = self._nxt, None
                self._step_u += FADE_HOLD + FADE_XFADE
                colour = shown[self._cur]
            else:
                colour = _overlap(shown[self._cur], shown[self._nxt], _ease(w))
        return [colour] * self.n

    def _projection_frame(self, img: np.ndarray | None, t: float, dt: float) -> Frame:
        """The 投影 DIY effect (effects.render "projection") with the two top screen
        colours as its palette: fixed groups of 2 segments, A A B B ..., swapping in
        place. One colour on screen: the other groups show it at ~45 %."""
        cols = self._screen_palette(img, 2)
        if len(cols) == 1:
            a = cols[0]
            dim = _scale(a, PROJECTION_DIM)
            prev = self._slots
            if prev is not None and math.dist(a, prev[1]) < math.dist(a, prev[0]):
                self._slots = [dim, a]
            else:
                self._slots = [a, dim]
        else:
            self._fill_slots(cols, 2)
        shown = self.smoother.step(self._slots, dt)
        return effects.render("projection", self._clock(t), self.n, shown, width=self.group)

    # -- frames -----------------------------------------------------------
    def _dt(self, t: float) -> float:
        """Seconds since the previous frame (first frame 0; nominal period if t did
        not advance)."""
        last, self._last_t = self._last_t, t
        if last is None:
            return 0.0
        if t <= last:
            return 1.0 / self.fps
        return t - last

    def next_frame(self, t: float) -> Frame:
        dt = self._dt(t)
        try:
            img: np.ndarray | None = self._capture()
            self._grab_failing = False
        except Exception:
            if not self._grab_failing:
                log.warning("screen capture failed; dimming strip", exc_info=True)
            self._grab_failing = True
            if self._grab is None:
                self._drop_sct()  # re-enumerate monitors next frame
            img = None  # every mode fades to dark
        if self.mode == "fade":
            return self._fade_frame(img, t, dt)
        if self.mode == "projection":
            return self._projection_frame(img, t, dt)
        if self.mode == "region":
            return self.smoother.step(self._region_target(img), dt)
        if img is None:
            return self.smoother.step([_BLACK] * self.n, dt)
        target = [adjust(c, self.saturation, self.gain) for c in self._dominant_target(img)]
        return self.smoother.step(target, dt)
