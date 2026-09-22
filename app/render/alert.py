"""The score alert screen, 128x64.

    +--------------------------------------+
    |##########  2 RUNS  ###################|  <- scoring team's color, pulsing
    |             N.CHURCH                  |  <- who did it
    |          DOUBLE  ·  2 RBI             |  <- how
    |   SCORED A.BURLESON, J.WALKER         |  <- who came around (MLB)
    |   BAL 7           B7          STL 6   |  <- result, and when
    |=================----------------------|  <- how long the alert stays up
    +--------------------------------------+

The third line is MLB-only and appears when runners scored. NFL alerts use the
two-line form, and the whole block shifts up when the third line is present.

Everything is static -- nothing scrolls. `describe_score` compresses the play
into two lines that fit at 128px rather than a sentence that has to crawl past.
When it can't parse the play text it falls back to "CLE +7", which is short by
construction, so the layout never needs to scroll.

The band pulses rather than blinking the whole screen: on a P3 panel at close
range a full-frame flash is genuinely unpleasant to look at.
"""

import math

from ..matrix import font
from ..sources.colors import led
from . import bigtext
from . import widgets as w

BAND_H = 16
PULSE_HZ = 2.0

Y_SCORER = 32      # who did it
Y_HOW = 43         # how
Y_RESULT = 57      # score line, with the game clock between the two teams
Y_BAR = 63

# When there's a third line to show (MLB runners who scored), everything above
# the result line tightens up to make room.
Y3_SCORER = 29
Y3_HOW = 39
Y3_SCORED = 48

# Vertical room for the 'how' line. Both layouts leave the same gap between
# the scorer above and the next line below, so one number covers each.
HOW_H = 9


def _pulse(elapsed: float, low: float = 0.7) -> float:
    """Smooth 0..1 oscillation, floored so the band stays vividly the team's color.

    The floor matters more than it looks: at 0.45 a dark primary like Cleveland's
    bottoms out as muddy brown and the banner stops reading as a team color.
    """
    return low + (1 - low) * (0.5 + 0.5 * math.sin(elapsed * PULSE_HZ * 2 * math.pi))


def _scale(rgb, factor: float):
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


def _readable_on(rgb) -> tuple:
    """Black or white text, whichever survives on this background."""
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return (0, 0, 0) if luma > 140 else (255, 255, 255)


def _as_text(rgb) -> tuple:
    """A team color bright enough to read as TEXT on black.

    The palette's own floor is tuned for filled blocks like the banner. Thin
    glyph strokes need more: the White Sox' near-black primary survives as a
    16px band but disappears as 4px-tall lettering.
    """
    return led(rgb, min_luma=155)


def draw(canvas, alert):
    e = alert.event
    f_band = font("7x13B")
    f_big = font("9x15B")
    f_small = font("5x7")
    f_tiny = font("4x6")

    color = e.team.color
    lit = _scale(color, _pulse(alert.elapsed))

    # --- banner ------------------------------------------------------------
    w.rect(canvas, 0, 0, 128, BAND_H, lit, fill=True)
    # Contrast is judged against the BASE color, not the pulsed one -- deciding
    # per frame would flip the text black/white as the band brightens.
    w.text_center(canvas, f_band, 64, 12, _readable_on(color), e.label[:16])

    # --- who scored, and how -----------------------------------------------
    # Falls back to the team and points when the play text can't be parsed --
    # both fit without scrolling, which is the whole point of this layout.
    if e.scorer:
        y_scorer, y_how = (Y3_SCORER, Y3_HOW) if e.scored else (Y_SCORER, Y_HOW)
        w.text_center(canvas, f_big, 64, y_scorer, w.WHITE, e.scorer)
        if e.how:
            # Sized to the gap rather than pinned to 4x6. "MAHOMES 12 YD PASS"
            # in single-pixel strokes is unreadable across a room, which is the
            # one place this is ever read from -- but the string length varies
            # enormously, so a fixed larger font would overflow instead. fill()
            # takes the biggest font that fits the box and trims only if even
            # the smallest won't.
            box_y = y_how - HOW_H + 1
            bigtext.fill(canvas, e.how, (2, box_y, 124, HOW_H), (200, 200, 200))
        if e.scored:
            # Runners who came around, in the scoring team's color so it reads as
            # "these are ours" rather than as more of the play description.
            w.text_center(canvas, f_tiny, 64, Y3_SCORED, _as_text(e.team.color), e.scored)
    else:
        w.text_center(canvas, f_big, 64, Y_SCORER, w.WHITE, f"{e.team.abbrev} +{e.points}")

    # --- result, with the clock sitting between the two teams --------------
    for team, anchor, right in ((e.away, 3, False), (e.home, 125, True)):
        label = f"{team.abbrev} {team.score}"
        # The scoring team's side is drawn in its color so the eye lands there.
        rgb = _as_text(team.color) if team.abbrev == e.team.abbrev else w.DIM
        if right:
            w.text_right(canvas, f_small, anchor, Y_RESULT, rgb, label)
        else:
            w.text(canvas, f_small, anchor, Y_RESULT, rgb, label)

    when = " ".join(x for x in (e.period, e.clock) if x)
    if when:
        w.text_center(canvas, f_small, 64, Y_RESULT, w.YELLOW, when)

    # --- how much longer this alert stays up -------------------------------
    remaining = max(0.0, 1.0 - (alert.elapsed / alert.duration if alert.duration else 1.0))
    w.hline(canvas, 0, 127, Y_BAR, (30, 30, 30))
    if remaining > 0:
        w.hline(canvas, 0, int(127 * remaining), Y_BAR, _scale(color, 0.9))
