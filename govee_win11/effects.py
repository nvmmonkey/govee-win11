"""DIY dynamic effects, computed on the PC and streamed frame by frame.

All effects are pure functions of (t, n, palette, speed). Internally
``u = t * speed`` is the effect clock; at speed 1 a full cycle is about 4 s.

Colour model -- light overlap, never invented hues. Every segment shows palette
colours only, as lights at some intensity: a transition (in time or along the strip)
is two palette lights overlapping additively in *linear* light (sRGB -> linear, sum,
-> sRGB), exactly as two LEDs shining on the same patch of wall. There is no hue
interpolation anywhere: yellow + sky blue meet as a whitish (#d5f0b7) "transition
bead", like the real Govee strip, not as a computed green.

The overlap is *peak-preserving* (see :func:`_light`): the weighted sum is rescaled so
its brightest channel equals the weighted mean of the lights' brightest channels. A
plain average of two lights whose peaks sit in different channels (red + blue) comes
out dim; two overlapping LEDs do not, so the bead stays as bright as its neighbours.
"""
from __future__ import annotations

import contextvars
import math
import random

from .protocol import RGB

Frame = list[RGB]

EFFECTS: dict[str, str] = {
    "fade1": "淡入淡出 1",
    "fade2": "淡入淡出 2",
    "fade3": "淡入淡出 3",
    "jump1": "跳跃 1",
    "jump2": "跳跃 2",
    "flash1": "闪烁 1",
    "flash2": "闪烁 2",
    "flash3": "闪烁 3",
    "marquee1": "跑马灯 1",
    "marquee2": "跑马灯 2",
    "marquee3": "跑马灯 3",
    "flow1": "流水 1",
    "flow2": "流水 2",
    "rainbow1": "彩虹 1",
    "rainbow2": "彩虹 2",
    "aurora": "极光",
    "projection": "投影",
}

_BLACK: RGB = (0, 0, 0)
_WHITE: RGB = (255, 255, 255)
_CYCLE = 4.0  # seconds per full cycle at speed 1
# Palette used by the rainbows when the user palette has fewer than two colours: the six
# primary/secondary LED colours as bands (their overlaps are the in-between hues).
_SPECTRUM: list[RGB] = [(255, 0, 0), (255, 255, 0), (0, 255, 0), (0, 255, 255), (0, 0, 255),
                        (255, 0, 255)]


# ---- light model: sRGB <-> linear light, additive overlap, intensity ----

def _srgb_to_linear(v: float) -> float:
    v /= 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


_LIN = [_srgb_to_linear(v) for v in range(256)]


def _encode(x: float) -> int:
    """Linear light 0..1 -> sRGB 0..255."""
    x = min(1.0, max(0.0, x))
    v = 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
    return int(round(v * 255))


def _lin(c: RGB) -> tuple[float, float, float]:
    return (_LIN[c[0]], _LIN[c[1]], _LIN[c[2]])


# ---- 混合方式 (设置 → 过渡方式): which space overlaps and crossfades are summed in ----
# "linear": linear light (physically additive, the default); "perceptual" (人眼):
# gamma-encoded sRGB, i.e. a plain sRGB-space weighted sum. One global switch read by
# ``_light`` (every effect) and ``screen.Smoother`` (screen sync), so it applies live.
MIX_MODES = ("linear", "perceptual")
_mix = "linear"
_SRGB_UNIT = [v / 255.0 for v in range(256)]


def set_mix(mode: str) -> None:
    global _mix
    if mode not in MIX_MODES:
        raise ValueError(f"unknown mix mode {mode!r}")
    _mix = mode


def get_mix() -> str:
    return _mix


def _encode_unit(x: float) -> int:
    """sRGB 0..1 -> 0..255."""
    return int(round(min(1.0, max(0.0, x)) * 255))


def _intensity_srgb(k: float) -> float:
    """Intensity ``k`` (linear light) as an sRGB factor: a full channel scaled by k in
    linear light lands on exactly this sRGB value, so dimming looks the same in both
    mixes while staying a pure scalar (same hue) in sRGB space."""
    if k >= 1.0:
        return k
    x = max(0.0, k)
    return 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055


