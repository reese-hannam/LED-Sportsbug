"""Text at any size, by scaling the BDF glyphs up.

The panel is 128x64 and the biggest font this project ships is 10x20, so
"fill the screen" is not something the normal text path can do -- a clock in
9x15B uses a fifth of the height and looks like a caption rather than a
display.

So this reads the BDF files directly and draws the glyph bitmaps itself,
optionally at 2x or 3x. Two things fall out of that which matter:

  * It only ever calls canvas.SetPixel. graphics.DrawText would have been
    less code, but on the real Pi that's a C function that wants a real
    Canvas -- it can't be pointed at a scratch buffer to capture glyphs. This
    works identically on the emulator and the hardware, which is the whole
    rule app/matrix.py exists to keep.
  * Having the bitmaps means the true INK box of a string is knowable, so
    text can be centred on what you actually see rather than on the font's
    nominal box. At these sizes that's the difference between centred and
    visibly-a-bit-high: the nominal box carries descender room that "5:38 PM"
    never uses.

Parsing is done once per font and cached; drawing is a flat list of filled
blocks, the same shape of work as render/logos.py.
"""

import os
from functools import lru_cache

from ..matrix import FONT_DIR

# name -> {"glyphs": {codepoint: (dwidth, bx, by, bw, bh, rows)}, "ascent", "descent"}
_fonts: dict = {}

# Widest first: what fill() walks looking for the biggest thing that fits.
LADDER = ("10x20", "9x15B", "7x13B", "6x10", "5x7", "4x6")
SCALES = (4, 3, 2, 1)

# Fonts whose W and M lose their middle vertex and read as H and N. In 7x13B
# the W is literally an H with a two-row crossbar:
#
#     7x13B  ##..##        9x15B  ##.##.##
#            ######  <- W         ########   <- vertex still visible
#            ######     as H      ###..###
#            ##..##               ##....##
#
# This project has been here before, with 6x13B turning MIN into HIN. It
# surfaced again on "NO WIFI", which is the single most important thing the
# panel ever displays -- it came out as "NO HIFI". Scaling up doesn't help:
# every pixel just gets bigger, crossbar included.
VERTEX_BLIND = ("7x13B", "5x7", "4x6")
SAFE_LADDER = tuple(name for name in LADDER if name not in VERTEX_BLIND)


def ladder_for(text: str, ladder=LADDER):
    """The fonts safe to set `text` in.

    Only narrows when it has to. Text without a W or an M can use everything,
    so the common case keeps the largest possible type; text with one drops
    the fonts that would garble it and comes out slightly smaller and
    actually readable, which is the trade this project has always made.
    """
    if ladder is not LADDER:
        return ladder
    return SAFE_LADDER if set("WM") & set(text.upper()) else LADDER


def _parse(name: str) -> dict:
    path = os.path.join(FONT_DIR, f"{name}.bdf")
    glyphs: dict = {}
    ascent = descent = None
    code = dwidth = None
    bbx = None
    rows: list[int] = []
    reading = False

    with open(path, encoding="latin-1") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            key = parts[0]
            if key == "FONT_ASCENT":
                ascent = int(parts[1])
            elif key == "FONT_DESCENT":
                descent = int(parts[1])
            elif key == "ENCODING":
                code = int(parts[1])
            elif key == "DWIDTH":
                dwidth = int(parts[1])
            elif key == "BBX":
                bbx = tuple(int(p) for p in parts[1:5])   # w h xoff yoff
            elif key == "BITMAP":
                rows, reading = [], True
            elif key == "ENDCHAR":
                reading = False
                if code is not None and bbx is not None:
                    bw, bh, bx, by = bbx
                    glyphs[code] = (dwidth if dwidth is not None else bw,
                                    bx, by, bw, bh, tuple(rows))
                code = dwidth = bbx = None
            elif reading:
                # One row of the glyph, MSB-first, padded to whole bytes.
                rows.append(int(key, 16) if key else 0)

    return {"glyphs": glyphs, "ascent": ascent or 0, "descent": descent or 0}


def _font(name: str) -> dict:
    if name not in _fonts:
        _fonts[name] = _parse(name)
    return _fonts[name]


def width(name: str, text: str, scale: int = 1) -> int:
    g = _font(name)["glyphs"]
    return sum(g[ord(c)][0] for c in text if ord(c) in g) * scale


