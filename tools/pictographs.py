#!/usr/bin/env python3
"""Draws the Make screen's quick-pick pictographs and wires them into the app.

The icon set is generated, not hand-drawn, so it stays one consistent style ("simple and tasteful"): monochrome line
art on a 48-unit grid, one stroke weight, round caps, no text, no faces. Every shape is reduced to plain <path>
polylines (curves are sampled, crops are clipped here in Python), so Xcode's SVG support never meets a clipPath,
mask, gradient, dash pattern or <text> it might not render.

Run it from anywhere:

    python tools/pictographs.py              # write the imagesets + Fixer/Create/PictographTable.swift
    python tools/pictographs.py --preview X  # also write a contact sheet (HTML) to X
    python tools/pictographs.py --check      # only compare the drawings with the live catalogue

Outputs (all regenerated from scratch each run; never edit them by hand):
  Fixer/Assets.xcassets/Pictographs/pg_<group>_<slug>.imageset   template-rendered, vector-preserving SVG
  Fixer/Create/PictographTable.swift                             option text -> asset name, per group

Option keys must match the catalogue's option TEXT (case-insensitive on the phone). The catalogue lives in
recipes.json under FIXER_HOME (C:\\FixerDesktop by default); --check lists options with no drawing and drawings
whose option is no longer in the catalogue.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ASSET_DIR = REPO / "Fixer" / "Assets.xcassets" / "Pictographs"
TABLE_FILE = REPO / "Fixer" / "Create" / "PictographTable.swift"
FIXER_HOME = Path(os.environ.get("FIXER_HOME", r"C:\FixerDesktop"))

GRID = 48.0
STROKE = 1.9      # one weight for the whole set; ~1.75pt at the tile's 44pt
THIN = 1.2        # guides only: rulers, sight lines, film sprockets

Pt = tuple[float, float]
Poly = list[Pt]


# ---------------------------------------------------------------------------------------------------------------------
# Geometry: everything is a polyline in grid units, y down.
# ---------------------------------------------------------------------------------------------------------------------

def catmull(pts: list[Pt], closed: bool = False, n: int = 10) -> Poly:
    """A smooth curve through every point (uniform Catmull-Rom), sampled n times per span."""
    if len(pts) < 3:
        return list(pts)
    p = list(pts)
    p = ([p[-1]] + p + [p[0], p[1]]) if closed else ([p[0]] + p + [p[-1]])
    out: Poly = []
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        for k in range(n):
            t = k / n
            t2, t3 = t * t, t * t * t
            out.append(tuple(
                0.5 * (2 * p1[j] + (-p0[j] + p2[j]) * t + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2
                       + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3)
                for j in (0, 1)))
    out.append(p[-2])
    return out


def arc(cx: float, cy: float, rx: float, ry: float | None = None, a0: float = 0, a1: float = 360,
        rot: float = 0, n: int | None = None) -> Poly:
    """An ellipse arc; angles in degrees, 0 = right, 90 = down."""
    ry = rx if ry is None else ry
    n = n or max(8, int(abs(a1 - a0) / 6))
    c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    out = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        x, y = rx * math.cos(a), ry * math.sin(a)
        out.append((cx + x * c - y * s, cy + x * s + y * c))
    return out


def rrect(x0: float, y0: float, x1: float, y1: float, r: float) -> Poly:
    """A closed rounded rectangle."""
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    pts: Poly = []
    for cx, cy, a in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        pts += arc(cx, cy, r, a0=a, a1=a + 90, n=6)
    return pts + [pts[0]]


def xf(polys: list[Poly], s: float = 1, sx: float | None = None, sy: float | None = None, rot: float = 0,
       about: Pt = (0, 0), dx: float = 0, dy: float = 0) -> list[Poly]:
    """Scale about `about`, rotate about it, then translate."""
    sx = s if sx is None else sx
    sy = s if sy is None else sy
    c, sn = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    ax, ay = about
    out = []
    for poly in polys:
        q = []
        for x, y in poly:
            x, y = (x - ax) * sx, (y - ay) * sy
            x, y = x * c - y * sn, x * sn + y * c
            q.append((x + ax + dx, y + ay + dy))
        out.append(q)
    return out


def warp(polys: list[Poly], fn) -> list[Poly]:
    """Apply a point function to every point (perspective foreshortening and the like)."""
    return [[fn(x, y) for x, y in poly] for poly in polys]


def _clip_seg(a: Pt, b: Pt, r):
    x0, y0, x1, y1 = r
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return (a[0] + t0 * dx, a[1] + t0 * dy), (a[0] + t1 * dx, a[1] + t1 * dy), t0 > 0, t1 < 1


def clip(polys: list[Poly], r) -> list[Poly]:
    """Crop polylines to the rectangle r = (x0, y0, x1, y1): the viewfinder crops in the camera icons."""
    out: list[Poly] = []
    for pts in polys:
        cur: Poly = []
        for a, b in zip(pts, pts[1:]):
            c = _clip_seg(a, b, r)
            if c is None:
                if len(cur) > 1:
                    out.append(cur)
                cur = []
                continue
            s, e, s_cut, e_cut = c
            if not cur or s_cut:
                if len(cur) > 1:
                    out.append(cur)
                cur = [s]
            cur.append(e)
            if e_cut:
                out.append(cur)
                cur = []
        if len(cur) > 1:
            out.append(cur)
    return out


def dashed(pts: Poly, on: float = 1.8, off: float = 1.8) -> list[Poly]:
    """Cut a polyline into dashes (drawn here because SVG dash support in asset catalogs is not guaranteed)."""
    out: list[Poly] = []
    cur: Poly = [pts[0]]
    drawing, left = True, on
    for a, b in zip(pts, pts[1:]):
        seg = math.dist(a, b)
        pos = 0.0
        while seg - pos > left:
            pos += left
            t = pos / seg
            p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            if drawing:
                cur.append(p)
                out.append(cur)
            else:
                cur = [p]
            drawing = not drawing
            left = on if drawing else off
        left -= seg - pos
        if drawing:
            cur.append(b)
    if drawing and len(cur) > 1:
        out.append(cur)
    return out


def line(*pts: Pt) -> Poly:
    return list(pts)


# ---------------------------------------------------------------------------------------------------------------------
# A drawing: strokes (optionally thin) and solid fills.
# ---------------------------------------------------------------------------------------------------------------------

class Art:
    def __init__(self) -> None:
        self.items: list[tuple[str, Poly, float]] = []

    def s(self, polys, w: float = STROKE) -> "Art":
        for p in ([polys] if polys and isinstance(polys[0], tuple) else polys):
            if len(p) > 1:
                self.items.append(("s", p, w))
        return self

    def f(self, polys) -> "Art":
        for p in ([polys] if polys and isinstance(polys[0], tuple) else polys):
            if len(p) > 2:
                self.items.append(("f", p, 0))
        return self

    def svg(self) -> str:
        def d(p: Poly) -> str:
            return "M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in p)
        body = []
        for kind, p, w in self.items:
            if kind == "f":
                body.append(f'<path d="{d(p)} Z" fill="#000"/>')
            else:
                body.append(f'<path d="{d(p)}" fill="none" stroke="#000" stroke-width="{w:g}" '
                            f'stroke-linecap="round" stroke-linejoin="round"/>')
        g = int(GRID)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{g}" height="{g}" viewBox="0 0 {g} {g}">'
                + "".join(body) + "</svg>\n")


# ---------------------------------------------------------------------------------------------------------------------
# The person: a head and one plain column, nothing else (no arms, no hands, no face). The legs are a single line up
# from the floor. Used where a picture needs "someone" (height, camera framings); the Build icons use `form` instead.
# Designed in a 100-unit-tall space (head top 0, soles 100); widths are half-widths from the centre line.
# ---------------------------------------------------------------------------------------------------------------------

BASE = dict(neck=3.4, sh=11.0, ch=10.0, wa=8.4, hip=10.6, kn=7.4, an=5.6, head=7.0, legs=1.0, widen=1.5)


def figure_parts(over: dict | None = None) -> dict[str, list[Poly]]:
    """The person in its own 100-unit space: {'head', 'body'} (body = the column and the leg line)."""
    p = dict(BASE, **(over or {}))
    L = p["legs"]                       # leg length multiplier; the torso shrinks to keep the total at 100
    hip_y = 100 - 52.5 * L
    t = hip_y / 47.5

    def ty(y: float) -> float:
        return y * t

    def ly(y: float) -> float:
        return hip_y + (y - 47.5) * L

    head_r = p["head"] * min(1.0, t * 1.1)
    head_c = head_r + 0.6
    W = p["widen"]
    right = catmull([
        (p["neck"], head_c + head_r + 3.4),
        (p["sh"] * 0.74, ty(17.8)),
        (p["sh"], ty(22.0)),
        (p["ch"], ty(29.0)),
        (p["wa"], ty(38.0)),
        (p["hip"], hip_y),
        (p["kn"], ly(73.0)),
        (p["an"], ly(97.0)),
    ]) + [(p["an"], 100.0)]
    left = [(-x, y) for x, y in reversed(right)]
    column = [(x * W, y) for x, y in left + right]
    column = column + [column[0]]
    legs = [(0.0, ly(56.0)), (0.0, 100.0)]
    return {"head": [arc(0, head_c, head_r)], "body": [column, legs]}


def figure(over: dict | None = None, cx: float = 24, sole: float = 45, height: float = 40) -> list[Poly]:
    """The person placed on the grid: centred on cx, standing on y = sole, `height` units tall."""
    k = height / 100
    parts = figure_parts(over)
    return xf(parts["head"] + parts["body"], s=k, dx=cx, dy=sole - 100 * k)


# ---------------------------------------------------------------------------------------------------------------------
# Small glyphs
# ---------------------------------------------------------------------------------------------------------------------

def brackets(x0: float, y0: float, x1: float, y1: float, n: float = 5.5) -> list[Poly]:
    """Viewfinder corner marks."""
    return [
        [(x0, y0 + n), (x0, y0), (x0 + n, y0)],
        [(x1 - n, y0), (x1, y0), (x1, y0 + n)],
        [(x1, y1 - n), (x1, y1), (x1 - n, y1)],
        [(x0 + n, y1), (x0, y1), (x0, y1 - n)],
    ]


def camera(cx: float, cy: float, rot: float = 0, s: float = 1.0) -> list[Poly]:
    """A small camera seen from the front: body, lens, viewfinder hump. Where it sits says where it looks from."""
    body = rrect(-5.2, -3.4, 5.2, 4.0, 1.3)
    hump = [(-2.4, -3.4), (-1.6, -5.2), (1.6, -5.2), (2.4, -3.4)]
    lens = arc(0, 0.3, 2.2)
    return xf([body, hump, lens], s=s, rot=rot, dx=cx, dy=cy)


FRAME = (6.0, 9.0, 42.0, 39.0)   # the viewfinder used by every framing icon


def framed(polys: list[Poly], frame=FRAME) -> list[Poly]:
    return clip(polys, frame) + brackets(*frame)


def lens_fov(barrel: float, half_angle: float, front: float = 0.0) -> Art:
    """Camera body side-on at the left, a lens `barrel` long, and the field of view opening to the right."""
    a = Art()
    x0, cy = 4.5, 26.0
    a.s([rrect(x0, cy - 6.5, x0 + 9, cy + 6.5, 1.6)])
    a.s([[(x0 + 2.5, cy - 6.5), (x0 + 3.5, cy - 9.0), (x0 + 6.5, cy - 9.0), (x0 + 7.5, cy - 6.5)]])
    lx0, lx1 = x0 + 9, x0 + 9 + barrel
    r0, r1 = 3.6, 3.6 + front
    a.s([[(lx0, cy - r0), (lx1, cy - r1), (lx1, cy + r1), (lx0, cy + r0)]])
    if barrel > 6:
        for gx in (lx0 + barrel * 0.35, lx0 + barrel * 0.55):
            rr = r0 + (r1 - r0) * (gx - lx0) / barrel
            a.s([[(gx, cy - rr), (gx, cy + rr)]], w=THIN)
    tip = (lx1 + 1.0, cy)
    reach = 44.5 - tip[0]
    h = math.tan(math.radians(half_angle)) * reach
    for sgn in (-1, 1):
        a.s(dashed([tip, (44.5, cy + sgn * min(h, 17))], 2.0, 1.6), w=THIN)
    return a


# ---------------------------------------------------------------------------------------------------------------------
# Build — a dress form per body type: one closed outline from the neck to the upper thigh, and nothing else. The
# shape is the whole message (shoulders, chest, waist, hips, thighs, plus overall size for petite/statuesque/stocky).
# Half-widths and the height are in grid units; every form stands on the same base line.
# ---------------------------------------------------------------------------------------------------------------------

def form(sh: float, ch: float, wa: float, hip: float, th: float, h: float = 37.0, neck: float = 3.0) -> Art:
    base = 44.0
    top = base - h
    right = catmull([
        (neck, top),
        (neck, top + 0.045 * h),
        (sh * 0.70, top + 0.095 * h),
        (sh, top + 0.17 * h),
        (ch, top + 0.33 * h),
        (wa, top + 0.55 * h),
        (hip, top + 0.79 * h),
        (th, base),
    ], n=14)
    left = [(-x, y) for x, y in reversed(right)]
    outline = [(24 + x, y) for x, y in right + left]
    return Art().s([outline + [outline[0]]])


BODY = {
    "slim":             lambda: form(9.5, 8.5, 6.3, 9.0, 7.6),
    "slender":          lambda: form(9.0, 7.8, 5.8, 8.2, 7.0),
    "petite":           lambda: form(9.8, 9.0, 7.0, 9.8, 8.4, h=28),
    "athletic":         lambda: form(12.0, 11.0, 8.0, 9.8, 8.8),
    "toned":            lambda: form(11.0, 10.0, 7.2, 9.8, 8.6),
    "fit":              lambda: form(11.2, 10.2, 7.6, 10.0, 8.8),
    "lean":             lambda: form(10.2, 8.6, 6.4, 8.6, 7.4),
    "curvy":            lambda: form(10.6, 11.0, 7.4, 12.8, 11.2),
    "hourglass":        lambda: form(11.0, 11.6, 6.4, 12.0, 10.4),
    "pear-shaped":      lambda: form(9.2, 9.0, 7.4, 13.4, 12.0),
    "apple-shaped":     lambda: form(11.0, 12.0, 12.2, 10.8, 9.4),
    "voluptuous":       lambda: form(11.4, 12.8, 8.4, 13.4, 11.8),
    "full-figured":     lambda: form(12.0, 12.8, 10.4, 13.6, 12.0),
    "plus-size":        lambda: form(12.8, 13.8, 12.6, 14.6, 13.0),
    "soft and round":   lambda: form(11.6, 12.8, 13.4, 13.4, 11.4),
    "muscular":         lambda: form(14.4, 13.2, 9.0, 10.4, 9.6, neck=4.0),
    "broad-shouldered": lambda: form(14.8, 12.2, 8.6, 9.8, 8.8),
    "stocky":           lambda: form(12.8, 12.6, 11.6, 12.0, 11.0, h=30, neck=4.0),
    "average build":    lambda: form(10.5, 9.5, 7.6, 10.4, 9.0),
    "statuesque":       lambda: form(11.0, 10.4, 7.4, 11.2, 9.8, h=41),
    "willowy":          lambda: form(8.8, 7.8, 5.6, 8.0, 6.8, h=41),
    "dancer's build":   lambda: form(9.4, 8.6, 5.8, 8.8, 7.6, h=39),
    "swimmer's build":  lambda: form(14.0, 12.4, 7.8, 8.8, 8.0),
    "gymnast's build":  lambda: form(12.0, 11.2, 8.2, 9.6, 9.0, h=31),
}


# ---------------------------------------------------------------------------------------------------------------------
# Height — the same figure against a fixed ruler.
# ---------------------------------------------------------------------------------------------------------------------

def tall(h: float, legs: float = 1.0) -> Art:
    floor_y = 44.5
    a = Art()
    a.s([[(5.0, floor_y), (43.0, floor_y)]])
    a.s(figure({"legs": legs}, cx=19.5, sole=floor_y, height=h))
    rx = 37.0
    a.s([[(rx, floor_y), (rx, 3.5)]], w=THIN)
    for i, y in enumerate(range(int(floor_y) - 5, 3, -5)):
        a.s([[(rx, y + (floor_y % 1)), (rx + (3.2 if i % 2 else 2.0), y + (floor_y % 1))]], w=THIN)
    top = floor_y - h
    a.s(dashed([(25.0, top), (rx - 1.0, top)], 1.6, 1.5), w=THIN)
    return a


HEIGHT = {
    "petite height":        lambda: tall(25.0),
    "short":                lambda: tall(27.5),
    "below average height": lambda: tall(30.0),
    "average height":       lambda: tall(32.5),
    "above average height": lambda: tall(35.0),
    "tall":                 lambda: tall(37.5),
    "very tall":            lambda: tall(40.5),
    "leggy":                lambda: tall(36.0, legs=1.22),
}


# ---------------------------------------------------------------------------------------------------------------------
# Camera — framings (the figure cropped by a viewfinder), angles (where the camera is), lenses (field of view), and a
# few formats.
# ---------------------------------------------------------------------------------------------------------------------

def crop(top_unit: float, bottom_unit: float, cx: float = 24, frame=FRAME, pad: float = 2.5, over=None) -> Art:
    """The figure scaled so units top_unit..bottom_unit of its 100-unit height fill the frame (head pad above)."""
    x0, y0, x1, y1 = frame
    k = (y1 - y0 - pad) / (bottom_unit - top_unit)
    height = 100 * k
    sole = y0 + pad - top_unit * k + height
    return Art().s(framed(figure(over, cx=cx, sole=sole, height=height), frame))


def head_turn(meridian_rx: float, meridian_side: int) -> Art:
    """A construction head: the sphere, its vertical centre line and brow line bending with the turn."""
    a = Art()
    cx, cy, r = 24.0, 21.0, 12.0
    a.s([arc(cx, cy, r)])
    if meridian_rx <= 0.01:
        a.s([[(cx, cy - r), (cx, cy + r)]], w=THIN)
    else:
        a.s([arc(cx, cy, meridian_rx, r, a0=-90 * meridian_side, a1=90 * meridian_side)], w=THIN)
    a.s([arc(cx, cy, r, r * 0.22, a0=0, a1=180)], w=THIN)
    jx = cx + meridian_side * meridian_rx * 0.45
    a.s([[(jx - 4.4, cy + r - 0.9), (jx - 4.8, 44.0)], [(jx + 4.4, cy + r - 0.9), (jx + 4.8, 44.0)]])
    return a


def profile() -> Art:
    pts = [
        (20.0, 44.0), (20.6, 36.0), (17.0, 31.5), (14.5, 25.0), (14.0, 17.0), (17.5, 9.5), (24.5, 6.0),
        (31.0, 7.6), (34.2, 12.5), (35.0, 17.6), (37.8, 22.4), (35.4, 23.4), (35.8, 25.6), (34.8, 27.0),
        (35.2, 28.6), (33.0, 31.6), (28.4, 32.0), (28.6, 44.0),
    ]
    a = Art().s([catmull(pts)])
    return a


def eye() -> Art:
    a = Art()
    upper = catmull([(9.0, 25.0), (16.0, 17.6), (24.0, 15.8), (32.0, 17.6), (39.0, 24.0)])
    lower = catmull([(9.0, 25.0), (17.0, 29.6), (25.0, 31.0), (33.0, 29.4), (39.0, 24.0)])
    a.s(clip([upper, lower], FRAME))
    a.s([arc(24.0, 23.8, 6.4, a0=-55, a1=235)])
    a.f([arc(24.0, 23.8, 2.6)])
    a.s(brackets(*FRAME))
    return a


def angle_cam(cam: Pt, rot: float, target: Pt, fig=None) -> Art:
    a = Art()
    a.s([[(5.0, 44.5), (43.0, 44.5)]], w=THIN)
    a.s(figure(fig, cx=31.0, sole=44.5, height=31.0))
    a.s(camera(cam[0], cam[1]))
    dx, dy = target[0] - cam[0], target[1] - cam[1]
    n = math.hypot(dx, dy)
    start = (cam[0] + dx / n * 7.0, cam[1] + dy / n * 7.0)
    a.s(dashed([start, target], 1.8, 1.6), w=THIN)
    return a


def foreshorten(top_scale: float, bottom_scale: float, cam_y: float) -> Art:
    """The person as seen from far above or below: the near end swells, the far end shrinks; the camera sits there."""
    top, h = (13.5, 31.0) if cam_y < 24 else (4.0, 30.0)
    f = figure(None, cx=24, sole=top + h, height=h)

    def fn(x: float, y: float) -> Pt:
        t = (y - top) / h
        s = top_scale + (bottom_scale - top_scale) * t
        return (24 + (x - 24) * s, y)
    return Art().s(warp(f, fn)).s(camera(24.0, cam_y, s=0.85))


def clip_out(polys: list[Poly], c: Pt, r: float) -> list[Poly]:
    """Remove the parts of polylines inside a circle (to tuck one shape behind another)."""
    out: list[Poly] = []
    for poly in polys:
        dense: Poly = []
        for a, b in zip(poly, poly[1:]):
            n = max(1, int(math.dist(a, b) / 0.3))
            dense += [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n)]
        dense.append(poly[-1])
        seg: Poly = []
        for p in dense:
            if math.dist(p, c) < r:
                if len(seg) > 1:
                    out.append(seg)
                seg = []
            else:
                seg.append(p)
        if len(seg) > 1:
            out.append(seg)
    return out


def birds_eye() -> Art:
    """A person from straight overhead: the crown of the head over the line of the shoulders."""
    a = Art()
    shoulders = rrect(8.0, 17.0, 40.0, 31.0, 7.0)
    hx, hy, hr = 24.0, 24.0, 5.6
    a.s([p for p in clip_out([shoulders], (hx, hy), hr + 1.8)])
    a.s([arc(hx, hy, hr)])
    a.s(brackets(*FRAME))
    return a


def over_shoulder() -> Art:
    """The back of someone's head and shoulder in the near left corner; the person they face, small, at the right."""
    a = Art()
    near_head = arc(15.0, 25.0, 8.0)
    near_body = catmull([(2.0, 37.0), (8.0, 34.4), (16.0, 34.0), (24.0, 37.0), (27.0, 41.0)])
    a.s(framed([near_head, near_body]))
    a.s(clip(figure(None, cx=32.5, sole=52.0, height=34.0), FRAME))
    return a


