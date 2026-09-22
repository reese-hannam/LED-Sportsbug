"""Clock screens: four styles, each sized to fill the panel.

    segment   seven-segment digits, drawn as bars and grown to whatever the
              layout box allows -- the classic "LED clock" this hardware is
              literally built like.
    digital   the time set as large as the type will go (see bigtext.py).
    analog    a face filling the height, optionally with the digital time
              across the middle.
    minimal   deliberately small and quiet, the one style that does NOT fill
              the screen -- that's the whole point of it.

Colour, size and whether the date shows all come from the theme and the
settings; nothing here hardcodes a palette.
"""

import datetime
import math

from ..matrix import graphics
from . import bigtext, widgets as w
from .lifestyle_theme import Theme, rows

STYLES = ("segment", "digital", "analog", "minimal")
LABEL = {
    "segment": "Segment",
    "digital": "Digital",
    "analog": "Analog",
    "minimal": "Minimal",
}


def clock_string(dt: datetime.datetime, seconds: bool = False) -> str:
    """"8:45 PM" or "8:45:32 PM" -- the one place AM/PM formatting happens,
    reused by the weather screen so the two never disagree on how a time
    looks."""
    fmt = "%-I:%M:%S" if seconds else "%-I:%M"
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{dt.strftime(fmt)} {suffix}"


def date_string(dt: datetime.datetime) -> str:
    return dt.strftime("%a %b %-d").upper()


def _now():
    return datetime.datetime.now().astimezone()


# -- segment --------------------------------------------------------------
# Seven bars per digit rather than a font: no BDF face ships at the size this
# wants, and a real digital clock IS seven segments, so drawing them directly
# is both simpler and more honest than faking the look with type.
SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
    "5": "afgcd", "6": "afgecd", "7": "abc", "8": "abcdefg", "9": "abcdfg",
}