@lru_cache(maxsize=512)
def _pixels(name: str, text: str):
    """(x, y) ink for `text`, with x from the pen start and y from the
    baseline (negative above it). Unscaled.

    Cached, and that matters more than it looks: the render loop redraws at
    30fps but a clock's text only changes once a second, so without this the
    same glyph bitmaps were being walked thirty times over to produce an
    identical answer. On a Pi Zero that was the difference between comfortably
    inside the frame budget and missing it -- see the note on best_fit.
    """
    glyphs = _font(name)["glyphs"]
    out = []
    pen = 0
    for ch in text:
        entry = glyphs.get(ord(ch))
        if entry is None:
            continue
        dwidth, bx, by, bw, bh, rows = entry
        # Rows are padded to whole bytes, so the top bit of the row sits at
        # bit (pad-1) counting from the low end.
        pad = ((bw + 7) // 8) * 8
        for j, bits in enumerate(rows[:bh]):
            if not bits:
                continue
            y = -by - bh + j
            for i in range(bw):
                if bits & (1 << (pad - 1 - i)):
                    out.append((pen + bx + i, y))
        pen += dwidth
    return tuple(out)


@lru_cache(maxsize=512)
def ink_box(name: str, text: str, scale: int = 1):
    """(x0, y0, x1, y1) of the ink itself, or None for a blank string."""
    xs0 = ys0 = 10**6
    xs1 = ys1 = -10**6
    for x, y in _pixels(name, text):
        xs0, ys0 = min(xs0, x), min(ys0, y)
        xs1, ys1 = max(xs1, x), max(ys1, y)
    if xs1 < xs0:
        return None
    return xs0 * scale, ys0 * scale, (xs1 + 1) * scale - 1, (ys1 + 1) * scale - 1


def draw(canvas, name: str, x: int, y: int, rgb, text: str, scale: int = 1) -> None:
    """`y` is the baseline, matching widgets.text."""
    r, g, b = rgb
    if scale <= 1:
        for px, py in _pixels(name, text):
            canvas.SetPixel(x + px, y + py, r, g, b)
        return
    for px, py in _pixels(name, text):
        bx, by = x + px * scale, y + py * scale
        for dy in range(scale):
            for dx in range(scale):
                canvas.SetPixel(bx + dx, by + dy, r, g, b)


def draw_centered(canvas, name: str, text: str, cx: int, cy: int, rgb,
                  scale: int = 1) -> None:
    """Centre the INK on (cx, cy) -- see the note about descender room."""
    box = ink_box(name, text, scale)
    if box is None:
        return
    x0, y0, x1, y1 = box
    draw(canvas, name, cx - (x0 + x1) // 2, cy - (y0 + y1) // 2, rgb, text, scale)


@lru_cache(maxsize=512)
def best_fit(text: str, max_w: int, max_h: int, ladder=LADDER, scales=SCALES):
    """The biggest, least blocky (font, scale) whose ink fits max_w x max_h.

    Ranked by the height actually achieved, and only then by how detailed the
    letterforms are. Scanning scale-first instead picked 4x6 at 4x for a clock
    -- 16 pixels tall, drawn in 4x4 blocks -- over 9x15B at 2x, which is both
    TALLER (20px) and four times finer. Same screen space, far better glyphs.

    Cached because it is pure and it is not cheap: it measures every font at
    every scale, so an uncached call walks the glyph bitmaps a couple of dozen
    times. At 30fps for a string that changes once a second, that was the
    single most expensive thing the lifestyle screens did.
    """
    ladder = ladder_for(text, ladder)
    best = None
    for detail, name in enumerate(ladder):
        for scale in scales:
            box = ink_box(name, text, scale)
            if box is None:
                continue
            x0, y0, x1, y1 = box
            w, h = x1 - x0 + 1, y1 - y0 + 1
            if w > max_w or h > max_h:
                continue
            rank = (h, -detail)
            if best is None or rank > best[0]:
                best = (rank, name, scale)
    return (best[1], best[2]) if best else (ladder[-1], 1)


def fill(canvas, text: str, box, rgb, ladder=LADDER, scales=SCALES):
    """Draw `text` as large as it will go inside (x, y, w, h), centred.

    If it won't fit even at the smallest size -- a very long countdown label,
    say -- it is TRIMMED rather than drawn anyway. Centred text that overflows
    runs off both edges at once, losing its start as well as its end, so it
    reads as neither truncated nor complete, just broken.
    """
    x, y, w, h = box
    if not text or w <= 0 or h <= 0:
        return None
    name, scale = best_fit(text, w, h, ladder, scales)
    box_ink = ink_box(name, text, scale)
    while box_ink is not None and (box_ink[2] - box_ink[0] + 1) > w and len(text) > 1:
        text = text[:-1]
        box_ink = ink_box(name, text, scale)
    draw_centered(canvas, name, text, x + w // 2, y + h // 2, rgb, scale)
    return name, scale