def dutch() -> Art:
    f = figure(None, cx=24.0, sole=78.0, height=68.0)
    horizon = [[(0.0, 31.0), (48.0, 31.0)]]
    tilted = xf(f + horizon, rot=-14, about=(24.0, 24.0))
    return Art().s(framed(tilted))


def phone_snap() -> Art:
    a = Art()
    a.s([rrect(14.0, 4.5, 34.0, 43.5, 3.6)])
    a.s([[(21.5, 7.6), (26.5, 7.6)]], w=THIN)
    a.s(clip(figure(None, cx=24.0, sole=50.0, height=36.0), (15.5, 10.5, 32.5, 36.5)))
    a.s([arc(24.0, 39.8, 1.9)])
    return a


def selfie() -> Art:
    a = Art()
    a.s([arc(17.0, 23.0, 6.6)])
    a.s([catmull([(4.0, 44.0), (6.0, 35.6), (11.0, 32.6), (17.0, 32.0), (23.0, 32.6), (27.0, 34.4)])])
    a.s([catmull([(27.0, 34.4), (31.0, 26.0), (33.6, 17.0)])])     # the raised arm
    a.s(xf([rrect(-4.2, -7.0, 4.2, 7.0, 1.8)], rot=14, dx=36.0, dy=12.0))
    return a


