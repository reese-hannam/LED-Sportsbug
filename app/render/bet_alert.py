"""Bet alert cards, 128x64.

Every card names its TICKET. With one card per leg and several parlays in play,
"which bet is this?" is the first question, so the parlay's name sits in the
band across the top of every card -- and "LEG 2/6" beside it says where on the
ticket this leg is.

  +------------------------------------------+
  | SUNDAY PARLAY                  LEG 2/6   |  <- ticket, and where on it
  |              C.MCCAFFREY                 |  <- who
  | RU+RE YD                        57 / 100 |  <- what, and the number
  | [####################..................]   |  <- bar, from before to now
  | +12                           2 OF 6 IN  |  <- what changed; the ticket
  +------------------------------------------+

The band's colour carries the state before a word is read: gold is progress,
green is a leg or bet landing, red is an under that just busted. Nothing that
has to be read is set smaller than 5x7 -- 4x6's single-pixel strokes don't
survive the distance a wall panel is read from.

A parlay completing keeps its own louder treatment (starburst, rings, every
leg) because it's rare and final. A whole-ticket card pages through every leg,
four to a screen, after a parlay's run of leg cards when that's switched on.

Everything is driven off `alert.elapsed`, so the render loop just calls draw()
at 30fps and the animation follows.
"""

import math

from ..bet_events import PROGRESS, LEG_HIT, BET_HIT, PARLAY_HIT, PARLAY_TICKET
from ..bets import BUSTED
from ..matrix import font, text_width
from . import bets_page, bigtext, widgets as w

GOLD = (255, 190, 0)
GREEN = (0, 225, 70)
RED = (235, 45, 45)

BAND_H = 13
STRIP_H = 16


def ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _phase(elapsed: float, start: float, length: float) -> float:
    """0..1 progress through a phase, clamped outside it."""
    if length <= 0:
        return 1.0
    return max(0.0, min(1.0, (elapsed - start) / length))


def _fit(text: str, f, width: int) -> str:
    while text and text_width(f, text) > width:
        text = text[:-1]
    return text


def _ink(rgb) -> tuple:
    """Black or white text, whichever survives on this background."""
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return (0, 0, 0) if luma > 135 else (255, 255, 255)


def _plot(canvas, x, y, rgb):
    if 0 <= x < 128 and 0 <= y < 64:
        canvas.SetPixel(x, y, *rgb)


def _line(canvas, x0, y0, x1, y1, rgb):
    """Bresenham, clipped to the panel.

    graphics.DrawLine writes wherever the geometry says, and a starburst
    reaching past the edge put pixels at y=72 -- silently ignored by a
    forgiving canvas, but still work done and a bounds bug waiting to matter.
    """
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        _plot(canvas, x0, y0, rgb)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _circle(canvas, cx, cy, r, rgb):
    """Midpoint circle, clipped -- a ring of radius 33 about y=32 spans y=-1..65."""
    x, y, err = r, 0, 1 - r
    while x >= y:
        for px, py in ((x, y), (y, x), (-y, x), (-x, y), (-x, -y), (-y, -x), (y, -x), (x, -y)):
            _plot(canvas, cx + px, cy + py, rgb)
        y += 1
        if err < 0:
            err += 2 * y + 1
        else:
            x -= 1
            err += 2 * (y - x) + 1


def _rings(canvas, cx, cy, elapsed, rgb, count=3, spacing=0.22, speed=54.0, max_r=74):
    """Concentric rings expanding outward, staggered."""
    for i in range(count):
        t = elapsed - i * spacing
        if t <= 0:
            continue
        r = int(t * speed)
        if 1 <= r <= max_r:
            # Rings dim as they grow, so the burst reads as travelling outward.
            fade = max(0.25, 1.0 - r / max_r)
            _circle(canvas, cx, cy, r, tuple(int(c * fade) for c in rgb))


def _starburst(canvas, cx, cy, elapsed, rgb, spokes=12, speed=60.0, length=13):
    """Spokes shooting outward from the middle."""
    reach = elapsed * speed
    if reach <= 0:
        return
    for i in range(spokes):
        angle = (2 * math.pi * i) / spokes
        inner = max(0.0, reach - length)
        x0, y0 = cx + math.cos(angle) * inner, cy + math.sin(angle) * inner
        x1, y1 = cx + math.cos(angle) * reach, cy + math.sin(angle) * reach
        _line(canvas, x0, y0, x1, y1, rgb)


