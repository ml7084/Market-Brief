"""Draw the app icons (sunrise over a rising chart line). Run: python scripts/make_icons.py"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "web" / "icons"
BG_TOP, BG_BOTTOM = (16, 42, 48), (9, 14, 20)
SUN, LINE, GRID = (247, 178, 64), (92, 214, 196), (255, 255, 255, 28)


def draw(size: int, padding: float = 0.0) -> Image.Image:
    s = size * 4  # supersample, then downscale for smooth edges
    img = Image.new("RGB", (s, s))
    d = ImageDraw.Draw(img, "RGBA")
    for y in range(s):
        t = y / s
        d.line([(0, y), (s, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))

    inset = s * padding
    span = s - 2 * inset

    def p(x: float, y: float) -> tuple[float, float]:
        return inset + x * span, inset + y * span

    for gy in (0.35, 0.5, 0.65):
        d.line([p(0.14, gy), p(0.86, gy)], fill=GRID, width=max(2, s // 160))
    cx, cy = p(0.64, 0.42)
    r = span * 0.15
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SUN)
    pts = [p(0.16, 0.72), p(0.34, 0.60), p(0.48, 0.66), p(0.66, 0.46), p(0.84, 0.30)]
    width = max(4, round(span * 0.06))
    d.line(pts, fill=LINE, width=width, joint="curve")
    for x, y in (pts[0], pts[-1]):
        d.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=LINE)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    draw(180).save(OUT / "apple-touch-icon.png")
    draw(192).save(OUT / "icon-192.png")
    draw(512).save(OUT / "icon-512.png")
    draw(512, padding=0.1).save(OUT / "icon-maskable-512.png")  # keep the art inside the maskable safe zone
    print("icons written to", OUT)
