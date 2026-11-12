import colorsys
import itertools

import numpy as np
import pytest

from govee_win11.effects import (EFFECTS, SAMPLE_JITTER, EffectMode, max_speed, render,
                                 render_mix, speed_from_pct)

PAL = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
N = 15
TS = [0.0, 0.3, 0.5, 1.0, 1.7, 3.3, 10.1, 123.456]


def test_effects_catalog():
    assert len(EFFECTS) == 17  # + projection (Task 16 C)
    assert "fade1" in EFFECTS and "rainbow2" in EFFECTS


def test_all_effects_shape_and_range():
    for e in EFFECTS:
        for t in TS:
            f = render(e, t, N, PAL)
            assert len(f) == N
            for px in f:
                assert len(px) == 3
                assert all(isinstance(c, int) and 0 <= c <= 255 for c in px)


def test_fade1_uniform_and_starts_on_first_color():
    assert render("fade1", 0, N, PAL) == [PAL[0]] * N
    f = render("fade1", 1.1, N, PAL)
    assert len(set(f)) == 1


def test_flash1_has_dark_phase():
    frames = [render("flash1", t / 10, N, PAL) for t in range(40)]
    assert any(all(px == (0, 0, 0) for px in f) for f in frames)
    assert any(any(px != (0, 0, 0) for px in f) for f in frames)


def test_marquee1_shifts_one_segment_per_beat():
    a = render("marquee1", 0.0, N, PAL)
    b = render("marquee1", 0.5, N, PAL)
    assert b[1:] == a[:-1]
    assert b != a


def test_rainbow1_single_colour_palette_is_full_hue_wheel():
    # with < 2 colours the palette is ignored and the full hue wheel is used
    assert render("rainbow1", 1.3, N, [(10, 200, 30)]) == render("rainbow1", 1.3, N, [(1, 2, 3)])
    assert len(set(render("rainbow1", 0, N, [(1, 2, 3)]))) > 5


def test_single_color_palette_ok():
    for e in EFFECTS:
        assert len(render(e, 0.7, N, [(10, 200, 30)])) == N


def test_empty_palette_is_white():
    assert render("fade1", 0, 4, []) == [(255, 255, 255)] * 4


@pytest.mark.parametrize("e", ["fade1", "fade2", "fade3", "flow1", "flow2", "rainbow1",
                               "rainbow2", "aurora", "marquee1"])
def test_speed_scales_period(e):
    for t in TS:
        assert render(e, t, N, PAL, speed=2) == render(e, 2 * t, N, PAL, speed=1)


def test_deterministic():
    for e in EFFECTS:
        for t in TS:
            assert render(e, t, N, PAL) == render(e, t, N, PAL)


def test_unknown_effect():
    with pytest.raises(ValueError):
        render("nope", 0, N, PAL)
    with pytest.raises(ValueError):
        EffectMode("nope", PAL, 1.0, N)


def test_effect_mode():
    m = EffectMode("fade1", PAL, 1.0, N)
    assert m.fps == 12
    assert EffectMode("jump1", PAL, 1.0, N).fps == 20
    m.open()
    assert m.next_frame(0.0) == render("fade1", 0.0, N, PAL, 1.0)
    m.close()


# ---- aurora ----
AUR_PAL = [(230, 30, 30), (30, 200, 60), (40, 90, 255), (200, 40, 220)]


def _hsv(px):
    return colorsys.rgb_to_hsv(*(c / 255 for c in px))


def test_aurora_in_effects():
    assert EFFECTS["aurora"] == "极光"
    assert EffectMode("aurora", AUR_PAL, 1.0, N).fps == 12


def test_aurora_is_palette_light_only():
    # every segment is one palette colour or two adjacent ones overlapping (no third hue),
    # and the shimmer only dims (never below ~half the dimmest palette colour)
    minv = min(_hsv(c)[2] for c in AUR_PAL)
    for width in (None, 1.0, 3.0, 9.0):
        for k in range(60):
            for px in render("aurora", k * 0.77, N, AUR_PAL, width=width):
                assert _explained(px, AUR_PAL), (width, k, px)
                assert _hsv(px)[2] >= 0.5 * minv


def test_aurora_bands_span_width():
    f = render("aurora", 0, N, [(255, 0, 0), (0, 0, 255)], width=6)

    def longest(pred):
        best = cur = 0
        for px in f:
            cur = cur + 1 if pred(px) else 0
            best = max(best, cur)
        return best
    assert longest(lambda p: p[0] > p[2] and p[0] > p[1]) >= 3
    assert longest(lambda p: p[2] > p[0] and p[2] > p[1]) >= 3


def _close(a, b, tol=6):
    return all(abs(x - y) <= tol for pa, pb in zip(a, b) for x, y in zip(pa, pb))


