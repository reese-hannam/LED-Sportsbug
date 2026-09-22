"""MLB score bug, 128x64.

    x0                     x77 | x78                  x127
    +--------------------------+-----------------------+
    |  | CLE               4   |  v7          <>       |   inning + bases
    |  ------------------------|  B * * o              |
    |  | BOS               2   |  S * o                |   count
    |                          |  O * o                |
    +--------------------------+-----------------------+
    | J.RAMIREZ 2-4 · RBI                              |   who's up, and his night
    +--------------------------------------------------+

The right column is only 50px wide, so the bases diamond sits beside the inning
on the top line rather than competing with the count rows below it.
"""

import datetime

from ..matrix import font, text_width
from ..sources.base import FINAL, PRE
from . import bigtext, logos, widgets as w

SPLIT = 78          # x of the divider between team block and state block
SCORE_RIGHT = 72    # scores are right-anchored here
# The bold display font is 7x13B, not the 6x13B this project started with.
# At six pixels a bold stem is 2px, leaving a 2px interior -- not enough room
# for an M's diagonals, so 6x13B drew them as three solid rows and "MIN" read
# as "HIN". The same squeeze removed the gap BETWEEN glyphs: "MW" came out as
# ##..####..## with the stems touching. One extra pixel of width fixes both,
# and no abbreviation in any of the three leagues needed the space back.

LOGO_X = 1          # left edge of the team logo, when logos are on
RECORD_GAP = 4      # clear space between abbreviation and record
# Records anchor nearer the divider than scores do. They have no reason to
# line up with the score column, and those few pixels are the difference
# between a bold record and falling back to a thinner one.
RECORD_RIGHT = SPLIT - 2
NAME_X = 20         # abbreviation moves right to make room for one
ROW_A_BASE = 19     # baseline of the away row
ROW_H_BASE = 42     # baseline of the home row
STRIP_Y = 48        # top of the batter strip

RZ = SPLIT + 2      # left edge of the right column


def _fmt_start(iso: str) -> str:
    """UTC ISO -> local '7:05p'."""
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%-I:%M") + ("a" if dt.hour < 12 else "p")
    except Exception:
        return ""


def _record_font(game, f_abbrev, big, small):
    """One record font for BOTH rows.

    Deciding per row means a card where one team's record is bold and the
    other's isn't -- which reads as a mistake rather than a choice. Sized to
    the worst case: the widest record against the widest abbreviation, with a
    logo assumed present since that pushes the name furthest right.
    """
    widest_name = max((text_width(f_abbrev, t.abbrev[:3])
                       for t in (game.away, game.home)), default=0)
    widest_rec = max((text_width(big, t.record or "")
                      for t in (game.away, game.home)), default=0)
    room = RECORD_RIGHT - (NAME_X + widest_name) - RECORD_GAP
    return big if widest_rec <= room else small


def _draw_teams(canvas, game):
    f_abbrev = font("7x13B")
    f_score = font("10x20")
    f_record = font("6x10")
    f_record_small = font("5x7")
    f_rec = _record_font(game, f_abbrev, f_record, f_record_small)

    # The logo takes the colour spine's place when it's on; the abbreviation
    # stays regardless -- see the note in nfl_bug.
    for side, team, base_y in (("away", game.away, ROW_A_BASE),
                               ("home", game.home, ROW_H_BASE)):
        drawn = logos.draw_team(canvas, game, side, LOGO_X, base_y - 14)
        if not drawn:
            w.rect(canvas, 1, base_y - 12, 3, 14, team.color, fill=True)
        name_x = NAME_X if drawn else 8
        name = team.abbrev[:3]
        w.text(canvas, f_abbrev, name_x, base_y, w.WHITE, name)

        if game.state == PRE:
            if team.record:
                # 6x10, not 4x6: a record is meant to be READ, and 4x6 draws
                # it in single-pixel strokes that vanish at any distance on a
                # real panel. There is no score competing for this space
                # pre-game, so use it -- but MEASURE first. "70-60" at 6x10 is
                # 30px, and right-anchored it lands 1px from the end of "LAD",
                # reading as one word. Step down a size rather than collide.
                # Baseline shared with the abbreviation: at 4x6 a raised
                # record read as a superscript annotation, but at this size it
                # just looks misaligned.
                w.text_right(canvas, f_rec, RECORD_RIGHT, base_y, w.MUTED,
                             team.record)
        else:
            w.text_right(canvas, f_score, SCORE_RIGHT, base_y, w.WHITE, str(team.score))

    w.hline(canvas, 1, SCORE_RIGHT, 25, (40, 40, 40))
    w.vline(canvas, SPLIT, 2, STRIP_Y - 2, (40, 40, 40))


def _draw_batter(canvas, game):
    """Bottom strip: who's at the plate and how his night has gone."""
    line = game.detail.get("batter_line", "")
    if not line:
        return
    # No filled background. (26,26,30) is nearly black as a number, but 128x16
    # of it on an LED panel is a visible dim-white wash behind white text --
    # the same thing that made the football down-and-distance strip look odd.
    # A single rule is enough to separate it from the scoreboard above.
    w.hline(canvas, 0, 127, STRIP_Y, (60, 60, 60))
    # Sized to the strip rather than pinned to 4x6: "J.SOTO 2-4, HR, 2 RBI" is
    # long enough to need the small font, but plenty of batter lines are short
    # and were being drawn tiny for no reason.
    bigtext.fill(canvas, line, (2, STRIP_Y + 2, 124, 12), (215, 215, 215))


def draw(canvas, game):
    f_abbrev = font("7x13B")
    f_small = font("5x7")
    f_tiny = font("4x6")

    _draw_teams(canvas, game)

    if game.state == PRE:
        w.text_center(canvas, f_small, 103, 26, w.YELLOW, _fmt_start(game.start_utc))
        w.text_center(canvas, f_tiny, 103, 38, w.DIM, game.status_detail[:9])
        return

    if game.state == FINAL:
        w.text_center(canvas, f_abbrev, 103, 30, w.YELLOW, game.period or "F")
        return

    d = game.detail
    is_top = d.get("is_top", True)
    inning = game.period[1:] if game.period else ""

    # Top line of the right column: half-inning arrow, inning number, bases.
    w.triangle(canvas, RZ + 4, 3, 4, w.YELLOW, up=is_top)
    w.text(canvas, f_abbrev, RZ + 9, 13, w.WHITE, inning)
    w.bases(canvas, 111, 9, d.get("bases", [False, False, False]), size=2, spread=5)

    # Between halves there is no live count -- say so instead of showing stale zeros.
    if d.get("inning_state") in ("Middle", "End"):
        w.text_center(canvas, f_small, 103, 34, w.DIM, "MID" if is_top else "END")
        return

    for label, count, total, y, rgb in (
        ("B", d.get("balls", 0), 3, 20, w.GREEN),
        ("S", d.get("strikes", 0), 2, 30, w.YELLOW),
        ("O", d.get("outs", 0), 2, 40, w.RED),
    ):
        w.text(canvas, f_tiny, RZ + 1, y + 5, w.DIM, label)
        w.dot_row(canvas, RZ + 8, y, count, total, rgb, gap=5)

    _draw_batter(canvas, game)
