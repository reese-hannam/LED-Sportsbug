"""Weather screen: the temperature large, with the time and conditions, and
sunrise/sunset as an optional arc.

Every row is optional except the temperature, and the layout is built from
whatever is switched on -- so turning the sun arc off doesn't leave a gap
where it used to be, it gives its height back to the temperature. That's the
difference between a screen that fills the panel and one that merely has
things on it.

No degree GLYPH is ever typed: these are pixel fonts with a fixed character
set, and this project has been burned before by assuming a font contains
something it doesn't. A small hand-drawn ring stands in for it.
"""

import datetime
import math

from . import bigtext, widgets as w
from .lifestyle_clock import clock_string
from .lifestyle_theme import Theme, rows

_ICON_MIN = 10


def _disc(canvas, cx, cy, r, rgb):
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                canvas.SetPixel(cx + dx, cy + dy, *rgb)


def _cloud(canvas, x, y, width, height, rgb):
    """A flat-bottomed cloud, drawn WIDER than it is tall.

    Round bumps on a square-ish block just merge into a dome at this size --
    a cloud reads by being wide and lumpy along the top, so the bumps are
    small relative to the width and sit at three different heights.
    """
    base_h = max(2, round(height * 0.34))
    w.rect(canvas, x, y + height - base_h, width, base_h, rgb, fill=True)
    for fx, fy, fr in ((0.25, 0.58, 0.20), (0.53, 0.36, 0.26), (0.79, 0.56, 0.18)):
        _disc(canvas, x + round(width * fx), y + round(height * fy),
              max(1, round(width * fr)), rgb)


def _icon(canvas, x, y, size, kind, main, accent):
    """One weather glyph inside a `size` square."""
    size = max(_ICON_MIN, size)
    if kind == "clear":
        cx, cy, r = x + size // 2, y + size // 2, max(2, size // 4)
        _disc(canvas, cx, cy, r, main)
        for deg in range(0, 360, 45):
            a = math.radians(deg)
            for step in (r + 2, r + 3):
                canvas.SetPixel(round(cx + step * math.cos(a)),
                                round(cy + step * math.sin(a)), *main)
        return

    if kind not in ("rain", "snow"):
        _cloud(canvas, x, y + round(size * 0.15), size, round(size * 0.7), accent)
        return

    # Rain and snow keep their fall INSIDE the square: drawing it below the
    # cloud spilled into the row underneath and read as stray pixels.
    cloud_h = round(size * 0.58)
    _cloud(canvas, x, y, size, cloud_h, accent)
    for i in range(3):
        dx = x + round(size * (0.22 + i * 0.28))
        if kind == "rain":
            w.vline(canvas, dx, y + cloud_h + 1, y + size - 1, main)
        else:
            canvas.SetPixel(dx, y + cloud_h + 2, *main)
            canvas.SetPixel(dx, y + size - 1, *main)


def _sun_arc(canvas, weather, box, theme):
    """Sunrise on the left, sunset on the right, a dot at today's elapsed
    fraction of daylight.

    Open-Meteo's times are already local to the place asked about (the fetch
    sends timezone=auto), so this stays naive-local throughout rather than
    mixing in an aware `now` and risking an offset mismatch.
    """
    x, y, bw, bh = box
    try:
        parse = datetime.datetime.fromisoformat
        sunrise, sunset = parse(weather["sunrise"]), parse(weather["sunset"])
    except (KeyError, ValueError):
        return

    label_h = 7 if bh >= 14 else 0
    arc_h = max(3, bh - label_h - 1)
    x0, x1 = x + 16, x + bw - 16
    base = y + arc_h
    for i in range(0, x1 - x0 + 1, 2):
        frac = i / max(1, x1 - x0)
        canvas.SetPixel(x0 + i, base - round(arc_h * math.sin(math.pi * frac)),
                        *theme.accent)

    now = datetime.datetime.now()
    span = (sunset - sunrise).total_seconds()
    frac = max(0.0, min(1.0, (now - sunrise).total_seconds() / span)) if span > 0 else 0.0
    sx = x0 + round(frac * (x1 - x0))
    sy = base - round(arc_h * math.sin(math.pi * frac))
    lit = sunrise <= now <= sunset
    w.rect(canvas, sx - 1, sy - 1, 3, 3, theme.main if lit else theme.accent, fill=True)

    if label_h:
        f = "4x6"
        bigtext.draw(canvas, f, x, base + label_h, theme.accent, clock_string(sunrise))
        rise_w = bigtext.width(f, clock_string(sunset))
        bigtext.draw(canvas, f, x + bw - rise_w, base + label_h, theme.accent,
                     clock_string(sunset))