def _check(canvas, cx, cy, scale, rgb):
    """A checkmark that can be drawn part-grown."""
    scale = max(0.0, min(1.0, scale))
    if scale <= 0:
        return
    ax, ay = cx - 7, cy + 1
    bx, by = cx - 2, cy + 6
    cx2, cy2 = cx + 7, cy - 6
    s1 = min(1.0, scale * 2)
    _line(canvas, ax, ay, ax + (bx - ax) * s1, ay + (by - ay) * s1, rgb)
    if scale > 0.5:
        s2 = (scale - 0.5) * 2
        _line(canvas, bx, by, bx + (cx2 - bx) * s2, by + (cy2 - by) * s2, rgb)


def _band(canvas, label, rgb, elapsed, height=15):
    """Pulsing header band for the parlay celebration."""
    pulse = 0.72 + 0.28 * (0.5 + 0.5 * math.sin(elapsed * 2.4 * 2 * math.pi))
    lit = tuple(min(255, int(c * pulse)) for c in rgb)
    w.rect(canvas, 0, 0, 128, height, lit, fill=True)
    w.text_center(canvas, font("7x13B"), 64, height - 4, _ink(rgb), label[:16])


def _leg_pips(canvas, y, hit, total, elapsed, appear_from=0.0, rgb=GOLD):
    """Row of pips, one per leg, popping in one at a time."""
    total = min(total, 8)
    gap = 9
    x0 = 64 - (total * gap) // 2
    for i in range(total):
        filled = i < hit
        if filled and elapsed < appear_from + i * 0.16:
            filled = False
        w.dot(canvas, x0 + i * gap, y, rgb if filled else (55, 55, 55), filled=filled)


def _ticket(e) -> str:
    return (e.parlay.name if e.parlay else "STRAIGHT BET").upper()


def _fraction(bet, value) -> float:
    """Where the bar stood at `value` -- the start of this card's animation."""
    if value is None:
        return 0.0
    if bet.goal > 0:
        return max(0.0, min(1.0, value / bet.goal))
    return 1.0


def _n(v: float) -> str:
    return str(int(v)) if float(v) == int(v) else f"{v:g}"


# ---------------------------------------------------------------------------


def draw_card(canvas, alert):
    """One leg (or straight bet): progress, a hit, or an under busting."""
    e = alert.event
    prog = e.progress
    t = alert.elapsed
    f = font("5x7")

    hit = e.kind in (LEG_HIT, BET_HIT)
    busted = prog.status == BUSTED
    tone = GREEN if hit else RED if busted else GOLD

    # --- ticket band --------------------------------------------------------
    if hit:
        right = "HIT"
    elif busted:
        right = "BUST"
    elif e.legs_total:
        right = f"LEG {e.leg_no}/{e.legs_total}"
    else:
        right = "BET"
    # A landing pulses; plain progress holds still, so the two read differently
    # from across a room before either is read.
    level = 0.78 + 0.22 * (0.5 + 0.5 * math.sin(t * 2.4 * 2 * math.pi)) if hit else 1.0
    band = tuple(min(255, int(c * level)) for c in tone)
    w.rect(canvas, 0, 0, 128, BAND_H, band, fill=True)
    ink = _ink(tone)
    right_w = text_width(f, right)
    w.text_right(canvas, f, 126, 10, ink, right)
    w.text(canvas, f, 2, 10, ink, w.fit_words(f, _ticket(e), 128 - right_w - 9))

    # --- who ------------------------------------------------------------------
    bigtext.fill(canvas, bets_page._panel_name(e.bet.player_name),
                 (2, BAND_H + 2, 124, 12), w.WHITE)

    # --- what, and the number --------------------------------------------------
    value = prog.display
    w.text_right(canvas, f, 126, 36, w.WHITE, value)
    w.text(canvas, f, 2, 36, w.MUTED,
           _fit(bets_page._stat_text(e.bet), f, 124 - text_width(f, value) - 6))

    # --- the bar, from where it was to where it is now -------------------------
    start = _fraction(e.bet, e.before)
    fill = start + (prog.fraction - start) * ease_out(_phase(t, 0.15, 0.9))
    w.progress(canvas, 2, 39, 124, 8, fill, rgb_on=tone, rgb_off=(34, 34, 38))

    # --- what changed, and how the ticket stands --------------------------------
    if hit:
        w.text(canvas, f, 2, 57, tone, "HIT")
        _check(canvas, 30, 54, _phase(t, 1.0, 0.5), tone)
    elif busted:
        w.text(canvas, f, 2, 57, tone, "BUSTED")
    elif e.delta:
        w.text(canvas, f, 2, 57, tone, f"+{_n(e.delta)}")
    if e.legs_total:
        w.text_right(canvas, f, 126, 57, (185, 185, 185),
                     f"{e.legs_hit} OF {e.legs_total} IN")


