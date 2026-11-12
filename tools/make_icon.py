"""Generate assets/GoveeWin11.ico: a rainbow LED strip on a dark rounded tile.

    python tools/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets" / "GoveeWin11.ico"
COLORS = [(255, 77, 77), (255, 150, 40), (255, 222, 60), (61, 214, 120),
          (47, 200, 220), (70, 120, 255), (150, 92, 255)]
SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size: int = 1024) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size * 0.04
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=size * 0.22,
                        fill=(24, 26, 33, 255))
    n = len(COLORS)
    x0, x1 = size * 0.14, size * 0.86
    y0, y1 = size * 0.40, size * 0.60
    gap = size * 0.018
    w = (x1 - x0 - gap * (n - 1)) / n
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    for i, c in enumerate(COLORS):
        a = x0 + i * (w + gap)
        g.rounded_rectangle([a - gap, y0 - size * 0.06, a + w + gap, y1 + size * 0.06],
                            radius=size * 0.05, fill=c + (150,))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.045))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img)
    for i, c in enumerate(COLORS):
        a = x0 + i * (w + gap)
        d.rounded_rectangle([a, y0, a + w, y1], radius=size * 0.03, fill=c + (255,))
    return img


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(OUT, sizes=[(s, s) for s in SIZES])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