def test_aurora_wraps_and_moves():
    a = render("aurora", 3.0, N, AUR_PAL)
    assert a != render("aurora", 4.0, N, AUR_PAL)
    assert _close(a, render("aurora", 23.0, N, AUR_PAL))


def test_aurora_speed_scales_period():
    assert render("aurora", 1.3, N, AUR_PAL, speed=2) == render("aurora", 2.6, N, AUR_PAL)
    a = render("aurora", 3.0, N, AUR_PAL, speed=2)
    assert _close(a, render("aurora", 13.0, N, AUR_PAL, speed=2))


# ---- fade3 zone rotation, palette rainbows, mix mode ----
YELLOW = (255, 224, 0)
CYAN = (62, 207, 224)
BLUE = (0, 0, 255)
PINK = (240, 64, 176)
PBLUE = (10, 120, 200)
PINK_BLUE = [(240, 64, 176), (10, 120, 200), (10, 16, 176), (192, 8, 79)]


def _maxdiff(a, b):
    return max(abs(x - y) for pa, pb in zip(a, b) for x, y in zip(pa, pb))


def test_fade3_unified_peaks_alternate_palette_colours():
    pal = [YELLOW, CYAN]
    for k, want in enumerate([YELLOW, CYAN, YELLOW, CYAN]):
        assert render("fade3", 2.5 + 5.0 * k, N, pal) == [want] * N


def test_fade3_zones_swap_between_peaks():
    pal = [YELLOW, CYAN]
    a = render("fade3", 0.0, N, pal)
    assert a[:7] == [YELLOW] * 7 and a[8:] == [CYAN] * 7
    b = render("fade3", 5.0, N, pal)
    assert b[:7] == [CYAN] * 7 and b[8:] == [YELLOW] * 7
    assert render("fade3", 10.0, N, pal) == a


def test_fade3_cycle_is_ten_seconds():
    pal = [YELLOW, CYAN]
    for t in (0.7, 3.1, 6.6):
        assert render("fade3", t, N, pal) == render("fade3", t + 10.0, N, pal)
    assert render("fade3", 0.7, N, pal) != render("fade3", 5.7, N, pal)
    assert render("fade3", 1.25, N, pal, 2.0) == [YELLOW] * N


def test_fade3_three_colours_unified_cycle():
    for k in range(3):
        assert render("fade3", 2.5 + 5.0 * k, N, PAL) == [PAL[k]] * N
    # 5|5|5 zones: each edge falls between two segments, so both carry a little of the
    # neighbour's light; the strip ends never spill
    a = render("fade3", 0.0, N, PAL)
    assert a[:4] == [PAL[0]] * 4 and a[6:9] == [PAL[1]] * 3 and a[11:] == [PAL[2]] * 4
    for i, (c0, c1) in ((4, (0, 1)), (5, (0, 1)), (9, (1, 2)), (10, (1, 2))):
        assert a[i] not in PAL and _explained(a[i], [PAL[c0], PAL[c1]])
    assert a[4][0] > a[4][1] and a[5][1] > a[5][0]  # each leans to its own zone
    b = render("fade3", 5.0, N, PAL)
    assert b[:4] == [PAL[1]] * 4 and b[6:9] == [PAL[2]] * 3 and b[11:] == [PAL[0]] * 4


def test_fade3_static_zones_have_one_whitish_transition_bead():
    # yellow | sky blue at rest: ...Y Y [Y+C] C C... -- the bead is the bright additive
    # overlap (whitish light, like the real strip), the only non-palette segment
    for t in (0.0, 5.0, 10.0):
        f = render("fade3", t, N, [YELLOW, CYAN])
        odd = [i for i, px in enumerate(f) if px not in (YELLOW, CYAN)]
        assert odd == [7]
        bead = f[7]
        assert min(bead) >= 150 and bead[1] == max(bead)  # whitish (lime-white), green channel highest
        assert bead == _ref_peak_mix(YELLOW, CYAN, 0.5)
        assert len(set(f[:7])) == 1 and len(set(f[8:])) == 1 and f[0] != f[14]


def test_fade3_mid_transition_is_linear_light_overlap():
    # 1.25 s = halfway (eased w = 0.5) from Y|C zones to all-yellow: the cyan zone is
    # yellow and cyan light overlapping 50/50 in linear light, peak-preserving
    f = render("fade3", 1.25, N, [YELLOW, CYAN])
    assert f[:7] == [YELLOW] * 7
    mid = _ref_peak_mix(YELLOW, CYAN, 0.5)
    assert f[8:] == [mid] * 7
    assert min(mid) >= 150  # pale / whitish: the natural overlap, intended
    assert f[7] == _ref_peak_mix(YELLOW, CYAN, 0.25)  # the bead fading to yellow
    # the peaks are exact palette colours
    assert render("fade3", 2.5, N, [YELLOW, CYAN]) == [YELLOW] * N
    assert render("fade3", 7.5, N, [YELLOW, CYAN]) == [CYAN] * N