def pov() -> Art:
    """Your own two hands at the bottom of the picture, reaching in."""
    a = Art()
    a.s([[(4.0, 20.0), (44.0, 20.0)]], w=THIN)
    for side in (-1, 1):
        local = [rrect(-3.8, -5.6, 3.8, 5.6, 3.6), [(-3.0, 5.2), (-3.0, 30.0)], [(3.0, 5.2), (3.0, 30.0)]]
        parts = xf(local, rot=-side * 26, dx=24 + side * 8.0, dy=29.0)
        a.s([parts[0]])
        a.s(clip(parts[1:], (0, 0, 48, 46.5)))
    return a


def fisheye() -> Art:
    """A grid bowed outward: barrel distortion inside the viewfinder."""
    a = Art()
    cx, cy = 24.0, 24.0
    hw, hh, bulge = 15.0, 12.0, 0.22

    def bow(u: float, v: float) -> Pt:    # u, v in -1..1 -> bowed point
        r2 = u * u * 0.6 + v * v * 0.6
        k = 1 + bulge * (1 - r2)
        return (cx + u * hw * k, cy + v * hh * k)

    for i, t in enumerate((-1.0, -0.5, 0.0, 0.5, 1.0)):
        w = STROKE if abs(t) == 1.0 else THIN
        a.s([[bow(t, -1 + 2 * j / 30) for j in range(31)]], w=w)
        a.s([[bow(-1 + 2 * j / 30, t) for j in range(31)]], w=w)
    return a


