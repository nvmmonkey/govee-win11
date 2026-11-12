import colorsys
import itertools
import re

import pytest

from govee_win11.palette import METHODS, MOODS, STYLES, generate


def hsl(hexstr):
    r, g, b = (int(hexstr[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, s, l


def hdist(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def test_returns_n_hex():
    for n in (1, 3, 5, 8):
        out = generate("vivid", 100, "focus", "triadic", n=n, seed=1)
        assert len(out) == n
        assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in out)


def test_complementary_hues_180_apart():
    out = generate("vivid", 40, "focus", "complementary", n=5, seed=1)
    assert abs(hdist(hsl(out[0])[0], hsl(out[1])[0]) - 180) <= 15


def test_analogous_within_30():
    base = 200
    out = generate("vivid", base, "focus", "analogous", n=6, seed=1)
    for c in out:
        assert hdist(hsl(c)[0], base) <= 30 + 3


def test_random_seeded_is_deterministic():
    a = generate("neon", 10, "joy", "random", n=5, seed=42)
    b = generate("neon", 10, "joy", "random", n=5, seed=42)
    c = generate("neon", 10, "joy", "random", n=5, seed=43)
    assert a == b
    assert a != c


def test_styles_differ():
    avgs = []
    for s in STYLES:
        out = generate(s, 120, "focus", "triadic", n=6, seed=1)
        avgs.append(sum(hsl(c)[1] for c in out) / len(out))
    assert len(set(round(a, 3) for a in avgs)) == len(avgs)


def test_all_ids_accepted():
    for s, m, me in itertools.product(STYLES, MOODS, METHODS):
        out = generate(s, 77.0, m, me, n=5, seed=3)
        assert len(out) == 5


def test_lightness_not_too_dark():
    for s, m, me in itertools.product(STYLES, MOODS, METHODS):
        for c in generate(s, 250, m, me, n=7, seed=3):
            assert hsl(c)[2] >= 0.3


@pytest.mark.parametrize("kw", [{"style": "x"}, {"mood": "x"}, {"method": "x"}])
def test_unknown_id_raises_valueerror(kw):
    args = dict(style="vivid", hue=0, mood="focus", method="triadic")
    args.update(kw)
    with pytest.raises(ValueError):
        generate(**args)


def test_palette_never_whitish():
    for s, m, me in itertools.product(STYLES, MOODS, METHODS):
        for hue in (0, 90, 200, 300):
            for c in generate(s, hue, m, me, n=7, seed=5):
                _, sat, l = hsl(c)
                assert l <= 0.65 + 0.01, (s, m, me, c)
                assert sat >= 0.55 - 0.02, (s, m, me, c)


def test_complementary_survives_warm_mood():
    for hue in (0, 100, 220, 300):
        out = generate("vivid", hue, "warm", "complementary", n=5, seed=1)
        assert abs(hdist(hsl(out[0])[0], hsl(out[1])[0]) - 180) <= 15