def test_fade3_every_frame_is_palette_light():
    for pal in ([YELLOW, CYAN], [YELLOW, BLUE], [PINK, PBLUE]):
        for k in range(12 * 11):
            for px in render("fade3", k / 12, N, pal):
                assert _explained(px, pal), (pal, k, px)
    # three colours: a zone-edge bead (two colours) fading into the unified colour may
    # briefly hold all three lights -- still only palette light, never a new hue
    pal3 = [YELLOW, CYAN, PINK]
    for k in range(12 * 16):
        for px in render("fade3", k / 12, N, pal3):
            assert _explained(px, pal3, at_most=3), (k, px)


def test_fade3_fades_are_smooth():
    for pal in ([YELLOW, CYAN], [YELLOW, BLUE], PAL, PINK_BLUE):
        frames = [render("fade3", k / 12, N, pal) for k in range(12 * 21)]
        for f0, f1 in zip(frames, frames[1:]):
            assert _maxdiff(f0, f1) <= 25


def test_fade3_single_colour_breathes():
    c = (10, 200, 30)
    frames = [render("fade3", k / 4, N, [c]) for k in range(16)]
    for f in frames:
        assert len(set(f)) == 1
        assert _hsv(f[0])[0] == pytest.approx(_hsv(c)[0], abs=0.02)
    assert len({f[0] for f in frames}) > 2


# ---- light model reference (independent of the implementation) ----
def _s2l(v):
    v = v / 255
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _l2s(x):
    x = min(1.0, max(0.0, x))
    return round(255 * (12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055))


def _ref_peak_mix(a, b, w):
    """Two lights overlapping in linear light: a*(1-w) + b*w, rescaled so the brightest
    channel equals (1-w)*peak(a) + w*peak(b)."""
    la, lb = [_s2l(c) for c in a], [_s2l(c) for c in b]
    m = [x * (1 - w) + y * w for x, y in zip(la, lb)]
    s = ((1 - w) * max(la) + w * max(lb)) / max(m)
    return tuple(_l2s(x * s) for x in m)


def _explained(px, pal, at_most=2, tol=3, s2l=_s2l, l2s=_l2s):
    """px is a non-negative linear-light combination of at most ``at_most`` palette
    colours (within 8-bit rounding), i.e. no hue that the palette lights cannot make.
    ``s2l``/``l2s``: the mixing space (identity on 0..1 = sRGB-space mixing)."""
    x = np.array([s2l(c) for c in px])
    cols = [np.array([s2l(c) for c in col]) for col in dict.fromkeys(tuple(c) for c in pal)]
    if not x.any():
        return True
    # weight each channel by 1 / (linear light per sRGB step) so the fit minimises the
    # error in sRGB codes (near black a tiny amount of light is many codes)
    wt = np.array([1.0 / (s2l(min(255, c + 1)) - s2l(max(0, c - 1))) for c in px])
    k = len(cols)
    adjacent = {frozenset((i, (i + 1) % k)) for i in range(k)}  # neighbours in the cycle
    for r in range(1, min(at_most, k) + 1):
        for idx in itertools.combinations(range(k), r):
            if r == 2 and at_most == 2 and frozenset(idx) not in adjacent:
                continue  # a two-colour overlap is between neighbouring palette colours
                # (at_most=3 is fade3: zone lights plus any unified colour, so no restriction)
            sub = [cols[i] for i in idx]
            A = np.stack(sub, axis=1)
            coef, *_ = np.linalg.lstsq(A * wt[:, None], x * wt, rcond=None)
            if (coef < -1e-6).any():
                continue
            back = tuple(l2s(v) for v in A @ coef)
            if max(abs(p - q) for p, q in zip(back, px)) <= tol:
                return True
    return False


def _hsv_lerp_old(a, b, f):
    h0, s0, v0 = colorsys.rgb_to_hsv(*(c / 255 for c in a))
    h1, s1, v1 = colorsys.rgb_to_hsv(*(c / 255 for c in b))
    dh = (h1 - h0 + 0.5) % 1.0 - 0.5
    r, g, bb = colorsys.hsv_to_rgb((h0 + dh * f) % 1.0, s0 + (s1 - s0) * f, v0 + (v1 - v0) * f)
    return tuple(round(c * 255) for c in (r, g, bb))