def film() -> Art:
    a = Art()
    a.s([rrect(4.0, 11.0, 44.0, 37.0, 1.6)])
    for x in range(7, 44, 5):
        a.s([rrect(x - 1.2, 13.0, x + 1.2, 15.6, 0.5), rrect(x - 1.2, 32.4, x + 1.2, 35.0, 0.5)], w=THIN)
    a.s([[(4.0, 17.6), (44.0, 17.6)], [(4.0, 30.4), (44.0, 30.4)]], w=THIN)
    for x0 in (8.0, 26.0):
        a.s([rrect(x0, 19.6, x0 + 14.0, 28.4, 0.8)])
    return a


def macro() -> Art:
    a = Art()
    fc = (19.0, 19.0)
    for i in range(5):
        ang = -90 + i * 72
        a.s([arc(fc[0] + 4.4 * math.cos(math.radians(ang)), fc[1] + 4.4 * math.sin(math.radians(ang)),
                 3.4, 2.2, rot=ang)], w=THIN)
    a.s([arc(fc[0], fc[1], 1.8)], w=THIN)
    a.s([arc(fc[0], fc[1], 12.0)])
    a.s([[(27.6, 27.6), (40.0, 40.0)]], w=STROKE * 1.8)
    return a


CAMERA = {
    "face close-up":         lambda: crop(0.0, 23.0),
    "extreme close-up":      eye,
    "waist up":              lambda: crop(0.0, 42.0),
    "full body":             lambda: Art().s(framed(figure(None, cx=24, sole=37.0, height=26.0))),
    "cowboy shot":           lambda: crop(0.0, 62.0),
    "wide shot":             lambda: Art().s(framed(
                                 figure(None, cx=30.0, sole=33.0, height=10.5)
                                 + [catmull([(4.0, 33.0), (14.0, 29.6), (22.0, 33.0)]), [(4.0, 33.0), (44.0, 33.0)]])),
    "low angle":             lambda: angle_cam((10.0, 38.0), 0, (28.6, 17.0)),
    "high angle":            lambda: angle_cam((10.0, 9.0), 0, (28.4, 24.0)),
    "over-the-shoulder":     over_shoulder,
    "Dutch angle":           dutch,
    "bird's eye":            birds_eye,
    "from above":            lambda: foreshorten(1.5, 0.5, 6.0),
    "from below":            lambda: foreshorten(0.5, 1.5, 42.0),
    "85mm portrait lens":    lambda: lens_fov(9.0, 14.0, 0.8),
    "50mm lens":             lambda: lens_fov(5.0, 24.0, 0.4),
    "35mm film":             film,
    "telephoto compression": lambda: lens_fov(17.0, 6.0, 1.6),
    "macro detail":          macro,
    "phone snapshot":        phone_snap,
    "selfie":                selfie,
    "POV first-person":      pov,
    "three-quarter view":    lambda: head_turn(6.6, 1),
    "profile view":          profile,
    "fisheye":               fisheye,
}


