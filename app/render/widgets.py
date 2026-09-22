"""Small drawing primitives for a 128x64 canvas.

Everything is drawn with SetPixel/DrawLine/DrawText so it behaves identically on
the emulator and on the real panels.
"""

from ..matrix import graphics, text_width

WHITE = (255, 255, 255)
DIM = (70, 70, 70)
# For text that is secondary but still has to be READ -- records, play detail.
# DIM is for furniture (empty pips, inactive dots): on a real panel it turns
# thin glyph strokes into a grey smudge, which is fine for a dot and useless
# for "12-4". Anything a person is meant to read gets this instead.
MUTED = (175, 175, 175)
YELLOW = (255, 200, 0)
GREEN = (0, 220, 60)
RED = (230, 40, 40)


def c(rgb):
    return graphics.Color(*rgb)


def text(canvas, font, x, y, rgb, s):
    """y is the BASELINE, not the top -- BDF convention. Returns width drawn."""
    return graphics.DrawText(canvas, font, x, y, c(rgb), s)


def text_right(canvas, font, right_x, y, rgb, s):
    """Draw so the string ENDS at right_x. Scores change width; anchoring the
    right edge keeps them from drifting when a team goes from 9 to 10."""
    return graphics.DrawText(canvas, font, right_x - text_width(font, s), y, c(rgb), s)


def text_center(canvas, font, cx, y, rgb, s):
    return graphics.DrawText(canvas, font, cx - text_width(font, s) // 2, y, c(rgb), s)



def hline(canvas, x0, x1, y, rgb):
    graphics.DrawLine(canvas, x0, y, x1, y, c(rgb))


def vline(canvas, x, y0, y1, rgb):
    graphics.DrawLine(canvas, x, y0, x, y1, c(rgb))


def rect(canvas, x, y, w, h, rgb, fill=False):
    if fill:
        for yy in range(y, y + h):
            graphics.DrawLine(canvas, x, yy, x + w - 1, yy, c(rgb))
    else:
        hline(canvas, x, x + w - 1, y, rgb)
        hline(canvas, x, x + w - 1, y + h - 1, rgb)
        vline(canvas, x, y, y + h - 1, rgb)
        vline(canvas, x + w - 1, y, y + h - 1, rgb)


def dot(canvas, x, y, rgb, filled=True):
    """A 3x3 indicator: filled square when on, hollow ring when off."""
    col = c(rgb)
    if filled:
        for dy in range(3):
            graphics.DrawLine(canvas, x, y + dy, x + 2, y + dy, col)
    else:
        for dx, dy in ((1, 0), (0, 1), (2, 1), (1, 2)):
            canvas.SetPixel(x + dx, y + dy, *rgb)


def dot_row(canvas, x, y, count, total, rgb_on, rgb_off=DIM, gap=4):
    for i in range(total):
        dot(canvas, x + i * gap, y, rgb_on if i < count else rgb_off, filled=i < count)


def diamond(canvas, cx, cy, size, rgb, filled):
    """One base: a diamond centered on (cx, cy). size is the half-diagonal."""
    for dy in range(-size, size + 1):
        span = size - abs(dy)
        if filled:
            for dx in range(-span, span + 1):
                canvas.SetPixel(cx + dx, cy + dy, *rgb)
        else:
            canvas.SetPixel(cx - span, cy + dy, *rgb)
            canvas.SetPixel(cx + span, cy + dy, *rgb)


def bases(canvas, cx, cy, occupied, rgb_on=YELLOW, rgb_off=DIM, size=3, spread=6):
    """Standard diamond: 2nd on top, 3rd left, 1st right. occupied = [1st, 2nd, 3rd]."""
    on1, on2, on3 = occupied
    diamond(canvas, cx, cy - spread, size, rgb_on if on2 else rgb_off, on2)
    diamond(canvas, cx - spread, cy, size, rgb_on if on3 else rgb_off, on3)
    diamond(canvas, cx + spread, cy, size, rgb_on if on1 else rgb_off, on1)


def triangle(canvas, cx, y, size, rgb, up=True):
    """Inning half indicator."""
    for i in range(size):
        row = y + i if up else y + size - 1 - i
        half = i
        graphics.DrawLine(canvas, cx - half, row, cx + half, row, c(rgb))


def progress(canvas, x, y, w, h, frac, rgb_on=GREEN, rgb_off=DIM):
    rect(canvas, x, y, w, h, rgb_off)
    fill_w = max(0, min(w - 2, round((w - 2) * frac)))
    if fill_w:
        rect(canvas, x + 1, y + 1, fill_w, h - 2, rgb_on, fill=True)


def fit_words(f, text: str, width: int) -> str:
    """`text` trimmed to `width`, on a word boundary wherever possible.

    A plain character trim cuts names mid-word -- "SUNDAY FUNDAY SI", "THE
    OTHER LE" -- which reads as a rendering bug rather than an abbreviation.
    Whole words are dropped from the end first; only a single word too long on
    its own is cut by characters.
    """
    text = text or ""
    if text_width(f, text) <= width:
        return text
    words = text.split()
    while len(words) > 1:
        words.pop()
        candidate = " ".join(words)
        if text_width(f, candidate) <= width:
            return candidate
    while text and text_width(f, text) > width:
        text = text[:-1]
    return text