def test_explained_rejects_invented_hues():
    # sanity check of the checker: the old short-arc HSV blend of yellow and sky blue
    # invents green, which no overlap of the two lights can make
    assert not _explained(_hsv_lerp_old(YELLOW, CYAN, 0.5), [YELLOW, CYAN])
    assert not _explained((0, 255, 0), [YELLOW, CYAN])
    assert _explained(_ref_peak_mix(YELLOW, CYAN, 0.3), [YELLOW, CYAN])
    assert _explained((128, 112, 0), [YELLOW, CYAN])  # dimmed yellow
    assert not _explained(_ref_peak_mix(PAL[0], PAL[2], 0.5), PAL[:2])


SMOOTH = ["fade1", "fade2", "fade3", "flow1", "flow2", "rainbow1", "rainbow2", "aurora"]
LIGHT_PALS = [[YELLOW, CYAN], [YELLOW, BLUE], [PINK, PBLUE], PINK_BLUE, PAL, AUR_PAL,
              [(255, 255, 255), (255, 0, 0)]]


@pytest.mark.parametrize("e", SMOOTH)
def test_every_segment_is_palette_light(e):
    """(a) For every smooth effect and many t: each segment is one palette colour, or two
    palette colours overlapping in linear light -- never a third hue."""
    for pal in LIGHT_PALS:
        at_most = 3 if e == "fade3" and len(pal) > 2 else 2
        for k in range(97):
            t = k * 0.29
            for px in render(e, t, N, pal):
                assert _explained(px, pal, at_most), (pal, t, px)


@pytest.mark.parametrize("e", ["jump1", "jump2", "flash1", "flash2", "flash3", "marquee1",
                               "marquee2", "marquee3"])
def test_beat_effects_show_pure_palette_colours(e):
    for t in TS:
        assert all(px in PINK_BLUE + [(0, 0, 0)] for px in render(e, t, N, PINK_BLUE))


def test_single_colour_effects_keep_the_hue():
    c = (10, 200, 30)
    for e in ("fade1", "fade2", "fade3", "flow1", "flow2", "aurora"):
        for k in range(40):
            for px in render(e, k * 0.31, N, [c]):
                assert _explained(px, [c], 1), (e, px)
    # rainbows without a palette use the six-colour LED spectrum as bands
    spec = [(255, 0, 0), (255, 255, 0), (0, 255, 0), (0, 255, 255), (0, 0, 255), (255, 0, 255)]
    for k in range(40):
        for px in render("rainbow1", k * 0.31, N, [c]):
            assert _explained(px, spec), px


def test_fade1_peaks_and_overlap():
    pal = [YELLOW, CYAN]
    assert render("fade1", 0.0, N, pal) == [YELLOW] * N
    assert render("fade1", 2.0, N, pal) == [CYAN] * N
    assert render("fade1", 1.0, N, pal) == [_ref_peak_mix(YELLOW, CYAN, 0.5)] * N


def test_band_edges_are_transition_beads():
    # static fade2 layout (at full breath): Y..Y [bead] C..C, ends never spill
    f = render("fade2", 0.0, N, [YELLOW, CYAN])
    assert f[:7] == [YELLOW] * 7 and f[8:] == [CYAN] * 7
    assert f[7] == _ref_peak_mix(YELLOW, CYAN, 0.5)
    # flowing bands: at most two non-palette segments per edge, mostly pure bands
    for e in ("flow1", "rainbow1", "rainbow2"):
        for k in range(48):
            f = render(e, k / 12, N, [YELLOW, CYAN])
            assert sum(px not in (YELLOW, CYAN) for px in f) <= 4  # two edges on the strip


@pytest.mark.parametrize("e", ["fade1", "fade2", "fade3", "aurora"])
def test_slow_effects_are_smooth_at_12fps(e):
    """(c) no per-frame jump > 25 for fade2/fade3/aurora. fade1 hands the whole strip to
    the next colour every 4 s / L (1 s for four colours) and the bright overlap of two
    saturated lights passes through their full-brightness mix (red+green -> yellow), so
    its path is longer than a straight sRGB blend: bounded at 50 instead."""
    for pal in ([YELLOW, CYAN], [PINK, PBLUE], PINK_BLUE, AUR_PAL):
        frames = [render(e, k / 12, N, pal) for k in range(12 * 21)]
        bound = 50 if e == "fade1" else 25
        assert max(_maxdiff(a, b) for a, b in zip(frames, frames[1:])) <= bound, pal