GROUPS: dict[str, dict] = {
    "body": BODY,
    "height": HEIGHT,
    "camera": CAMERA,
}


# ---------------------------------------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------------------------------------

def slug(option: str) -> str:
    s = option.lower().replace("'", "")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def asset_name(group: str, option: str) -> str:
    return f"pg_{group}_{slug(option)}"


def render_all() -> dict[str, dict[str, tuple[str, str]]]:
    """{group: {option: (asset name, svg)}}; refuses two options that would share an asset name."""
    out: dict[str, dict[str, tuple[str, str]]] = {}
    seen: dict[str, str] = {}
    for g, drawings in GROUPS.items():
        out[g] = {}
        for option, draw in drawings.items():
            name = asset_name(g, option)
            if name in seen:
                sys.exit(f"two options map to {name}: {seen[name]!r} and {option!r}")
            seen[name] = option
            out[g][option] = (name, draw().svg())
    return out


def write_assets(rendered) -> int:
    if ASSET_DIR.exists():
        shutil.rmtree(ASSET_DIR)
    ASSET_DIR.mkdir(parents=True)
    (ASSET_DIR / "Contents.json").write_text(json.dumps(
        {"info": {"author": "xcode", "version": 1}, "properties": {"provides-namespace": False}}, indent=2) + "\n",
        encoding="utf-8")
    n = 0
    for g, items in rendered.items():
        for option, (name, svg) in items.items():
            d = ASSET_DIR / f"{name}.imageset"
            d.mkdir()
            (d / f"{name}.svg").write_text(svg, encoding="utf-8")
            (d / "Contents.json").write_text(json.dumps({
                "images": [{"filename": f"{name}.svg", "idiom": "universal"}],
                "info": {"author": "xcode", "version": 1},
                "properties": {"preserves-vector-representation": True, "template-rendering-intent": "template"},
            }, indent=2) + "\n", encoding="utf-8")
            n += 1
    return n


