#!/usr/bin/env python3
"""Build the save-indicator sprite sheet from the Club Penguin dance frames.

Three things have to happen and none of them is a crop.

The body is a flat cyan, so recolouring is a hue selection rather than a
redraw. Selecting on raw channel values leaves a blue rim wherever the body
meets the background, because those pixels are half cyan and half white; the
mask ramps on saturation instead so they blend.

The background is near-white and so is the belly, so it cannot be keyed on
colour. The fill starts at the border and stops at the outline.

Shading is kept by mapping luminance into a narrow dark range rather than
flat-filling. A flat fill loses the fold down a raised flipper and the
shadow under a crossed arm, which is most of what makes the frames read as
a body rather than a silhouette.

Usage:
    make_penguin_sheet.py FRAME_DIR [-o assets/penguin_saving.png]
"""
import argparse
import pathlib
import sys
from collections import deque

import numpy as np
from PIL import Image

COLS = 12                 # grid, so the sheet is not 17000px wide
HUE_LO, HUE_HI = 168, 220     # the body's cyan
SAT_FLOOR, SAT_RAMP = 0.06, 0.22
DARK_BASE, DARK_RANGE = 0.10, 0.20    # final luminance range of the body


def hue_sat(a):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(axis=2), a.min(axis=2)
    df = mx - mn
    safe = np.where(df == 0, 1, df)
    hue = np.zeros_like(mx)
    m = df > 1e-6
    i = (mx == r) & m
    hue[i] = (60 * ((g - b) / safe))[i] % 360
    i = (mx == g) & m
    hue[i] = (60 * ((b - r) / safe) + 120)[i]
    i = (mx == b) & m
    hue[i] = (60 * ((r - g) / safe) + 240)[i]
    sat = np.where(mx > 0, df / np.where(mx == 0, 1, mx), 0)
    return hue, sat


def recolour(a):
    """Cyan body to dark, everything else untouched."""
    hue, sat = hue_sat(a)
    alpha = np.clip((sat - SAT_FLOOR) / SAT_RAMP, 0, 1) * ((hue > HUE_LO) & (hue < HUE_HI))
    lum = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    shade = DARK_BASE + lum * DARK_RANGE
    out = a.copy()
    for c in range(3):
        out[..., c] = a[..., c] * (1 - alpha) + shade * alpha
    return out


def key_background(rgb):
    """Alpha from a border flood fill, so the white belly survives."""
    h, w = rgb.shape[:2]
    light = rgb.min(axis=2) > 0.80
    seen = np.zeros((h, w), bool)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if light[y, x] and not seen[y, x]:
                seen[y, x] = True
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if light[y, x] and not seen[y, x]:
                seen[y, x] = True
                q.append((x, y))
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not seen[ny, nx] and light[ny, nx]:
                seen[ny, nx] = True
                q.append((nx, ny))
    alpha = np.where(seen, 0.0, 1.0)
    # Feather the one anti-aliased pixel at the outline, or a white halo
    # shows against the dark chip it is drawn on.
    edge = np.zeros_like(alpha, bool)
    edge[1:, :] |= seen[:-1, :]
    edge[:-1, :] |= seen[1:, :]
    edge[:, 1:] |= seen[:, :-1]
    edge[:, :-1] |= seen[:, 1:]
    soft = edge & (~seen)
    mn = rgb.min(axis=2)
    partial = np.clip((0.96 - mn) / 0.22, 0, 1)
    alpha = np.where(soft, np.minimum(alpha, partial), alpha)
    return alpha


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames")
    ap.add_argument("-o", "--out", default="assets/penguin_saving.png")
    ap.add_argument("--cell-height", type=int, default=120,
                    help="frames are stored at this height; the chip "
                         "draws them at about 40px, and full 465px "
                         "cells made a 5760x5580 sheet for nothing")
    args = ap.parse_args()

    files = sorted(pathlib.Path(args.frames).glob("*.png"))
    if not files:
        sys.exit("no frames found")
    cells = []
    for f in files:
        a = np.asarray(Image.open(f).convert("RGB")).astype(float) / 255.0
        rgb = recolour(a)
        alpha = key_background(rgb)
        rgba = np.dstack([rgb, alpha])
        cells.append(Image.fromarray((np.clip(rgba, 0, 1) * 255).astype(np.uint8), "RGBA"))

    # One box for every frame, or the penguin jumps around between them.
    bb = None
    for c in cells:
        b = c.getbbox()
        if b:
            bb = b if bb is None else (min(bb[0], b[0]), min(bb[1], b[1]),
                                       max(bb[2], b[2]), max(bb[3], b[3]))
    cells = [c.crop(bb) for c in cells]
    cw, ch = cells[0].size
    if args.cell_height and ch > args.cell_height:
        scale = args.cell_height / ch
        cw, ch = max(1, round(cw * scale)), args.cell_height
        cells = [c.resize((cw, ch), Image.LANCZOS) for c in cells]
    rows = (len(cells) + COLS - 1) // COLS
    sheet = Image.new("RGBA", (COLS * cw, rows * ch), (0, 0, 0, 0))
    for i, c in enumerate(cells):
        sheet.paste(c, ((i % COLS) * cw, (i // COLS) * ch))
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"{len(cells)} frames of {cw}x{ch} in a {COLS}x{rows} grid -> {out}")
    print(f"sheet {sheet.size[0]}x{sheet.size[1]}")


if __name__ == "__main__":
    main()
