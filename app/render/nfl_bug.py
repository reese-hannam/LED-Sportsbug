"""NFL score bug, 128x64.

    x0                      x77 | x78                 x127
    +---------------------------+---------------------+
    |  | CLE  ...            17 |        3RD          |
    |    ooo                    |                     |   timeout pips
    |  | PIT  *              24 |       5:32          |
    |    oo_                    |                     |
    +---------------------------+---------------------+
    | 1st & 10                              PIT 35    |
    +-------------------------------------------------+

Timeouts are drawn as three small pips under the abbreviation -- filled for
remaining, hollow for spent. A number would be smaller but pips read at a glance
from across a room, which is the whole point of the panel.
"""

import datetime

from ..matrix import font, text_width
from ..sources.base import FINAL, PRE
from . import logos, widgets as w

SPLIT = 78
SCORE_RIGHT = SPLIT - 6
ROW_A_BASE = 17     # baseline of the away abbreviation
ROW_H_BASE = 41     # baseline of the home abbreviation
PIP_DROP = 4        # pips sit this far below the abbreviation baseline
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
STRIP_Y = 49


def _fmt_start(iso: str) -> tuple[str, str]:
    """(day, time) for an upcoming game, e.g. ("SUN", "1:00").

    Split onto two lines because the pre-game screen is what the panel cycles
    through all week when nothing is live, so it wants to be readable rather
    than merely present.
    """
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%a").upper(), dt.strftime("%-I:%M")
    except Exception:
        return "", ""


def _timeout_pips(canvas, x, y, left):
    """Three pips: filled = timeout in hand, hollow = used. `left` None = unknown."""
    if left is None:
        return
    for i in range(3):
        on = i < left
        w.dot(canvas, x + i * 5, y, w.YELLOW if on else (55, 55, 55), filled=on)


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


def draw(canvas, game):
    f_abbrev = font("7x13B")
    f_score = font("9x15B")
    f_small = font("5x7")
    f_record = font("6x10")
    f_record_small = font("5x7")
    f_rec = _record_font(game, f_abbrev, f_record, f_record_small)

    d = game.detail
    poss = d.get("possession", "")
    live = game.state not in (PRE, FINAL)

    rows = (
        ("away", game.away, ROW_A_BASE, d.get("timeouts_away")),
        ("home", game.home, ROW_H_BASE, d.get("timeouts_home")),
    )

    # A logo replaces the colour spine when it's switched on and the artwork
    # exists. The abbreviation STAYS either way: at 16px a bold mark (a Browns
    # helmet, a Cowboys star) reads instantly, but a detailed one turns to mush,
    # so the logo is recognition-at-a-glance and the abbreviation is what
    # actually guarantees you can tell who is playing.
    for side, team, base_y, touts in rows:
        drawn = logos.draw_team(canvas, game, side, LOGO_X, base_y - 14)
        if not drawn:
            w.rect(canvas, 1, base_y - 11, 3, 13, team.color, fill=True)

        name_x = NAME_X if drawn else 8
        name = team.abbrev[:3]
        w.text(canvas, f_abbrev, name_x, base_y, w.WHITE, name)

        # Possession marker sits right of the abbreviation -- measured, not a
        # fixed offset, so it can't collide when a font or name length changes.
        if poss and poss == team.abbrev:
            w.dot(canvas, name_x + text_width(f_abbrev, name) + 3, base_y - 8, w.YELLOW)

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

        if live:
            _timeout_pips(canvas, name_x + 1, base_y + PIP_DROP, touts)

    w.vline(canvas, SPLIT, 2, STRIP_Y - 2, (40, 40, 40))

    if game.state == PRE:
        day, at = _fmt_start(game.start_utc)
        w.text_center(canvas, f_small, 103, 22, w.YELLOW, day)
        w.text_center(canvas, f_small, 103, 34, w.WHITE, at)
        return

    if game.state == FINAL:
        w.text_center(canvas, f_abbrev, 103, 30, w.YELLOW, game.period or "F")
        return

    # Quarter over clock. The clock is shown exactly as the feed reports it --
    # see README: no source exposes whether it's running, so it is not ticked
    # locally. It updates when the data does.
    w.text_center(canvas, f_abbrev, 103, 20, w.YELLOW, game.period)
    w.text_center(canvas, f_small, 103, 38, w.WHITE, d.get("clock", ""))

    # Bottom strip: down & distance, ball spot. Red zone tints the strip.
    dd = d.get("down_distance", "")
    spot = d.get("yardline", "")
    if dd or spot:
        # Only the RED ZONE gets a filled strip. A neutral fill was (26,26,30)
        # -- nearly black in RGB, but 128x15 of "nearly black" on an LED panel
        # is a visible dim-white wash behind white text, and it made the red
        # zone tint mean less by being always-on. Black background, one rule
        # above it: the strip now only lights up when it means something.
        if d.get("red_zone"):
            w.rect(canvas, 0, STRIP_Y, 128, 15, (110, 0, 0), fill=True)
        w.hline(canvas, 0, 127, STRIP_Y, (60, 60, 60))
        if dd:
            w.text(canvas, f_small, 3, STRIP_Y + 11, w.WHITE, dd[:13])
        if spot:
            w.text_right(canvas, f_small, 125, STRIP_Y + 11, w.WHITE, spot[:9])