@pytest.mark.parametrize("e", ["flow1", "flow2", "rainbow1", "rainbow2"])
def test_flowing_bands_are_continuous(e):
    """(c) band edges glide through segments: no hard cuts -- sampling 4x finer makes
    every step >= 2.5x smaller -- and at the slowest speed the steps stay small."""
    for pal in ([YELLOW, CYAN], PINK_BLUE):
        f12 = [render(e, k / 12, N, pal) for k in range(12 * 8)]
        f48 = [render(e, k / 48, N, pal) for k in range(48 * 8)]
        j12 = max(_maxdiff(a, b) for a, b in zip(f12, f12[1:]))
        j48 = max(_maxdiff(a, b) for a, b in zip(f48, f48[1:]))
        assert j48 <= j12 / 2.5 and j12 <= 100
        slow = [render(e, k / 12, N, pal, speed_from_pct(0)) for k in range(12 * 20)]
        assert max(_maxdiff(a, b) for a, b in zip(slow, slow[1:])) <= 30


@pytest.mark.parametrize("e", ["flow1", "flow2", "rainbow1", "rainbow2"])
def test_fast_flow_glides_at_streaming_rate(e):
    """At max speed (x4) an edge travels ~1.25 segments per 1/12 s frame; the overlap
    reach widens with it so no frame is a near-full jump (was 210-255 per segment).
    Measured: 140 worst case (3-4 colour palettes, reach capped at half a band), 99 for
    two colours; 50 % speed is untouched (<= 93)."""
    for pal in ([YELLOW, CYAN], PINK_BLUE, PAL):
        for pct, bound in ((50, 100), (100, 150)):
            sp = speed_from_pct(pct)
            fr = [render(e, k / 12, N, pal, sp, fps=12.0) for k in range(12 * 8)]
            assert max(_maxdiff(a, b) for a, b in zip(fr, fr[1:])) <= bound, (pal, pct)


def test_rainbow_pass_period_four_seconds_at_speed_one():
    for e in ("rainbow1", "rainbow2"):
        a = render(e, 1.3, N, PINK_BLUE)
        assert _maxdiff(a, render(e, 5.3, N, PINK_BLUE)) <= 2
        assert _maxdiff(a, render(e, 3.3, N, PINK_BLUE)) > 20
        # 75 % speed: one pass per ~2 s
        s = speed_from_pct(75)
        assert _maxdiff(render(e, 1.3, N, PINK_BLUE, s), render(e, 3.3, N, PINK_BLUE, s)) <= 2


def test_rainbow_directions_opposite():
    dt = 4.0 / N  # exactly one segment of travel at speed 1
    for t in (0.0, 1.1, 7.3):
        a1, b1 = render("rainbow1", t, N, PINK_BLUE), render("rainbow1", t + dt, N, PINK_BLUE)
        assert _maxdiff(b1[1:], a1[:-1]) <= 2
        assert _maxdiff(b1, a1) > 10
        a2, b2 = render("rainbow2", t, N, PINK_BLUE), render("rainbow2", t + dt, N, PINK_BLUE)
        assert _maxdiff(b2[:-1], a2[1:]) <= 2


def test_rainbow_uses_palette():
    for pal in ([PINK, PBLUE], PINK_BLUE):
        for e in ("rainbow1", "rainbow2"):
            for k in range(40):
                f = render(e, k * 0.37, N, pal)
                assert all(_explained(px, pal) for px in f)
                assert sum(px in pal for px in f) >= N // 2  # mostly pure bands


def test_rainbow_full_spectrum_without_palette():
    for e in ("rainbow1", "rainbow2"):
        hs = sorted(_hsv(px)[0] * 360 for px in render(e, 0.9, N, [(10, 200, 30)]))
        gaps = [b - a for a, b in zip(hs, hs[1:])] + [hs[0] + 360 - hs[-1]]
        assert max(gaps) < 90  # i.e. hues spread over > 270 degrees


def test_speed_from_pct():
    assert speed_from_pct(0) == pytest.approx(0.25)
    assert speed_from_pct(50) == pytest.approx(1.0)
    assert speed_from_pct(100) == pytest.approx(4.0)
    assert speed_from_pct(25) == pytest.approx(0.5)
    assert speed_from_pct(-20) == pytest.approx(0.25)
    assert speed_from_pct(250) == pytest.approx(4.0)
    assert speed_from_pct(45) < speed_from_pct(50) < speed_from_pct(75)


def test_mix_single_equals_render():
    for e in EFFECTS:
        for t in TS:
            assert render_mix([e], t, N, PAL, 1.3) == render(e, t, N, PAL, 1.3)
    assert render_mix(["aurora"], 2.0, N, PAL, 1.0, width=4) == render("aurora", 2.0, N, PAL, 1.0, 4)


