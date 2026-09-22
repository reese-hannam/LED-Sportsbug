"""A typed message, set as large as it will go.

Wrapping and sizing are decided together: for every font and scale the panel
could use, the text is wrapped at that size and the result kept only if the
whole block fits the box. The biggest survivor wins, so a short message comes
out enormous and a long one steps down gracefully instead of everything being
locked to one font.
"""

from . import bigtext
from .lifestyle_theme import Theme

# How much of a line's height to leave between lines.
_LEADING = 0.25


def _wrap(name, text, max_w, scale):
    """`text` broken to fit `max_w`, splitting a word that can't fit alone.

    That last part isn't hypothetical padding: these lines are CENTRED, so a
    word wider than the panel runs off both edges at once and loses its start
    as well as its end.
    """
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if bigtext.width(name, trial, scale) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if bigtext.width(name, word, scale) <= max_w:
            cur = word
            continue
        for ch in word:
            if cur and bigtext.width(name, cur + ch, scale) > max_w:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
    if cur:
        lines.append(cur)
    return lines


def _line_height(name, scale):
    f = bigtext._font(name)
    return (f["ascent"] + f["descent"]) * scale


def layout(text, box, ladder=bigtext.LADDER, scales=bigtext.SCALES):
    """(font, scale, lines) -- the largest setting whose wrapped block fits."""
    _, _, bw, bh = box
    best = None
    for detail, name in enumerate(ladder):
        for scale in scales:
            lines = _wrap(name, text, bw, scale)
            if not lines:
                continue
            lh = _line_height(name, scale)
            block = lh * len(lines) + round(lh * _LEADING) * (len(lines) - 1)
            if block > bh or max(bigtext.width(name, ln, scale) for ln in lines) > bw:
                continue
            rank = (lh, -detail)
            if best is None or rank > best[0]:
                best = (rank, name, scale, lines)
    if best is None:
        name = ladder[-1]
        return name, 1, _wrap(name, text, bw, 1)[:1]
    return best[1], best[2], best[3]


def draw(canvas, text: str, theme=None) -> None:
    theme = theme or Theme()
    text = (text or "").strip().upper()
    x, y, bw, bh = theme.box
    if not text:
        bigtext.fill(canvas, "NO MESSAGE SET", (x, y + bh // 3, bw, bh // 4), theme.accent)
        return

    name, scale, lines = layout(text, theme.box)
    lh = _line_height(name, scale)
    step = lh + round(lh * _LEADING)
    block = step * (len(lines) - 1) + lh
    top = y + (bh - block) // 2
    for i, line in enumerate(lines):
        bigtext.draw_centered(canvas, name, line, x + bw // 2,
                              top + i * step + lh // 2, theme.main, scale)


# -- compact: sized to a face slot rather than the whole panel -------------

def draw_compact(canvas, x, y, w_, h_, text: str, theme=None) -> None:
    theme = theme or Theme()
    text = (text or "").strip().upper()
    if not text:
        return
    box = (x + 1, y + 1, w_ - 2, h_ - 2)
    name, scale, lines = layout(text, box)
    lh = _line_height(name, scale)
    step = lh + round(lh * _LEADING)
    block = step * (len(lines) - 1) + lh
    top = box[1] + (box[3] - block) // 2
    for i, line in enumerate(lines):
        bigtext.draw_centered(canvas, name, line, box[0] + box[2] // 2,
                              top + i * step + lh // 2, theme.main, scale)