def write_table(rendered) -> None:
    lines = [
        "// GENERATED by tools/pictographs.py — do not edit by hand. Change the drawings there and re-run it.",
        "// Option keys are the catalogue's option TEXT (matched case-insensitively by `Pictograph.asset`); values",
        "// are the template images in Assets.xcassets/Pictographs.",
        "",
        "extension Pictograph {",
        "    static let table: [String: [String: String]] = [",
    ]
    for g, items in rendered.items():
        lines.append(f"        {json.dumps(g)}: [")
        for option, (name, _) in items.items():
            lines.append(f"            {json.dumps(option)}: {json.dumps(name)},")
        lines.append("        ],")
    lines += ["    ]", "}", ""]
    TABLE_FILE.write_text("\n".join(lines), encoding="utf-8")


def catalogue() -> dict[str, list[str]] | None:
    for f in (FIXER_HOME / "recipes.json", FIXER_HOME / "recipes.example.json"):
        if f.is_file():
            data = json.loads(f.read_text(encoding="utf-8-sig"))
            groups = (data.get("quickselect") or {}).get("groups") or []
            return {g["id"]: (g.get("options") or []) + (g.get("explicit_options") or []) for g in groups if "id" in g}
    return None


def check() -> bool:
    cat = catalogue()
    if cat is None:
        print(f"no recipes.json under {FIXER_HOME}; skipped the catalogue check")
        return True
    ok = True
    for g, drawings in GROUPS.items():
        options = cat.get(g)
        if options is None:
            print(f"[{g}] not in the catalogue at all")
            ok = False
            continue
        lower = {o.lower() for o in options}
        missing = [o for o in options if o.lower() not in {k.lower() for k in drawings}]
        stale = [k for k in drawings if k.lower() not in lower]
        if missing:
            print(f"[{g}] no drawing yet (shows as a label-only tile): {missing}")
        if stale:
            print(f"[{g}] drawn but not in the catalogue: {stale}")
            ok = False
        if not missing and not stale:
            print(f"[{g}] {len(drawings)} drawings, all match the catalogue")
    return ok