def test_mix_alternates_effects():
    mix = ["fade1", "jump1"]
    for t in (0.0, 3.0, 6.9, 16.0, 19.0):
        assert render_mix(mix, t, N, PAL, 1.0) == render("fade1", t, N, PAL)
    for t in (8.0, 11.0, 14.9):
        assert render_mix(mix, t, N, PAL, 1.0) == render("jump1", t, N, PAL)
    # speed scales the slots (8 s / speed)
    assert render_mix(mix, 5.5, N, PAL, 2.0) == render("jump1", 5.5, N, PAL, 2.0)
    # 1 s crossfade at the end of each slot lies between the two effects
    x = render_mix(mix, 7.5, N, PAL, 1.0)
    a, b = render("fade1", 7.5, N, PAL), render("jump1", 7.5, N, PAL)
    assert x != a and x != b
    for px, pa, pb in zip(x, a, b):
        for c, ca, cb in zip(px, pa, pb):
            assert min(ca, cb) - 1 <= c <= max(ca, cb) + 1


def test_mix_crossfade_is_continuous():
    # smooth effects only (jump1's own 1 s beat would land on the slot boundary)
    mix = ["fade1", "aurora", "flow1"]
    frames = [render_mix(mix, k / 12, N, PAL, 1.0) for k in range(12 * 30)]

    def own(e, s):  # the effect's own largest step over the 4 s before the boundary
        fr = [render(e, k / 12, N, PAL) for k in range(12 * s - 48, 12 * s + 2)]
        return max(_maxdiff(a, b) for a, b in zip(fr, fr[1:]))
    for s, (a, b) in zip((8, 16, 24), zip(mix, mix[1:] + mix[:1])):
        # no hard cut at slot boundaries: no bigger than the effects' own motion
        bound = max(own(a, s), own(b, s), 60) + 5  # 60: the old fixed bound
        for f0, f1 in zip(frames[12 * s - 12:12 * s], frames[12 * s - 11:12 * s + 1]):
            assert _maxdiff(f0, f1) <= bound


def test_mix_crossfade_is_light_overlap():
    pal = [YELLOW, CYAN, PINK]
    for mix in (["fade1", "aurora"], ["fade3", "rainbow1"], ["flow1", "fade2"]):
        for k in range(12 * 2):
            t = 7.0 + k / 12  # the 1 s crossfade window and just after
            for px in render_mix(mix, t, N, pal, 1.0):
                assert _explained(px, pal, at_most=3), (mix, t, px)


def test_mix_rainbow_reversal_is_continuous():
    for mix in (["rainbow1", "rainbow2"], ["rainbow2", "rainbow1"]):
        frames = [render_mix(mix, k / 12, N, PINK_BLUE, 1.0) for k in range(12 * 34)]
        within = max(_maxdiff(a, b) for a, b in zip(frames[12:12 * 6], frames[13:12 * 6 + 1]))
        for s in (8, 16, 24, 32):
            seg = frames[12 * s - 18:12 * s + 18]
            for a, b in zip(seg, seg[1:]):
                assert _maxdiff(a, b) <= within + 2
    # the direction really reverses: slot 0 flows up, slot 1 flows down
    mix, dt = ["rainbow1", "rainbow2"], 4.0 / N
    a, b = render_mix(mix, 3.0, N, PINK_BLUE, 1.0), render_mix(mix, 3.0 + dt, N, PINK_BLUE, 1.0)
    assert _maxdiff(b[1:], a[:-1]) <= 2
    a, b = render_mix(mix, 11.0, N, PINK_BLUE, 1.0), render_mix(mix, 11.0 + dt, N, PINK_BLUE, 1.0)
    assert _maxdiff(b[:-1], a[1:]) <= 2


def test_mix_errors_and_empty_strip():
    with pytest.raises(ValueError):
        render_mix([], 0, N, PAL, 1.0)
    with pytest.raises(ValueError):
        render_mix(["fade1", "nope"], 0, N, PAL, 1.0)
    assert render_mix(["fade1", "jump1"], 0, 0, PAL, 1.0) == []


def test_effectmode_accepts_list():
    pal = [PINK, PBLUE]
    m = EffectMode(["rainbow1", "rainbow2"], pal, 0.8, N)
    assert m.fps == 12
    m.open()
    for t in (0.0, 7.4, 9.9):
        assert m.next_frame(t) == render_mix(["rainbow1", "rainbow2"], t, N, pal, 0.8)
    m.close()
    assert EffectMode(["fade3"], pal, 1.0, N).next_frame(1.0) == render("fade3", 1.0, N, pal)
    assert EffectMode(["jump1", "flash1"], pal, 1.0, N).fps == 20
    assert EffectMode(["rainbow1", "flash3"], pal, 1.0, N).fps == 20  # any beat effect
    with pytest.raises(ValueError):
        EffectMode(["fade1", "nope"], pal, 1.0, N)
    with pytest.raises(ValueError):
        EffectMode([], pal, 1.0, N)


# ---- beat effects keep every phase visible at the frame rate (final review minor 5) ----

