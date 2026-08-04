#!/usr/bin/env python3
"""Rebuild assets/tux_saving.png from the original sprite sheet.

The source is a 8x4 grid of JPEG cells on white with grey rules between
them, plus a blue banner underneath that is not part of the animation.
Three things make this more than a crop:

  * The background cannot be keyed on colour. The penguin's belly and its
    placard are both white, so a global key punches holes in them. The fill
    starts at the cell border and stops at the outline instead.
  * The grid rules are mid-grey: too dark to flood, too light to be
    outline. They survive as hairlines down the edge of a frame and, being
    opaque, drag the bounding box out with them.
  * Rows differ in height by a few pixels, so frames are stood on a common
    floor or the penguin bobs between rows.

Usage: extract_tux.py SOURCE.jpeg [OUT.png]
"""
import sys
from collections import deque
from PIL import Image

ROWS = [(3, 163), (167, 325), (327, 493), (495, 656)]   # content, sans banner
COLS = [(c * 128, (c + 1) * 128 - 1) for c in range(8)]
INSET, RING = 3, 5


def cut(im, x0, x1, y0, y1):
    c = im.crop((x0 + INSET, y0 + INSET, x1 - INSET + 1, y1 - INSET + 1))
    c = c.convert("RGBA")
    w, h = c.size
    p = c.load()
    for y in range(h):
        for x in range(w):
            if RING <= x < w - RING and RING <= y < h - RING:
                continue
            r, g, b, _ = p[x, y]
            if max(r, g, b) - min(r, g, b) < 34 and min(r, g, b) > 118:
                p[x, y] = (255, 255, 255, 255)

    def light(t):
        return min(t[0], t[1], t[2]) > 196

    seen = [[False] * w for _ in range(h)]
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if light(p[x, y]) and not seen[y][x]:
                seen[y][x] = True
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if light(p[x, y]) and not seen[y][x]:
                seen[y][x] = True
                q.append((x, y))
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not seen[ny][nx] and light(p[nx, ny]):
                seen[ny][nx] = True
                q.append((nx, ny))
    # Feather the anti-aliased pixel at the outline, or a white halo shows
    # up against the dark overlay the sprite is drawn on.
    for y in range(h):
        for x in range(w):
            r, g, b, _ = p[x, y]
            if seen[y][x]:
                p[x, y] = (r, g, b, 0)
                continue
            if any(0 <= x + dx < w and 0 <= y + dy < h and seen[y + dy][x + dx]
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                mn = min(r, g, b)
                if mn > 168:
                    p[x, y] = (r, g, b, max(0, min(255, int((255 - mn) * 255 / 87))))
    return c


def union_bbox(frames):
    bb = None
    for f in frames:
        b = f.getbbox()
        if b:
            bb = b if bb is None else (min(bb[0], b[0]), min(bb[1], b[1]),
                                       max(bb[2], b[2]), max(bb[3], b[3]))
    return bb


def main(src, out):
    im = Image.open(src).convert("RGB")
    raw = [cut(im, x0, x1, y0, y1) for (y0, y1) in ROWS for (x0, x1) in COLS]

    cw = max(f.size[0] for f in raw)
    ch = max(f.size[1] for f in raw)
    frames = []
    for f in raw:
        canv = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        canv.paste(f, ((cw - f.size[0]) // 2, ch - f.size[1]))
        frames.append(canv)

    # Anything still opaque nearly top to bottom at an edge is a rule: these
    # characters are rounded and centred, so their own art never is.
    #
    # Clearing and cropping have to alternate until they settle. One frame's
    # rule holds the shared bounding box open, and only once that box tightens
    # does the next frame's rule become an edge column at all.
    while True:
        for f in frames:
            p = f.load()
            w, h = f.size
            for xs in (range(8), range(w - 1, w - 9, -1)):
                for x in xs:
                    frac = sum(1 for y in range(h) if p[x, y][3] > 25) / h
                    if frac < 0.05:
                        continue          # empty margin, the rule may be behind it
                    if frac <= 0.45:
                        break             # real art, stop cutting
                    for y in range(h):
                        r, g, b, _ = p[x, y]
                        p[x, y] = (r, g, b, 0)
        bb = union_bbox(frames)   # one box for all, or the penguin jumps
        cropped = [f.crop(bb) for f in frames]
        if cropped[0].size == frames[0].size:
            frames = cropped
            break
        frames = cropped
    fw, fh = frames[0].size
    sheet = Image.new("RGBA", (fw * len(frames), fh), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f, (i * fw, 0))
    sheet.save(out)
    print(f"{len(frames)} frames of {fw}x{fh} -> {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "assets/tux_saving.png")
