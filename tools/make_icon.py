"""Fixer's app icon, drawn as clean shapes (11-20, the owner: an eye, black ombre background, just a few stars,
dark-mode friendly, not purple, simple and recognisable). Drawn at 4x and scaled down for smooth edges.

    python tools/make_icon.py                       writes the variants to tools/icon-out/
    python tools/make_icon.py --final 1-amber-outline   writes Fixer/Assets.xcassets/AppIcon.appiconset/icon-1024.png
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 4096                       # drawing size (4x)
OUT = 1024
HERE = Path(__file__).resolve().parent

IRIS = {                       # inner, outer colour of the iris
    "teal": ((120, 255, 236), (0, 120, 128)),
    "amber": ((255, 214, 120), (176, 92, 12)),
    "ice": ((210, 240, 255), (40, 120, 200)),
    "mint": ((190, 255, 200), (20, 140, 90)),
}


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def background() -> Image.Image:
    """Charcoal at the top fading to black at the bottom."""
    top, bottom = (34, 36, 42), (0, 0, 0)
    bg = Image.new("RGB", (S, S))
    px = ImageDraw.Draw(bg)
    for y in range(S):
        t = (y / (S - 1)) ** 0.9
        px.line([(0, y), (S, y)], fill=lerp(top, bottom, t))
    return bg


def almond(cx: float, cy: float, half_w: float, half_h: float, n: int = 400) -> list[tuple[float, float]]:
    """An eye shape: two arcs meeting in points at the corners (sin^p for a slightly fuller middle)."""
    pts = []
    for i in range(n + 1):
        t = i / n
        x = cx - half_w + 2 * half_w * t
        h = half_h * math.sin(math.pi * t) ** 0.85
        pts.append((x, cy - h))
    for i in range(n, -1, -1):
        t = i / n
        x = cx - half_w + 2 * half_w * t
        h = half_h * math.sin(math.pi * t) ** 0.85
        pts.append((x, cy + h))
    return pts


def radial(size: int, inner, outer, power: float = 1.2) -> Image.Image:
    im = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(im)
    for r in range(size // 2, 0, -2):
        t = (r / (size / 2)) ** power
        d.ellipse([size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r], fill=lerp(inner, outer, t))
    return im


def sparkle(d: ImageDraw.ImageDraw, x: float, y: float, r: float, alpha: int) -> None:
    w = r * 0.22
    d.polygon([(x, y - r), (x + w, y - w), (x + r, y), (x + w, y + w), (x, y + r), (x - w, y + w), (x - r, y),
               (x - w, y - w)], fill=(255, 255, 255, alpha))


def bezier(p0, p1, p2, n=300):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]) for t in (i / n for i in range(n + 1))]


def draw(colour: str, style: str, swoop: bool = True, burst: bool = True) -> Image.Image:
    """style: "solid" (a white almond) or "outline" (a white ring). swoop: one thin curve through the eye.
    burst: a sparse, quiet ring of dots and short rays around it."""
    inner, outer = IRIS[colour]
    img = background().convert("RGBA")
    cx, cy = S / 2, S * 0.52
    hw, hh = S * 0.30, S * 0.175

    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - hw * 0.8, cy - hh * 1.3, cx + hw * 0.8, cy + hh * 1.3], fill=outer + (60,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * 0.07)))

    deco = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dd = ImageDraw.Draw(deco)
    if burst:
        import random
        rnd = random.Random(7)
        for k in range(16):
            a = 2 * math.pi * k / 16 + rnd.uniform(-0.12, 0.12)
            r = rnd.uniform(1.18, 1.42)
            x, y = cx + math.cos(a) * hw * r, cy + math.sin(a) * hh * r * 1.55
            c = (inner if k % 3 else (255, 255, 255)) + (rnd.randint(110, 190),)
            if k % 4 == 0:                                   # a short ray pointing out
                l = S * rnd.uniform(0.022, 0.036)
                dd.line([(x, y), (x + math.cos(a) * l, y + math.sin(a) * l * 0.9)], fill=c, width=int(S * 0.006))
            else:
                rr = S * rnd.uniform(0.004, 0.009)
                dd.ellipse([x - rr, y - rr, x + rr, y + rr], fill=c)
    if swoop:
        curve = bezier((S * 0.12, S * 0.93), (S * 0.50, S * 0.62), (S * 0.93, S * 0.07))
        dd.line(curve, fill=(236, 238, 242, 210), width=int(S * 0.008), joint="curve")
    img.alpha_composite(deco)

    shape = almond(cx, cy, hw, hh)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).polygon(shape, fill=255)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.polygon(shape, fill=(236, 238, 242, 255) if style == "solid" else (10, 11, 14, 255))
    ir = S * 0.135
    iris = radial(int(ir * 2), inner, outer)
    circle = Image.new("L", iris.size, 0)
    ImageDraw.Draw(circle).ellipse([0, 0, iris.size[0] - 1, iris.size[1] - 1], fill=255)
    layer.paste(iris, (int(cx - ir), int(cy - ir)), circle)
    pr = ir * 0.42
    ld.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=(6, 6, 8, 255))
    cr = ir * 0.15
    hx, hy = cx - ir * 0.32, cy - ir * 0.36
    ld.ellipse([hx - cr, hy - cr, hx + cr, hy + cr], fill=(255, 255, 255, 240))
    clipped = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    clipped.paste(layer, (0, 0), mask)
    img.alpha_composite(clipped)

    if style == "outline":                                  # a clean ring: the almond minus a smaller almond
        t = S * 0.024
        ring = Image.new("L", (S, S), 0)
        rd = ImageDraw.Draw(ring)
        rd.polygon(almond(cx, cy, hw + t, hh + t * 1.25), fill=255)
        rd.polygon(shape, fill=0)
        white = Image.new("RGBA", (S, S), (236, 238, 242, 255))
        img.paste(white, (0, 0), ring)

    stars = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stars)
    sparkle(sd, S * 0.80, S * 0.22, S * 0.026, 160)
    sparkle(sd, S * 0.19, S * 0.26, S * 0.013, 100)
    img.alpha_composite(stars)
    return img.convert("RGB").resize((OUT, OUT), Image.LANCZOS)


VARIANTS = {                     # name: (colour, style, swoop, burst)
    "1-amber-outline": ("amber", "outline", True, True),
    "2-amber-solid": ("amber", "solid", True, True),
    "3-teal-outline": ("teal", "outline", True, True),
    "4-ice-outline": ("ice", "outline", True, True),
    "5-amber-outline-quiet": ("amber", "outline", True, False),
    "6-teal-solid-quiet": ("teal", "solid", True, False),
}


LINE = {                         # stroke gradient (light, deep) for the line icons
    "amber": ((255, 222, 150), (226, 128, 30)),
    "mint": ((200, 255, 225), (40, 190, 140)),
}


def gradient_fill(c1, c2) -> Image.Image:
    """A diagonal gradient, top-left light to bottom-right deep."""
    g = Image.new("RGB", (256, 256))
    d = ImageDraw.Draw(g)
    for i in range(512):
        t = i / 511
        d.line([(i, 0), (0, i)], fill=lerp(c1, c2, t), width=2)
    return g.resize((S, S), Image.BILINEAR).convert("RGBA")


def lid(cx, cy, hw, h_out, thick, sign, t0=0.0, t1=1.0, n=500):
    """A tapered stroke along one lid: full thickness in the middle, a hairline at the ends."""
    outer, inner = [], []
    for i in range(n + 1):
        t = t0 + (t1 - t0) * i / n
        x = cx - hw + 2 * hw * t
        h = h_out * math.sin(math.pi * t) ** 0.85
        w = thick * math.sin(math.pi * t) ** 0.7 + S * 0.0015
        outer.append((x, cy + sign * h))
        inner.append((x, cy + sign * (h - w)))
    return outer + inner[::-1]


def tail(p0, p1, p2, w_max, n=400):
    """A tapered curve: hairline at the start, widest a third of the way, a fine point at the end."""
    pts = bezier(p0, p1, p2, n)
    left, right = [], []
    for i in range(len(pts)):
        a = pts[max(0, i - 1)]
        b = pts[min(len(pts) - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy) or 1
        nx, ny = -dy / L, dx / L
        t = i / (len(pts) - 1)
        w = w_max * (math.sin(math.pi * min(1.0, t * 1.4)) if t < 0.36 else (1 - t) / 0.64) + S * 0.0012
        left.append((pts[i][0] + nx * w / 2, pts[i][1] + ny * w / 2))
        right.append((pts[i][0] - nx * w / 2, pts[i][1] - ny * w / 2))
    return left + right[::-1]


def draw_line(colour: str, lids: str = "colour", glow_colour: str | None = None) -> Image.Image:
    """The line icon: tapered lids, the lower one flowing into a flick (the swoop), a fine iris ring, a pupil."""
    c1, c2 = LINE[colour]
    g2 = LINE[glow_colour][1] if glow_colour else c2
    img = background().convert("RGBA")
    cx, cy = S * 0.47, S * 0.54
    hw, hh = S * 0.345, S * 0.185
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - S * 0.2, cy - S * 0.2, cx + S * 0.2, cy + S * 0.2], fill=g2 + (52,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * 0.08)))

    colour_mask = Image.new("L", (S, S), 0)
    white_mask = Image.new("L", (S, S), 0)
    cm, wm = ImageDraw.Draw(colour_mask), ImageDraw.Draw(white_mask)
    lid_mask = Image.new("L", (S, S), 0)
    lid_draw = cm if lids == "colour" else wm if lids == "white" else ImageDraw.Draw(lid_mask)
    lid_draw.polygon(lid(cx, cy, hw, hh, S * 0.040, -1), fill=255)                  # upper lid, the bold one
    lid_draw.polygon(lid(cx, cy, hw, hh * 0.92, S * 0.020, +1, 0.04, 0.96), fill=255)   # lower lid, finer
    # the flick: from the outer corner, sweeping up and out to the top right
    lid_draw.polygon(tail((cx + hw * 0.96, cy + S * 0.004), (cx + hw * 1.25, cy - S * 0.02),
                          (S * 0.86, S * 0.20), S * 0.022), fill=255)
    ir = S * 0.122
    ring = S * 0.017
    cm.ellipse([cx - ir, cy - ir, cx + ir, cy + ir], fill=255)
    cm.ellipse([cx - ir + ring, cy - ir + ring, cx + ir - ring, cy + ir - ring], fill=0)
    pr = ir * 0.40
    cm.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=255)
    img.paste(gradient_fill(c1, c2), (0, 0), colour_mask)
    img.paste(Image.new("RGBA", (S, S), (238, 240, 244, 255)), (0, 0), white_mask)
    if lids in LINE:                                   # lids in another colour than the iris
        img.paste(gradient_fill(*LINE[lids]), (0, 0), lid_mask)
    hr = pr * 0.30
    hx, hy = cx - pr * 0.28, cy - pr * 0.30
    ImageDraw.Draw(img).ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=(20, 20, 24, 255))   # a tiny cut-out light

    stars = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stars)
    sparkle(sd, S * 0.20, S * 0.24, S * 0.020, 150)
    sparkle(sd, S * 0.30, S * 0.82, S * 0.011, 90)
    img.alpha_composite(stars)
    return img.convert("RGB").resize((OUT, OUT), Image.LANCZOS)


LINE_VARIANTS = {                # iris colour, lids ("colour" = same as the iris, "white", or another colour), glow
    "M1-amber-lids-mint-iris": ("mint", "amber", None),
    "M2-white-lids-mint-iris-amber-glow": ("mint", "white", "amber"),
    "M3-white-lids-mint-iris": ("mint", "white", None),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", help="colour-style to write as the app icon, e.g. teal-solid")
    a = ap.parse_args()
    if a.final:
        dest = HERE.parent / "Fixer" / "Assets.xcassets" / "AppIcon.appiconset" / "icon-1024.png"
        (draw_line(*LINE_VARIANTS[a.final]) if a.final in LINE_VARIANTS else draw(*VARIANTS[a.final])).save(dest, optimize=True)
        print("wrote", dest)
        return
    out = HERE / "icon-out"
    out.mkdir(exist_ok=True)
    for f in out.glob("*.png"):
        f.unlink()
    for name, v in LINE_VARIANTS.items():
        draw_line(*v).save(out / f"{name}.png")
    print("wrote", out)


if __name__ == "__main__":
    main()