def _sampled(effect, pct, seconds=12.0, jitter=False):
    """Frames as the engine samples them: t = k / fps (+ up to one timer tick late)."""
    import random
    rng = random.Random(7)
    m = EffectMode(effect, [(255, 0, 0), (0, 0, 255)], speed_from_pct(pct), N)
    ts = [k / m.fps + (rng.uniform(0, SAMPLE_JITTER) if jitter else 0.0)
          for k in range(int(seconds * m.fps))]
    return m, [m.next_frame(t) for t in ts]


@pytest.mark.parametrize("jitter", [False, True])
@pytest.mark.parametrize("pct", [50, 70, 80, 90, 100])
def test_flash3_double_flash_never_missed(pct, jitter):
    m, frames = _sampled("flash3", pct, jitter=jitter)
    speed = min(m.speed, max_speed("flash3", m.fps))
    on = [f[0] != (0, 0, 0) for f in frames]
    # count rising edges = flashes; each 2 s cycle (effect clock) has exactly two
    flashes = sum(1 for a, b in zip([False] + on, on) if b and not a)
    cycles = len(frames) / m.fps * speed / 2.0
    assert flashes >= 2 * int(cycles) - 1
    # and the pause between the two flashes of a cycle is seen too (no merged flash)
    assert flashes <= 2 * (int(cycles) + 1)


@pytest.mark.parametrize("effect,beat", [("flash1", 0.5), ("jump2", 0.5), ("flash2", 0.25),
                                         ("marquee1", 0.5)])