def _seg_digit(canvas, x, y, dw, dh, ch, rgb, t):
    segs = SEGMENTS.get(ch, "")
    half = dh // 2
    if "a" in segs:
        w.rect(canvas, x + t, y, dw - 2 * t, t, rgb, fill=True)
    if "g" in segs:
        w.rect(canvas, x + t, y + half - t // 2, dw - 2 * t, t, rgb, fill=True)
    if "d" in segs:
        w.rect(canvas, x + t, y + dh - t, dw - 2 * t, t, rgb, fill=True)
    if "f" in segs:
        w.rect(canvas, x, y, t, half, rgb, fill=True)
    if "b" in segs:
        w.rect(canvas, x + dw - t, y, t, half, rgb, fill=True)
    if "e" in segs:
        w.rect(canvas, x, y + half, t, half, rgb, fill=True)
    if "c" in segs:
        w.rect(canvas, x + dw - t, y + half, t, half, rgb, fill=True)


def _seg_metrics(groups, box_w, box_h):
    """The biggest digit size that fits, as (dw, dh, t, gap, colon_w, total).

    Grown from the height down until the row also fits the width, so the
    digits are as large as the box allows in both directions rather than a
    fixed size that leaves the panel half empty.
    """
    digits = sum(len(g) for g in groups)
    colons = len(groups) - 1
    for dh in range(box_h, 5, -1):
        dw = max(4, round(dh * 0.58))
        gap = max(1, round(dw * 0.20))
        colon_w = max(2, round(dw * 0.30))
        total = digits * dw + (digits - 1) * gap + colons * (colon_w + gap)
        if total <= box_w:
            return dw, dh, max(2, round(dh / 8)), gap, colon_w, total
    return 4, 6, 1, 1, 2, digits * 5


def _seg_clock(canvas, cx, cy, groups, box_w, box_h, rgb):
    dw, dh, t, gap, colon_w, total = _seg_metrics(groups, box_w, box_h)
    x = cx - total // 2
    y = cy - dh // 2
    for gi, grp in enumerate(groups):
        for ch in grp:
            _seg_digit(canvas, x, y, dw, dh, ch, rgb, t)
            x += dw + gap
        if gi < len(groups) - 1:
            dot = max(2, t)
            cxx = x + colon_w // 2 - dot // 2
            w.rect(canvas, cxx, y + dh // 3 - dot // 2, dot, dot, rgb, fill=True)
            w.rect(canvas, cxx, y + dh * 2 // 3 - dot // 2, dot, dot, rgb, fill=True)
            x += colon_w + gap


def _draw_segment(canvas, now, seconds, theme, box):
    hour = int(now.strftime("%I"))
    groups = [str(hour), f"{now.minute:02d}"]
    if seconds:
        groups.append(f"{now.second:02d}")
    # The AM/PM marker earns a strip of its own rather than floating.
    main_row, tail = rows(box, [8, 2])
    _seg_clock(canvas, main_row[0] + main_row[2] // 2, main_row[1] + main_row[3] // 2,
               groups, main_row[2], main_row[3], theme.main)
    suffix = "AM" if now.hour < 12 else "PM"
    bigtext.fill(canvas, suffix, tail, theme.accent)


def _draw_digital(canvas, now, seconds, theme, box):
    bigtext.fill(canvas, clock_string(now, seconds), box, theme.main)


def _draw_minimal(canvas, now, seconds, theme, box):
    # Small on purpose, in BOTH directions: the one style that doesn't fill
    # the panel. Constraining only the height let it grow until it spanned
    # the full width anyway, which is the opposite of minimal -- if you want
    # the time big, the other three styles are right there.
    x, y, bw, bh = box
    iw, ih = round(bw * 0.62), round(bh * 0.42)
    inner = (x + (bw - iw) // 2, y + (bh - ih) // 2, iw, ih)
    bigtext.fill(canvas, clock_string(now, seconds), inner, theme.main,
                 scales=(2, 1))


def _hand(canvas, cx, cy, angle, length, rgb):
    graphics.DrawLine(canvas, cx, cy,
                      round(cx + length * math.cos(angle)),
                      round(cy + length * math.sin(angle)), w.c(rgb))


def _draw_analog(canvas, now, seconds, theme, box, digital=False):
    x, y, bw, bh = box
    r = max(8, min(bw, bh) // 2 - 1)
    cx, cy = x + bw // 2, y + bh // 2

    steps = max(48, r * 6)
    for i in range(steps):
        a = i / steps * 2 * math.pi
        canvas.SetPixel(round(cx + r * math.cos(a)), round(cy + r * math.sin(a)),
                        *theme.accent)
    for deg in range(0, 360, 30):
        a = math.radians(deg - 90)
        long_tick = deg % 90 == 0
        inner = r - (5 if long_tick else 2)
        graphics.DrawLine(canvas,
                          round(cx + inner * math.cos(a)), round(cy + inner * math.sin(a)),
                          round(cx + r * math.cos(a)), round(cy + r * math.sin(a)),
                          w.c(theme.main if long_tick else theme.accent))

    hour_angle = math.radians((now.hour % 12 + now.minute / 60) / 12 * 360 - 90)
    minute_angle = math.radians((now.minute + now.second / 60) / 60 * 360 - 90)
    _hand(canvas, cx, cy, hour_angle, r * 0.55, theme.main)
    _hand(canvas, cx, cy, minute_angle, r * 0.82, theme.main)
    if seconds:
        _hand(canvas, cx, cy, math.radians(now.second / 60 * 360 - 90),
              r * 0.88, theme.accent)
    w.rect(canvas, cx - 1, cy - 1, 2, 2, theme.main, fill=True)

    if digital:
        # A window in the lower dial, the way a watch carries a date -- cut
        # out and drawn last so the hands pass BEHIND it. Centring it instead
        # put it under the pivot, where every hand scribbles through the
        # digits and neither reads.
        half_w, half_h = int(r * 0.66), max(4, int(r * 0.22))
        wx, wy = cx - half_w, cy + int(r * 0.40) - half_h
        ww, wh = half_w * 2, half_h * 2
        w.rect(canvas, wx, wy, ww, wh, (0, 0, 0), fill=True)
        w.rect(canvas, wx, wy, ww, wh, theme.accent)
        # No AM/PM in here: the dial says which half of the day it is, and the
        # window is far too narrow to spend three characters saying it again.
        bigtext.fill(canvas, now.strftime("%-I:%M"),
                     (wx + 2, wy + 2, ww - 4, wh - 4), theme.main, scales=(2, 1))


def draw(canvas, style: str = "segment", seconds: bool = False, theme=None,
         show_date: bool = False, analog_digital: bool = False) -> None:
    theme = theme or Theme()
    now = _now()
    box = theme.box

    # The date takes a strip off the bottom; the clock fills whatever is left.
    if show_date:
        top, bottom = rows(box, [8, 2])
        bigtext.fill(canvas, date_string(now), bottom, theme.accent, scales=(2, 1))
        box = top

    if style == "digital":
        _draw_digital(canvas, now, seconds, theme, box)
    elif style == "analog":
        _draw_analog(canvas, now, seconds, theme, box, digital=analog_digital)
    elif style == "minimal":
        _draw_minimal(canvas, now, seconds, theme, box)
    else:
        _draw_segment(canvas, now, seconds, theme, box)


# -- compact: sized to a face slot rather than the whole panel -------------

def draw_compact(canvas, x, y, w_, h_, seconds: bool = False, theme=None) -> None:
    theme = theme or Theme()
    bigtext.fill(canvas, clock_string(_now(), seconds), (x + 1, y + 1, w_ - 2, h_ - 2),
                 theme.main)


def draw_date_compact(canvas, x, y, w_, h_, theme=None) -> None:
    theme = theme or Theme()
    bigtext.fill(canvas, date_string(_now()), (x + 1, y + 1, w_ - 2, h_ - 2), theme.accent)