def _light(lights: list[tuple[RGB, float]], k: float = 1.0) -> RGB:
    """Overlap palette lights: ``lights`` is [(colour, weight)] with weights summing to 1.

    The colours are summed in linear light, then the sum is rescaled so its brightest
    channel equals the weighted mean of each light's brightest channel (peak-preserving
    additive overlap: yellow + sky blue reads as a bright cyan-white, red + blue as a
    full-brightness magenta, never a dim average). A single light, or weights (1, 0),
    give the palette colour exactly. ``k`` then scales the intensity (linear light).

    With the perceptual mix (``set_mix("perceptual")``) the same weighted sum and
    peak-preserving rescale happen on the gamma-encoded sRGB values instead, and ``k``
    is applied as the equivalent sRGB factor (``_intensity_srgb``)."""
    linear = _mix == "linear"
    table = _LIN if linear else _SRGB_UNIT
    r = g = b = peak = 0.0
    for c, w in lights:
        if w <= 0.0:
            continue
        lr, lg, lb = table[c[0]], table[c[1]], table[c[2]]
        r += lr * w
        g += lg * w
        b += lb * w
        peak += max(lr, lg, lb) * w
    m = max(r, g, b)
    if m <= 0.0:
        return _BLACK
    if linear:
        s = peak / m * k
        return (_encode(r * s), _encode(g * s), _encode(b * s))
    s = peak / m * _intensity_srgb(k)
    return (_encode_unit(r * s), _encode_unit(g * s), _encode_unit(b * s))


def _overlap(a: RGB, b: RGB, w: float) -> RGB:
    """Light a at intensity 1-w overlapping light b at intensity w (see _light)."""
    if w <= 0.0:
        return a
    if w >= 1.0:
        return b
    return _light([(a, 1.0 - w), (b, w)])


def _scale(c: RGB, k: float) -> RGB:
    """Palette colour at intensity k (0..1), scaled in linear light: same hue."""
    return _light([(c, 1.0)], k)


def _ease(f: float) -> float:
    f = min(1.0, max(0.0, f))
    return (1 - math.cos(math.pi * f)) / 2


# Effect-seconds advanced per rendered frame (speed / fps), set by render(). Moving band
# edges use it to widen their overlap so an edge never hops over a segment between frames.
_FRAME_DU: contextvars.ContextVar[float] = contextvars.ContextVar("frame_du", default=0.0)
_REACH_FACTOR = 3.0  # overlap reach as a multiple of the per-frame edge travel


def _travel(cycle: float, L: int) -> float:
    """Palette units an edge moves per frame in an effect that passes the palette once per
    ``cycle`` effect-seconds."""
    return _FRAME_DU.get() / cycle * L


def _band_lights(pal: list[RGB], p: float, seg: float,
                 span: tuple[int, int] | None = None, travel: float = 0.0) -> list[tuple[RGB, float]]:
    """Palette colours as bands along the strip: band j covers palette positions
    [j, j + 1) and is pure ``pal[j % L]``. A segment centred at position ``p`` and
    ``seg`` palette units long is a pure band colour, except near a band edge, where the
    light of the neighbouring band spills over it: within one segment of the edge the
    segment is the overlap of the two colours, weighted by where the edge sits (an edge
    on a segment's centre makes that segment a 50/50 "transition bead"; as the edge moves
    the weights move with it, so motion stays smooth). Only the two colours meeting at
    the nearest edge are ever involved -- no third colour, no in-between hue. The spill
    weight follows a cosine ease, so the first glimmer of the neighbour's light (where
    gamma makes a few percent of light jump out of the dark) creeps in gently.

    ``span=(a, b)`` marks a static layout whose strip ends sit at positions a and b: the
    edges there are the ends of the strip, not band edges, so no light spills across them.
    """
    L = len(pal)
    e = math.floor(p + 0.5)  # nearest band edge
    if span is not None:
        if span[1] - span[0] < 2:
            return [(pal[math.floor(p) % L], 1.0)]
        e = min(span[1] - 1, max(span[0] + 1, e))
    # spill reach: one segment (at rest), widened to _REACH_FACTOR x the edge's per-frame
    # travel so fast flows still glide; at most half a band so edges never overlap
    hw = min(max(seg, _REACH_FACTOR * travel), 0.5)
    w = _ease(0.5 + (p - e) / (2 * hw))
    lo, hi = pal[(e - 1) % L], pal[e % L]
    if w <= 0.0:
        return [(lo, 1.0)]
    if w >= 1.0:
        return [(hi, 1.0)]
    return [(lo, 1.0 - w), (hi, w)]