def test_beat_effects_render_every_beat_at_full_speed(effect, beat):
    m, frames = _sampled(effect, 100)
    speed = min(m.speed, max_speed(effect, m.fps))
    beats_seen = {int((k / m.fps) * speed // beat) for k in range(len(frames))}
    total = int(len(frames) / m.fps * speed // beat)
    assert len(beats_seen) >= total  # no beat falls between two frames


def test_flash3_speed_capped_only_when_needed():
    assert max_speed("flash3", 20) == pytest.approx(0.15 / (0.05 + SAMPLE_JITTER))
    assert max_speed("aurora", 20) == float("inf")
    # below the cap the effect is untouched
    assert render("flash3", 0.4, N, PAL, 2.0, fps=20) == render("flash3", 0.4, N, PAL, 2.0)
    # above it, the speed is clamped to the cap
    cap = max_speed("flash3", 20)
    assert render("flash3", 0.4, N, PAL, 4.0, fps=20) == render("flash3", 0.4, N, PAL, cap)


# ---- 投影 (projection): fixed zones cycling colours in place (Task 16 C) -------------

import functools  # noqa: E402

from govee_win11 import effects as fx  # noqa: E402

A, B, C3 = (255, 0, 0), (0, 0, 255), (0, 255, 0)


@functools.lru_cache(maxsize=None)
def _is_light_mix(px, pal):
    """px is a palette colour or the additive overlap of exactly two palette colours."""
    if px in pal:
        return True
    # fine weight grid: near w = 0 gamma makes 0.1 % of a light visible (5/255)
    return any(all(abs(a - b) <= 1 for a, b in zip(px, fx._overlap(pal[i], pal[j], w / 2000)))
               for i, j in itertools.permutations(range(len(pal)), 2) for w in range(1, 2000))


def test_projection_catalog():
    assert EFFECTS["projection"] == "投影"
    assert EffectMode("projection", PAL, 1.0, N).fps == fx.SMOOTH_FPS


@pytest.mark.parametrize("pal", [(A, B), (A, C3, B)])
def test_projection_zones_never_move(pal):
    for k in range(0, 160):
        t = k * 0.05
        f = render("projection", t, N, list(pal))
        for g in range(0, N, 2):
            assert len(set(f[g:g + 2])) == 1, (t, f)        # a group is one colour
        for px in f:
            assert _is_light_mix(px, pal), (t, px)


def test_projection_two_colours_swap_after_one_step():
    step = fx._PROJ_HOLD + fx._PROJ_XFADE
    assert (fx._PROJ_HOLD, fx._PROJ_XFADE) == (1.5, 2.0)
    f = render("projection", 0.5, N, [A, B])
    assert f == [A if (i // 2) % 2 == 0 else B for i in range(N)]      # A A B B A A ...
    g = render("projection", step + 0.5, N, [A, B])
    assert g == [B if (i // 2) % 2 == 0 else A for i in range(N)]      # B B A A B B ...
    mid = render("projection", fx._PROJ_HOLD + fx._PROJ_XFADE / 2, N, [A, B])
    assert mid == [fx._overlap(A, B, 0.5)] * N                         # the overlap bead
    assert mid[0][0] > 200 and mid[0][2] > 200                         # bright, not a dim dip


def test_projection_three_colours_cycle_with_neighbour_offset():
    pal = [A, C3, B]
    step = fx._PROJ_HOLD + fx._PROJ_XFADE
    for k in range(4):
        f = render("projection", k * step + 0.2, N, pal)
        assert f == [pal[(i // 2 + k) % 3] for i in range(N)], k


@pytest.mark.parametrize("width,group", [(None, 2), (1, 1), (3, 3), (5, 5), (9, 5), (0.2, 1)])
def test_projection_width_is_group_size(width, group):
    f = render("projection", 0.5, N, [A, B], width=width)
    assert f == [A if (i // group) % 2 == 0 else B for i in range(N)]


def test_projection_speed_scales_time():
    for t in (0.3, 1.9, 2.6, 5.0):
        assert render("projection", t, N, [A, B], 2.0) == render("projection", 2 * t, N, [A, B])
        assert render("projection", t, N, [A, B], 0.5) == render("projection", t / 2, N, [A, B])


def test_projection_mode_passes_width():
    m = EffectMode("projection", [A, B], 1.0, N, width=3)
    assert m.next_frame(0.5) == [A if (i // 3) % 2 == 0 else B for i in range(N)]


# ---- Task 17: 混合方式 (linear light / perceptual sRGB), one global switch ----------------

from govee_win11 import effects as fx  # noqa: E402


@pytest.fixture
def perceptual():
    fx.set_mix("perceptual")
    try:
        yield
    finally:
        fx.set_mix("linear")


def _srgb_unit(c):
    return c / 255.0


def _from_unit(x):
    return round(255 * min(1.0, max(0.0, x)))


def test_mix_default_is_linear_and_validated():
    assert fx.get_mix() == "linear"
    assert fx.MIX_MODES == ("linear", "perceptual")
    with pytest.raises(ValueError):
        fx.set_mix("bogus")
    assert fx.get_mix() == "linear"


def test_perceptual_mix_changes_crossfade_mid_values(perceptual):
    red, blue = (255, 0, 0), (0, 0, 255)
    orange, teal = (255, 128, 0), (0, 128, 128)
    p_q = fx._overlap(red, blue, 0.25)
    p_mid = fx._overlap(orange, teal, 0.5)
    fx.set_mix("linear")
    l_q = fx._overlap(red, blue, 0.25)
    l_mid = fx._overlap(orange, teal, 0.5)
    assert p_q != l_q and p_mid != l_mid
    # sRGB-space weighted sum, same peak-preserving rescale: red 3/4 + blue 1/4 ->
    # (1, 0, 1/3) in sRGB (linear light gives a brighter blue share)
    assert p_q == (255, 0, 85)
    assert l_q[0] == 255 and l_q[2] > 85
    # red/blue meet at full-brightness magenta in both at the exact midpoint
    assert fx._overlap(red, blue, 0.5) == (255, 0, 255)


def test_perceptual_mix_keeps_endpoints(perceptual):
    for a, b in ((YELLOW, CYAN), (PINK, PBLUE), ((255, 0, 0), (0, 0, 255))):
        assert fx._overlap(a, b, 0.0) == a and fx._overlap(a, b, 1.0) == b
        assert fx._light([(a, 1.0)]) == a
    assert render("fade1", 0.0, N, [YELLOW, CYAN]) == [YELLOW] * N
    assert render("fade1", 2.0, N, [YELLOW, CYAN]) == [CYAN] * N


def test_perceptual_mix_switch_applies_to_effect_frames(perceptual):
    pal = [(255, 128, 0), (0, 128, 128)]
    p = render("fade1", 1.0, N, pal)
    fx.set_mix("linear")
    assert render("fade1", 1.0, N, pal) != p


@pytest.mark.parametrize("e", SMOOTH)
def test_perceptual_mix_is_still_palette_only(perceptual, e):
    """In sRGB mixing every segment is a non-negative sRGB-space combination of one or two
    palette colours: still never a third hue."""
    for pal in LIGHT_PALS:
        at_most = 3 if e == "fade3" and len(pal) > 2 else 2
        for k in range(41):
            t = k * 0.29
            for px in render(e, t, N, pal):
                assert _explained(px, pal, at_most, s2l=_srgb_unit, l2s=_from_unit), (pal, t, px)


def test_intensity_keeps_the_hue_in_both_mixes(perceptual):
    c = (10, 200, 30)
    for k in (0.2, 0.5, 0.9):
        px = fx._scale(c, k)
        assert _explained(px, [c], 1, s2l=_srgb_unit, l2s=_from_unit), px
    # the peak channel dims the same way in both mixes (perceived intensity of k)
    p = fx._scale((255, 0, 0), 0.5)
    fx.set_mix("linear")
    assert fx._scale((255, 0, 0), 0.5) == p