def write_preview(rendered, path: Path) -> None:
    """A contact sheet: each icon at tile size in light and dark, plus 2x for detail."""
    cards = []
    for g, items in rendered.items():
        cards.append(f"<h2>{html.escape(g)}</h2><div class=grid>")
        for option, (name, svg) in items.items():
            inner = svg.replace('stroke="#000"', 'stroke="currentColor"').replace('fill="#000"', 'fill="currentColor"')
            small = inner.replace('width="48" height="48"', 'width="44" height="44"')
            big = inner.replace('width="48" height="48"', 'width="96" height="96"')
            cards.append(
                f'<div class=card><div class=row><span class="t light">{small}</span>'
                f'<span class="t dark">{small}</span><span class="t sel">{small}</span></div>'
                f'<div class=big>{big}</div><div class=l>{html.escape(option)}</div></div>')
        cards.append("</div>")
    path.write_text(
        "<!doctype html><meta charset=utf-8><title>Pictographs</title><style>"
        "body{font:13px -apple-system,Segoe UI,sans-serif;background:#f2f2f7;color:#111;margin:16px}"
        "h2{margin:24px 0 8px;text-transform:capitalize}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}"
        ".card{background:#fff;border-radius:12px;padding:8px;text-align:center}"
        ".row{display:flex;gap:4px;justify-content:center}.t{display:inline-flex;padding:4px;border-radius:10px}"
        ".light{background:#f2f2f7;color:#1c1c1e}.dark{background:#2c2c2e;color:#f2f2f7}"
        ".sel{background:#dfe8ff;color:#2a5bd7;outline:1px solid #2a5bd7}"
        ".big{color:#1c1c1e;margin-top:4px}.l{margin-top:2px;color:#555}"
        "</style>" + "".join(cards), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preview", type=Path, help="also write an HTML contact sheet here")
    ap.add_argument("--check", action="store_true", help="only compare the drawings with the catalogue")
    ap.add_argument("--no-write", action="store_true", help="do not touch the app's assets or table")
    args = ap.parse_args()
    ok = check()
    if args.check:
        sys.exit(0 if ok else 1)
    rendered = render_all()
    if args.preview:
        write_preview(rendered, args.preview)
        print(f"preview: {args.preview}")
    if not args.no_write:
        n = write_assets(rendered)
        write_table(rendered)
        print(f"wrote {n} imagesets to {ASSET_DIR.relative_to(REPO)} and {TABLE_FILE.relative_to(REPO)}")


if __name__ == "__main__":
    main()
