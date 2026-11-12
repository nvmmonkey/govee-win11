"""Palette generator (配色推荐): style x hue x mood x method -> list of #rrggbb."""
import colorsys
import random

STYLES = {
    "refreshing": "清新", "retro": "复古", "elegant": "优雅",
    "vivid": "鲜艳", "pastel": "柔和", "neon": "霓虹",
}
MOODS = {
    "work": "工作", "leisure": "休闲", "joy": "欢乐", "warm": "温暖",
    "in_love": "恋爱", "party": "派对", "gourmet": "美食", "calm": "平静",
    "happiness": "幸福", "lively": "活力", "romantic": "浪漫", "focus": "专注",
}
METHODS = {
    "gradient": "渐变色", "complementary": "互补色", "random": "随机色",
    "double_complementary": "两组互补色", "split_complementary": "分裂互补色",
    "analogous": "类似色", "triadic": "三角色",
}

# style -> ((S_min, S_max), (L_min, L_max)); never whitish/grey: S >= MIN_S, L in [MIN_L, MAX_L]
# after moods. Pastel stays softer than vivid via lower S and the lightest allowed L.
STYLE_RANGES = {
    "refreshing": ((0.65, 0.85), (0.50, 0.62)),
    "retro":      ((0.60, 0.75), (0.42, 0.55)),
    "elegant":    ((0.55, 0.60), (0.38, 0.50)),
    "vivid":      ((0.85, 1.00), (0.45, 0.55)),
    "pastel":     ((0.55, 0.70), (0.60, 0.65)),
    "neon":       ((0.95, 1.00), (0.50, 0.60)),
}
# mood -> (hue_target or None, pull strength 0..1, dS, dL, dHue degrees)
MOOD_ADJUST = {
    "work":      (None, 0, -0.10, 0.00, 0),
    "leisure":   (None, 0, -0.05, 0.05, 0),
    "joy":       (None, 0, 0.10, 0.05, 0),
    "warm":      (35, 0.6, 0.00, 0.00, 0),   # pull hues towards 20-50 degrees
    "in_love":   (340, 0.4, 0.00, 0.05, 0),
    "party":     (None, 0, 0.20, 0.00, 0),
    "gourmet":   (25, 0.4, 0.05, 0.00, 0),
    "calm":      (None, 0, -0.25, 0.00, 0),
    "happiness": (50, 0.3, 0.05, 0.05, 0),
    "lively":    (None, 0, 0.15, 0.00, 0),
    "romantic":  (310, 0.3, -0.05, 0.05, 0),
    "focus":     (None, 0, -0.05, 0.00, 0),
}
MIN_L, MAX_L = 0.35, 0.65
MIN_S = 0.55


def _pull(h, target, k):
    d = (target - h + 180) % 360 - 180
    return (h + d * k) % 360


def _hues(method, h, n, rng):
    if method == "random":
        return [rng.uniform(0, 360) for _ in range(n)]
    if method == "gradient":
        return [h + (90 * i / (n - 1) if n > 1 else 0) for i in range(n)]
    if method == "analogous":
        return [h + (-30 + 60 * i / (n - 1) if n > 1 else 0) for i in range(n)]
    base = {
        "complementary": [0, 180],
        "split_complementary": [0, 150, 210],
        "double_complementary": [0, 180, 60, 240],
        "triadic": [0, 120, 240],
    }[method]
    return [h + base[i % len(base)] for i in range(n)]


def generate(style: str, hue: float, mood: str, method: str,
             n: int = 5, seed: int | None = None) -> list[str]:
    if style not in STYLES:
        raise ValueError(f"unknown style: {style}")
    if mood not in MOODS:
        raise ValueError(f"unknown mood: {mood}")
    if method not in METHODS:
        raise ValueError(f"unknown method: {method}")
    rng = random.Random(seed)
    (s0, s1), (l0, l1) = STYLE_RANGES[style]
    target, k, ds, dl, dh = MOOD_ADJUST[mood]
    # hue set length: gradient/analogous/random spread over n; others cycle
    set_len = {"complementary": 2, "split_complementary": 3,
               "double_complementary": 4, "triadic": 3}.get(method, n)
    out = []
    base = hue % 360
    if target is not None:
        base = _pull(base, target, k)   # rotate the whole hue set, keeping its structure
    for i, h in enumerate(_hues(method, base, n, rng)):
        h = (h + dh) % 360
        t = (i * 0.618) % 1.0          # spread S/L over the style range
        s = s0 + (s1 - s0) * t + ds
        l = l0 + (l1 - l0) * (1 - t)
        cycle = i // set_len           # repeated hue: nudge lightness
        if cycle:
            l += 0.04 * cycle * (1 if cycle % 2 else -1)
        s = min(1.0, max(MIN_S, s))
        l = min(MAX_L, max(MIN_L, l + dl))
        r, g, b = colorsys.hls_to_rgb(h / 360, l, s)
        out.append("#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255)))
    return out