def _bands(pal: list[RGB], p: float, seg: float, k: float = 1.0,
           span: tuple[int, int] | None = None, travel: float = 0.0) -> RGB:
    return _light(_band_lights(pal, p, seg, span, travel), k)


# ---- smooth effects ----

def _fade1(u, n, pal):
    """Whole strip one colour; each palette colour hands over to the next by the two
    lights exchanging intensity (eased), L steps per 4 s cycle."""
    L = len(pal)
    pos = (u / _CYCLE * L) % L
    i = int(pos)
    return [_overlap(pal[i % L], pal[(i + 1) % L], _ease(pos - i))] * n


def _fade2(u, n, pal):
    """Palette bands spread over the strip (overlap beads at the edges), breathing as a
    whole (linear-light intensity 39-100 %, i.e. about 65-100 % in sRGB). Cycle 4 s."""
    k = 0.695 + 0.305 * math.cos(2 * math.pi * u / _CYCLE)
    L = len(pal)
    return [_bands(pal, (i + 0.5) / n * L, L / n, k, (0, L)) for i in range(n)]


# fade3: per step k the strip goes zone arrangement k -> whole strip = pal[k % L] ->
# arrangement k+1, each half a sinusoidal ease. Full cycle (L steps) = L * _F3_STEP seconds
# at speed 1 (10 s for two colours, matching the real Govee app at 50 %).
_F3_STEP = 5.0