def draw_strip(canvas, alert):
    """Bottom-bar variant, drawn OVER whatever is showing -- the game stays up."""
    e = alert.event
    prog = e.progress
    t = alert.elapsed
    total = alert.duration
    f = font("5x7")

    # Slide in, hold, slide out.
    if t < 0.35:
        off = int((1 - ease_out(t / 0.35)) * STRIP_H)
    elif t > total - 0.35:
        off = int(ease_out((t - (total - 0.35)) / 0.35) * STRIP_H)
    else:
        off = 0
    y = 64 - STRIP_H + off
    if y >= 64:
        return

    hit = e.kind in (LEG_HIT, BET_HIT)
    tone = GREEN if hit else RED if prog.status == BUSTED else GOLD
    # Only the rows actually on the panel: while it slides, part of the strip
    # is below the bottom edge, and drawing all sixteen rows put a row at y=64.
    w.rect(canvas, 0, y, 128, 64 - y, (14, 14, 18), fill=True)
    w.hline(canvas, 0, 127, y, tone)
    if y + 10 > 63:
        return      # still sliding: no room for the text yet

    # Compact value ("57/100") -- every pixel here goes to the words.
    value = prog.display.replace(" / ", "/")
    w.text_right(canvas, f, 126, y + 9, tone, value)
    # The PLAYER can't be dropped: they're who the strip is about. An earlier
    # layout led with the ticket and a long parlay name pushed the player off
    # entirely ("SUNDAY FUNDAY S  57/100"). So the ticket gives way instead --
    # whole, then its first word, then not at all.
    room = 124 - text_width(f, value) - 4
    name = bets_page._panel_name(e.bet.player_name)
    ticket = _ticket(e)
    first = ticket.split()[0] if ticket.split() else ""
    lead = next((c for c in (f"{ticket} {name}", f"{first} {name}")
                 if text_width(f, c) <= room), name)
    w.text(canvas, f, 2, y + 9, w.WHITE, _fit(lead, f, room))
    if y + 14 <= 63:
        w.progress(canvas, 2, y + 12, 124, 3, prog.fraction, rgb_on=tone,
                   rgb_off=(40, 40, 44))


def draw_parlay_hit(canvas, alert):
    """Radial and loud: starburst, rings, then every leg accounted for."""
    e = alert.event
    t = alert.elapsed

    if t < 1.55:
        _starburst(canvas, 64, 32, t, GOLD)
        _rings(canvas, 64, 32, t - 0.3, GOLD, count=2, speed=48)
        if t > 0.6:
            w.text_center(canvas, font("7x13B"), 64, 37, w.WHITE, "PARLAY!")
        return

    s = t - 1.55
    _band(canvas, "PARLAY HIT", GOLD, t)
    f = font("5x7")
    w.text_center(canvas, f, 64, 27, w.WHITE, w.fit_words(f, _ticket(e), 124))
    w.text_center(canvas, f, 64, 37, GOLD, f"ALL {e.legs_total} LEGS IN")
    _leg_pips(canvas, 45, e.legs_hit, e.legs_total, t, appear_from=1.55 + 0.2)
    _check(canvas, 64, 56, _phase(s, 0.8, 0.5), GOLD)


def draw_ticket(canvas, alert):
    """The whole ticket, paged, closing a parlay's run of leg cards.

    Which page shows is derived from `alert.elapsed` rather than counted, so
    this stays a pure function of time like everything else here.
    """
    e = alert.event
    page = int(alert.elapsed // e.page_seconds) if e.page_seconds > 0 else 0
    # Clamp rather than wrap: the card expires at the end of the last page, so
    # a float rounding up mustn't flip it back to page one for a frame.
    page = max(0, min(page, e.context_pages - 1))
    bets_page.draw_parlay(canvas, e.parlay, e.parlay_legs, page=page,
                          highlight=e.moved_ids())


def draw(canvas, alert) -> None:
    """Route by what the card is, then by the style the user picked for it."""
    e = alert.event
    if e.kind == PARLAY_TICKET:
        draw_ticket(canvas, alert)
    elif getattr(e, "style", "full") == "banner":
        draw_strip(canvas, alert)
    elif e.kind == PARLAY_HIT:
        draw_parlay_hit(canvas, alert)
    elif e.kind in (PROGRESS, LEG_HIT, BET_HIT):
        draw_card(canvas, alert)
