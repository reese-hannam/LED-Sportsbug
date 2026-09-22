"""College football score bug, 128x64.

    x0                        x77 | x78                x127
    +------------------------------+---------------------+
    | | 14 USC                 21  |        3RD          |
    |                              |                     |
    | |    SJSU                 7  |       5:32          |
    +------------------------------+---------------------+
    | 1st & 10                                  USC 35   |
    +----------------------------------------------------+

Two differences from the NFL bug, both driven by the sport rather than taste.

Rankings are shown, because in college they're most of the story -- "14 USC"
tells you more about a game than the score alone, and an unranked team simply
gets no number rather than a placeholder.

Abbreviations run longer (SJSU, MTST, WASH) than the NFL's uniform three, so
the name column is measured and the score is right-anchored against it rather
than sitting at a fixed offset.
"""

import datetime

from ..matrix import font, text_width
from ..sources.base import FINAL, PRE
from . import logos, widgets as w

SPLIT = 78
SCORE_RIGHT = SPLIT - 3
ROW_A_BASE = 17
ROW_H_BASE = 41
STRIP_Y = 49

# Abbreviations use the 7x13B bold face -- see the note in nfl_bug.py for why
# 6x13B could not draw an M. No FBS abbreviation is longer than four
# characters, so the extra pixel per glyph costs nothing here either.
NAME_X = 20         # left edge of the team abbreviation

# With a logo in front, everything shifts right. The rank is RIGHT-aligned
# against the name rather than left-aligned after the logo: a two-digit rank is
# twice as wide as a single digit, so left-aligning put #10-#25 a single pixel
# from the abbreviation while #1-#9 sat five away. Right-aligning keeps the gap
# to the name constant and lets the extra width grow back toward the logo,
# where there is slack.
LOGO_X = 0
LOGO_NAME_X = 25
RANK_GAP = 3        # between the rank digits and the abbreviation


def _fmt_start(iso: str) -> tuple[str, str]:
    """(day, time) for an upcoming game, e.g. ("SUN", "1:00").

    The day is ALWAYS included, even for a game later today. When the panel is
    cycling a whole week's schedule -- which is the main reason a pre-game
    screen is on the panel at all -- a bare "6:30" doesn't say which night.
    """
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%a").upper(), dt.strftime("%-I:%M")
    except Exception:
        return "", ""


def _fit(text: str, f, width: int) -> str:
    while text and text_width(f, text) > width:
        text = text[:-1]
    return text


def draw(canvas, game):
    f_abbrev = font("7x13B")
    f_score = font("9x15B")
    f_small = font("5x7")
    f_tiny = font("4x6")
    f_record = font("6x10")

    d = game.detail or {}
    poss = d.get("possession", "")

    rows = (
        ("away", game.away, ROW_A_BASE, d.get("rank_away")),
        ("home", game.home, ROW_H_BASE, d.get("rank_home")),
    )

    # College is the tight one: rank, logo, a name up to five characters, and
    # the score all want the same 72 pixels. The logo is 12px here rather than
    # 16 so the rank column survives -- a rank tells you more about a college
    # game than a crest does, and at this size most college crests are the
    # hardest to read anyway.
    for side, team, base_y, rank in rows:
        drawn = logos.draw_team(canvas, game, side, LOGO_X, base_y - 12)
        if not drawn:
            w.rect(canvas, 1, base_y - 11, 3, 13, team.color, fill=True)

        name_x = LOGO_NAME_X if drawn else NAME_X

        # Names line up whether or not a team is ranked; the rank grows
        # leftward from a fixed gap, so the column reads like a ranked list.
        if rank:
            digits = str(rank)
            w.text(canvas, f_tiny,
                   name_x - RANK_GAP - text_width(f_tiny, digits), base_y - 3,
                   w.YELLOW, digits)

        # The name's room is whatever the thing on the right doesn't need.
        # Measured rather than a fixed number, because that number depends on
        # the score's width ("7" and "35" differ by nine pixels), the font, and
        # whether a logo pushed the name right -- and a stale constant here
        # means an abbreviation drawn straight through the score.
        if game.state == PRE:
            right, right_f = (team.record or ""), f_record
        else:
            right, right_f = str(team.score), f_score
        budget = SCORE_RIGHT - text_width(right_f, right) - 4 - name_x

        name = _fit(team.abbrev[:5], f_abbrev, budget)
        w.text(canvas, f_abbrev, name_x, base_y, w.WHITE, name)

        if poss and poss == team.abbrev:
            w.dot(canvas, name_x + text_width(f_abbrev, name) + 3, base_y - 8, w.YELLOW)

        if right:
            w.text_right(canvas, right_f, SCORE_RIGHT,
                         base_y - 3 if game.state == PRE else base_y,
                         w.MUTED if game.state == PRE else w.WHITE, right)

    w.vline(canvas, SPLIT, 2, STRIP_Y - 2, (40, 40, 40))

    if game.state == PRE:
        # Two lines rather than one cramped 4x6 row: this is the screen you
        # read from across the room when nothing is live.
        day, at = _fmt_start(game.start_utc)
        w.text_center(canvas, f_small, 103, 22, w.YELLOW, day)
        w.text_center(canvas, f_small, 103, 34, w.WHITE, at)
        return

    if game.state == FINAL:
        w.text_center(canvas, f_abbrev, 103, 30, w.YELLOW, game.period or "F")
        return

    w.text_center(canvas, f_abbrev, 103, 20, w.YELLOW, game.period)
    w.text_center(canvas, f_small, 103, 38, w.WHITE, d.get("clock", ""))

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