def _fade3(u, n, pal):
    """Zone rotation with unified states: len(pal) contiguous zones (a transition bead
    where two zones meet); each step eases (sinusoidally) from the zone arrangement to the
    whole strip in one palette colour, then on to the arrangement rotated by one. Every
    segment is the zone light(s) and the unified colour's light overlapping with
    exchanging intensity. Single colour: gentle breathing."""
    L = len(pal)
    if L == 1:
        return [_scale(pal[0], 0.75 + 0.25 * math.cos(2 * math.pi * u / _CYCLE))] * n
    k = int(u // _F3_STEP)
    x = (u - k * _F3_STEP) / _F3_STEP * 2  # 0..2
    uni = pal[k % L]
    step, f = (k, _ease(x)) if x < 1 else (k + 1, 1.0 - _ease(x - 1))
    if f >= 1.0:
        return [uni] * n
    out = []
    for i in range(n):
        zone = _band_lights(pal, (i + 0.5) / n * L + step, L / n, (step, step + L))
        out.append(_light([(c, w * (1.0 - f)) for c, w in zone] + [(uni, f)]))
    return out


def _jump1(u, n, pal):
    """Beat = 1 s. Whole strip jumps to the next palette color each beat."""
    return [pal[int(u // 1.0) % len(pal)]] * n


def _jump2(u, n, pal):
    """Beat = 0.5 s. Each segment jumps to a random palette color (seeded by beat)."""
    rng = random.Random(int(u // 0.5))
    return [pal[rng.randrange(len(pal))] for _ in range(n)]


def _flash1(u, n, pal):
    """Beat = 0.5 s. Whole strip alternates on (next palette color) / off."""
    beat = int(u // 0.5)
    return [pal[(beat // 2) % len(pal)] if beat % 2 == 0 else _BLACK] * n


def _flash2(u, n, pal):
    """Beat = 0.25 s. A random ~third of segments flash each beat (seeded by beat)."""
    rng = random.Random(int(u // 0.25) + 100003)
    out = []
    for _ in range(n):
        c = pal[rng.randrange(len(pal))]
        out.append(c if rng.random() < 0.35 else _BLACK)
    return out


def _flash3(u, n, pal):
    """Cycle = 2 s (beat). Quick double flash (0-0.15 s, 0.3-0.45 s), then pause."""
    cyc = int(u // 2.0)
    ph = u - cyc * 2.0
    on = ph < 0.15 or 0.3 <= ph < 0.45
    return [pal[cyc % len(pal)] if on else _BLACK] * n


def _marquee(u, n, pal, direction):
    block = 2
    shift = int(u // 0.5)
    if direction == 0:  # bounce
        if n > 1:
            k = shift % (2 * (n - 1))
            shift = k if k <= n - 1 else 2 * (n - 1) - k
        else:
            shift = 0
    else:
        shift *= direction
    return [pal[((i - shift) // block) % len(pal)] for i in range(n)]


def _marquee1(u, n, pal):
    """Beat = 0.5 s. Palette blocks (2 segments each) move forward 1 segment per beat."""
    return _marquee(u, n, pal, 1)


def _marquee2(u, n, pal):
    """Beat = 0.5 s. Like marquee1 but moving backward."""
    return _marquee(u, n, pal, -1)


def _marquee3(u, n, pal):
    """Beat = 0.5 s. Blocks move 1 segment per beat, bouncing back and forth."""
    return _marquee(u, n, pal, 0)


def _flow1(u, n, pal):
    """Palette bands flowing forward (edges glide through segments), one strip length per 4 s."""
    L = len(pal)
    return [_bands(pal, ((i + 0.5) / n - u / _CYCLE) * L, L / n, travel=_travel(_CYCLE, L)) for i in range(n)]


def _flow2(u, n, pal):
    """Palette bands flowing from the middle toward both ends, 4 s cycle."""
    L = len(pal)
    mid = (n - 1) / 2
    half = max(n / 2, 1)
    return [_bands(pal, (abs(i - mid) / half - u / _CYCLE) * L, L / half,
                   travel=_travel(_CYCLE, L)) for i in range(n)]


def _rainbow_at(phase, n, pal):
    """Palette colours as bands across the strip (overlap beads where they meet), shifted
    by ``phase`` strip lengths (increasing phase = moving toward higher indices). With
    fewer than 2 colours the six-colour LED spectrum is used instead."""
    if len(pal) < 2:
        pal = _SPECTRUM
    L = len(pal)
    tr = _travel(_RAINBOW_CYCLE, L)
    return [_bands(pal, ((i + 0.5) / n - phase) * L, L / n, travel=tr) for i in range(n)]


def _rainbow1(u, n, pal):
    """Palette rainbow flowing toward higher indices, one strip length per 4 s."""
    return _rainbow_at(u / _RAINBOW_CYCLE, n, pal)


def _rainbow2(u, n, pal):
    """Like rainbow1 but flowing toward lower indices."""
    return _rainbow_at(-u / _RAINBOW_CYCLE, n, pal)


_RAINBOW_CYCLE = 4.0  # seconds per full palette pass along the strip at speed 1
_AURORA_CYCLE = 20.0  # seconds for one full palette cycle to pass a point at speed 1


def _aurora(u, n, pal, width=None):
    """Wide palette bands (~width segments each, overlap beads at the edges) drifting along
    the strip, one palette cycle per 20 s, with a gentle per-segment brightness shimmer
    (linear-light intensity 53-100 %, about 75-100 % in sRGB)."""
    L = len(pal)
    w = width if width and width > 0 else max(2.0, n / L)
    ph = u / _AURORA_CYCLE
    out = []
    for i in range(n):
        # integer frequencies of the 20 s cycle keep the loop seamless
        sh = 0.5 + 0.5 * math.sin(2 * math.pi * (3 * ph + i * 0.37)) * math.cos(2 * math.pi * (2 * ph - i * 0.21))
        out.append(_bands(pal, (i + 0.5) / w - ph * L, 1.0 / w, 1.0 - 0.47 * sh,
                          travel=_travel(_AURORA_CYCLE, L)))
    return out


# 投影 (projection): fixed zones whose colours cycle in place -- nothing moves along the
# strip. Also the renderer behind screen sync's projection mode (screen.py), with the two
# dominant screen colours as the palette.
_PROJ_HOLD = 1.5   # effect-seconds a step holds its colours
_PROJ_XFADE = 2.0  # effect-seconds each zone crossfades to its next colour
PROJECTION_GROUP = 2  # default segments per zone
PROJECTION_GROUP_MAX = 5


def projection_group(width: float | None) -> int:
    """Segments per projection zone from the effect's ``width`` (default 2, 1-5)."""
    if not width or width <= 0:
        return PROJECTION_GROUP
    return int(min(PROJECTION_GROUP_MAX, max(1, round(width))))


def _projection(u, n, pal, width=None):
    """Fixed zones of ``width`` segments (default 2). At step k zone g shows
    pal[(g + k) % L]; each step every zone crossfades in place to its next colour, the two
    lights exchanging intensity (eased) -- with two colours A A B B <-> B B A A, mid-fade
    every zone shows the A+B overlap bead. Step = 1.5 s hold + 2 s crossfade at speed 1.
    Single colour: steady."""
    L = len(pal)
    size = projection_group(width)
    step = _PROJ_HOLD + _PROJ_XFADE
    k = math.floor(u / step)
    local = u - k * step
    w = _ease((local - _PROJ_HOLD) / _PROJ_XFADE) if local > _PROJ_HOLD else 0.0
    return [_overlap(pal[(i // size + k) % L], pal[(i // size + k + 1) % L], w)
            for i in range(n)]


_FUNCS = {k: globals()["_" + k] for k in EFFECTS}
_WIDTH_EFFECTS = ("aurora", "projection")  # effects taking the ``width`` argument

# Shortest on/off phase of each beat effect, in effect-seconds (speed 1). At ``fps``
# frames/s a phase shorter than the gap between two samples can vanish (flash3's 0.15 s
# double flash at speed 4 lasts 37 ms; at 8 fps most were missed), so render() caps the
# speed of these effects so every phase lasts at least one frame period plus the
# Windows timer jitter (~16 ms): any sample sequence that far apart lands inside it.
_MIN_PHASE = {"jump1": 1.0, "jump2": 0.5, "flash1": 0.5, "flash2": 0.25, "flash3": 0.15,
              "marquee1": 0.5, "marquee2": 0.5, "marquee3": 0.5}
SAMPLE_JITTER = 0.016


def max_speed(effect: str, fps: float | None) -> float:
    """Highest speed at which ``effect`` keeps every phase visible at ``fps``."""
    phase = _MIN_PHASE.get(effect)
    if phase is None or not fps:
        return math.inf
    return phase / (1.0 / fps + SAMPLE_JITTER)


def render(effect: str, t: float, n: int, palette: list[RGB], speed: float = 1.0,
           width: float | None = None, fps: float | None = None) -> Frame:
    """Pure function: frame for ``effect`` at ``t`` seconds. Raises ValueError if unknown.
    With ``fps`` (the rate frames are sampled at) beat effects are capped at
    :func:`max_speed` so no flash or jump falls between two frames."""
    fn = _FUNCS.get(effect)
    if fn is None:
        raise ValueError(f"unknown effect: {effect!r}")
    pal = [tuple(c) for c in palette] or [_WHITE]
    if n <= 0:
        return []
    speed = min(speed, max_speed(effect, fps))
    _FRAME_DU.set(speed / fps if fps else 0.0)
    if effect in _WIDTH_EFFECTS:
        return list(fn(t * speed, n, pal, width))
    return list(fn(t * speed, n, pal))


def speed_from_pct(pct: int) -> float:
    """Map the app's 0-100 % speed slider to a multiplier: 0 -> 0.25, 50 -> 1, 100 -> 4."""
    pct = max(0.0, min(100.0, float(pct)))
    return 0.25 * 16 ** (pct / 100)


# 混合 (mix): each effect plays for one slot, the last _MIX_XFADE of a slot crossfades
# into the next effect (seconds at speed 1).
_MIX_SLOT = 8.0
_MIX_XFADE = 1.0
_RAINBOW_DIR = {"rainbow1": 1.0, "rainbow2": -1.0}


def _mix_window(effects: list[str], j: int) -> tuple[float, float]:
    """Rainbow velocity at the start/end of slot j's crossfade window (ramps linearly).
    rainbow1<->rainbow2 reverses smoothly; a rainbow fading in/out keeps its own speed."""
    m = len(effects)
    a = _RAINBOW_DIR.get(effects[j % m])
    b = _RAINBOW_DIR.get(effects[(j + 1) % m])
    if a is None and b is None:
        return 0.0, 0.0
    return (a if a is not None else b), (b if b is not None else a)


def _mix_slot_travel(effects: list[str], j: int, x: float) -> float:
    """Rainbow travel (in effect-seconds) accumulated x seconds into slot j."""
    body = _MIX_SLOT - _MIX_XFADE
    v = _RAINBOW_DIR.get(effects[j % len(effects)], 0.0)
    if x <= body:
        return v * x
    a, b = _mix_window(effects, j)
    y = (x - body) / _MIX_XFADE
    return v * body + _MIX_XFADE * (a * y + (b - a) * y * y / 2)


def _mix_rainbow_phase(effects: list[str], u: float) -> float:
    """Integrated rainbow position at effect clock u, so direction changes never jump."""
    m = len(effects)
    loops, rest = divmod(u, _MIX_SLOT * m)
    per_loop = sum(_mix_slot_travel(effects, j, _MIX_SLOT) for j in range(m))
    j = int(rest // _MIX_SLOT)
    pos = loops * per_loop + sum(_mix_slot_travel(effects, k, _MIX_SLOT) for k in range(j))
    return pos + _mix_slot_travel(effects, j, rest - j * _MIX_SLOT)


def render_mix(effects: list[str], t: float, n: int, palette: list[RGB], speed: float = 1.0,
               width: float | None = None, fps: float | None = None) -> Frame:
    """Pure function behind 混合 mode: plays ``effects`` in turn, 8 s / speed each with a
    1 s / speed crossfade into the next, looping. Consecutive rainbow1/rainbow2 slots share
    one integrated gradient position, so the flow reverses smoothly instead of crossfading."""
    if not effects:
        raise ValueError("mix needs at least one effect")
    for e in effects:
        if e not in _FUNCS:
            raise ValueError(f"unknown effect: {e!r}")
    if len(effects) == 1:
        return render(effects[0], t, n, palette, speed, width, fps)
    pal = [tuple(c) for c in palette] or [_WHITE]
    if n <= 0:
        return []
    u = t * speed
    m = len(effects)
    j = int((u % (_MIX_SLOT * m)) // _MIX_SLOT)
    local = u % _MIX_SLOT
    cur, nxt = effects[j], effects[(j + 1) % m]

    def frame(e: str) -> Frame:
        if e in _RAINBOW_DIR:
            return _rainbow_at(_mix_rainbow_phase(effects, u) / _RAINBOW_CYCLE, n, pal)
        return render(e, t, n, pal, speed, width, fps)

    f = (local - (_MIX_SLOT - _MIX_XFADE)) / _MIX_XFADE
    if f <= 0 or (cur in _RAINBOW_DIR and nxt in _RAINBOW_DIR):
        return frame(cur)
    f = _ease(f)  # the two effects' lights exchange intensity
    return [_overlap(a, b, f) for a, b in zip(frame(cur), frame(nxt))]


_SMOOTH = ("fade", "flow", "rainbow", "aurora", "projection")
# Smooth effects: 12 fps. The packet budget (25/s) is shared by time, not per frame, so
# a higher rate does not refresh the strip faster (measured: 10-15 fps give the same
# shown-vs-target error) -- 12 is smooth enough. Beat effects (and mixes containing
# one): 20 fps, so short flashes/jumps are sampled finely (see _MIN_PHASE). Their frames
# are mostly a single colour (1 packet) or unchanged (0 packets), so this costs nothing.
SMOOTH_FPS = 12.0
BEAT_FPS = 20.0


class EffectMode:
    """ContinuousMode (structural): fps, open(), next_frame(t), close().

    ``effect`` may be a list of effect ids: 混合 (mix) mode, rendered by ``render_mix``."""

    def __init__(self, effect: str | list[str], palette: list[RGB], speed: float, n: int,
                 width: float | None = None):
        names = list(effect) if isinstance(effect, (list, tuple)) else [effect]
        if not names:
            raise ValueError("no effect given")
        for e in names:
            if e not in _FUNCS:
                raise ValueError(f"unknown effect: {e!r}")
        self.effect = effect if isinstance(effect, str) else names
        self.palette = list(palette)
        self.speed = speed
        self.n = n
        self.width = width
        self.fps: float = SMOOTH_FPS if all(e.startswith(_SMOOTH) for e in names) else BEAT_FPS

    def open(self) -> None:
        pass

    def next_frame(self, t: float) -> Frame:
        if isinstance(self.effect, list):
            return render_mix(self.effect, t, self.n, self.palette, self.speed, self.width,
                              self.fps)
        return render(self.effect, t, self.n, self.palette, self.speed, self.width, self.fps)

    def close(self) -> None:
        pass