def _temp_row(canvas, weather, units, box, theme):
    """Icon, the temperature as large as it will go, a drawn degree ring and
    the unit -- centred as one group so it reads as a single object."""
    x, y, bw, bh = box
    text = str(weather["temp"])
    icon = max(_ICON_MIN, min(bh, bw // 5))
    unit = "F" if units == "f" else "C"

    # The number takes whatever the icon and the degree-and-unit cluster don't
    # need. The share is measured rather than guessed: the cluster's size
    # depends on the number's, so an over-generous first guess is re-fitted
    # smaller until the whole group genuinely fits the row -- otherwise a
    # three-digit temperature pushed the unit off the edge of the panel.
    def cluster(share):
        name, scale = bigtext.best_fit(text, max(8, round((bw - icon - 4) * share)), bh)
        nbox = bigtext.ink_box(name, text, scale)
        nw, nh = nbox[2] - nbox[0] + 1, nbox[3] - nbox[1] + 1
        # Ring and unit are sized FROM the number, so the group stays in
        # proportion at any temperature, colour scheme or panel scale.
        ring = max(3, round(nh * 0.22))
        u_name, u_scale = bigtext.best_fit(unit, max(8, round(nh * 0.6)),
                                           max(6, round(nh * 0.55)))
        ubox = bigtext.ink_box(u_name, unit, u_scale)
        uw = ubox[2] - ubox[0] + 1
        gap = max(1, ring // 2)
        total = icon + 4 + nw + gap + ring + gap + uw
        return total, (name, scale, nbox, nw, nh, ring, u_name, u_scale, ubox, uw, gap)

    for share in (0.80, 0.72, 0.64, 0.56, 0.48):
        total, parts = cluster(share)
        if total <= bw:
            break
    name, scale, nbox, nw, nh, ring, u_name, u_scale, ubox, _uw, gap = parts

    left = x + max(0, (bw - total) // 2)
    cy = y + bh // 2
    top = cy - nh // 2

    _icon(canvas, left, cy - icon // 2, icon, weather.get("icon"), theme.main, theme.accent)
    nx = left + icon + 4
    bigtext.draw(canvas, name, nx - nbox[0], top - nbox[1], theme.main, text, scale)
    # Ring at the number's shoulder; the unit's ink bottom lines up with the
    # number's, the way a weather app sets it.
    rx = nx + nw + gap
    w.rect(canvas, rx, top, ring, ring, theme.main)
    bigtext.draw(canvas, u_name, rx + ring + gap, top + nh - 1 - ubox[3],
                 theme.main, unit, u_scale)


def draw(canvas, weather: dict | None, units: str, theme=None, show_time=True,
         show_conditions=True, show_hilo=False, show_sun=True) -> None:
    theme = theme or Theme()
    box = theme.box
    if not weather:
        x, y, bw, bh = box
        bigtext.fill(canvas, "WEATHER", (x, y + bh // 4, bw, bh // 4), theme.main)
        bigtext.fill(canvas, "NO READING YET", (x, y + bh // 2, bw, bh // 5), theme.accent)
        return

    # Weights, not pixel positions: a row that's switched off gives its space
    # back to the temperature rather than leaving a hole.
    time_text = clock_string(datetime.datetime.now().astimezone())
    cond = str(weather.get("condition", ""))[:20]
    hilo = f"H{weather['high']}  L{weather['low']}"

    plan = [
        (10, lambda b: _temp_row(canvas, weather, units, b, theme)),
        (3 if show_time else 0, lambda b: bigtext.fill(canvas, time_text, b, theme.main)),
        (3 if show_conditions else 0, lambda b: bigtext.fill(canvas, cond, b, theme.accent)),
        (2 if show_hilo else 0, lambda b: bigtext.fill(canvas, hilo, b, theme.accent)),
        (6 if show_sun else 0, lambda b: _sun_arc(canvas, weather, b, theme)),
    ]
    for slot, (weight, drawer) in zip(rows(box, [p[0] for p in plan]), plan):
        if slot is not None:
            drawer(slot)


# -- compact: sized to a face slot rather than the whole panel -------------

def draw_temp_compact(canvas, x, y, w_, h_, weather, units, theme=None) -> None:
    if not weather:
        return
    theme = theme or Theme()
    text = f"{weather['temp']}{'F' if units == 'f' else 'C'}"
    bigtext.fill(canvas, text, (x + 1, y + 1, w_ - 2, h_ - 2), theme.main)


def draw_conditions_compact(canvas, x, y, w_, h_, weather, theme=None) -> None:
    if not weather:
        return
    theme = theme or Theme()
    bigtext.fill(canvas, str(weather.get("condition", ""))[:16],
                 (x + 1, y + 1, w_ - 2, h_ - 2), theme.main)


def draw_hilo_compact(canvas, x, y, w_, h_, weather, theme=None) -> None:
    if not weather:
        return
    theme = theme or Theme()
    bigtext.fill(canvas, f"H{weather['high']} L{weather['low']}",
                 (x + 1, y + 1, w_ - 2, h_ - 2), theme.accent)
