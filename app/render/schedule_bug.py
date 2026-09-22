"""Pre-game screens, 128x64 -- what the panel shows for a game not yet started.

The live bug is the wrong shape for this. Its layout is built around data a
scheduled game doesn't have: the score column is empty, the clock column says
nothing a date can't, and the down-and-distance strip along the bottom is
simply blank -- fifteen pixels of black on a panel that only has sixty-four.
That is most of a quarter of the display spent on nothing.

So a pre-game gets its own layout, and there is more than one to choose from
because they answer different questions:

    ROWS      the familiar shape -- one team per row -- but sized to the whole
              panel instead of leaving the strip empty. Reads like a schedule:
              you scan several of these in a row and take in who's playing.

    MATCHUP   one game, presented. Big logos facing each other, then who and
              when. Reads like a title card, and it's the one worth having on
              screen when nothing is live and people are looking at the panel
              rather than glancing at it.

Both are deliberately built from the same three facts -- who, their record or
rank, and when -- so switching between them changes the presentation and never
the information.
"""

import datetime

from ..matrix import font, text_width
from . import logos, widgets as w

ROWS = "rows"
MATCHUP = "matchup"
LOOKS = (ROWS, MATCHUP)

LOOK_LABEL = {
    ROWS: "Rows",
    MATCHUP: "Matchup",
}

BIG_LOGO = 28


def _parts(iso: str):
    """(day, date, time) for a kickoff, e.g. ("SUN", "AUG 23", "8:00P").

    Split into three because the layouts want different amounts of it: a row
    of schedules only has room for the day and time, while a title card can
    afford the date and is much more useful with it.
    """
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return "", "", ""
    hour = dt.strftime("%-I:%M")
    suffix = "P" if dt.hour >= 12 else "A"
    return dt.strftime("%a").upper(), dt.strftime("%b %-d").upper(), hour + suffix


# The rank is typed, not hand-drawn, but NOT at every size: 4x6 and 6x13B both
# weld the "#" to the digit beside it -- 4x6 has no side bearing at all, and
# 6x13B's crossbars fill the whole cell -- so "#7" comes out as one blob. 5x7
# and 6x10 render it cleanly, and the rank is drawn at those.
RANK_GAP = 4        # between the abbreviation and the rank that follows it


def _rank(detail, side) -> str:
    """The rank digits ("3"), or empty when the team isn't ranked.

    A rank sits BESIDE the abbreviation rather than replacing the record --
    they're different facts and a ranked team still has a record worth seeing.
    An earlier version returned one or the other, so ranked teams silently lost
    their record.
    """
    rank = (detail or {}).get(f"rank_{side}")
    return str(rank) if rank else ""


# ---------------------------------------------------------------------------


def draw_rows(canvas, game):
    """Two team rows over a full-width footer carrying the date.

    Same shape as the live bug so a mixed rotation doesn't jump around, but the
    rows are spread into the height the empty score column was wasting and the
    footer holds the kickoff rather than nothing.
    """
    f_name = font("7x13B")
    f_small = font("5x7")
    f_rank = font("6x10")
    d = game.detail or {}

    day, date, at = _parts(game.start_utc)

    for team, side, base_y in ((game.away, "away", 20), (game.home, "home", 43)):
        drawn = logos.draw_team(canvas, game, side, 2, base_y - 15)
        if not drawn:
            w.rect(canvas, 1, base_y - 12, 3, 14, team.color, fill=True)

        # The abbreviation sits at a FIXED x whether or not the team is ranked,
        # and the rank follows it. Putting the rank first pushed the name right
        # by a variable amount, so a ranked team's name sat several pixels off
        # from an unranked one on the row above -- the column stopped looking
        # like a column. Trailing it costs nothing: the space between the name
        # and the record is dead otherwise.
        x = 22 if drawn else 8
        name = team.abbrev[:5]
        w.text(canvas, f_name, x, base_y, w.WHITE, name)

        rank = _rank(d, side)
        if rank:
            w.text(canvas, f_rank, x + text_width(f_name, name) + RANK_GAP,
                   base_y, w.YELLOW, f"#{rank}")

        if team.record:
            # Bold and brighter: 4x6 in DIM was a grey smudge on the panel.
            w.text_right(canvas, font("6x10"), 125, base_y - 3,
                         w.MUTED, team.record)

    # Footer: the whole width, which is exactly the space the live bug's
    # down-and-distance strip leaves black on a game that hasn't started.
    #
    # It carries a second line when there's a betting line to show. Kickoff
    # moves up to make room rather than the line being squeezed in at 4x6 --
    # a spread is a number you read, not decoration. With no odds (which is
    # normal days out, and for some games always) the footer keeps its
    # original single centred line instead of leaving a gap.
    spread = d.get("spread", "")
    when = " ".join(p for p in (day, date) if p)

    if spread:
        w.hline(canvas, 0, 127, 47, (48, 48, 54))
        w.text(canvas, f_small, 3, 55, w.YELLOW, when[:15])
        if at:
            w.text_right(canvas, f_small, 125, 55, w.WHITE, at)
        w.text(canvas, f_small, 3, 63, w.MUTED, spread[:11])
    else:
        w.hline(canvas, 0, 127, 50, (48, 48, 54))
        w.text(canvas, f_small, 3, 61, w.YELLOW, when[:15])
        if at:
            w.text_right(canvas, f_small, 125, 61, w.WHITE, at)


def draw_matchup(canvas, game):
    """Title card: the two logos facing each other, then who and when.

    The logos carry the top two thirds because at 28px they are finally big
    enough to be the thing you recognise rather than a garnish on a text row --
    including college crests, which are hopeless at the 12px the live bug can
    spare them.
    """
    f_name = font("7x13B")
    f_small = font("5x7")
    f_at = font("10x20")
    f_tiny = font("4x6")
    d = game.detail or {}

    day, date, at = _parts(game.start_utc)
    # Pushed apart from the original 33/95 to open the centre gap from 34px to
    # 48px. That gap has to hold the spread, and at 34px even a short line like
    # "LAR -3.5" only fits in 4x6 with a single pixel off each logo -- which is
    # both unreadable and looks like a collision. The logos stay 28px; they
    # just sit further out, which the 128px width has room for.
    left_cx, right_cx = 26, 102

    for team, side, cx in ((game.away, "away", left_cx), (game.home, "home", right_cx)):
        placed = logos.draw_team(canvas, game, side,
                                 cx - BIG_LOGO // 2, 2, size=BIG_LOGO)
        if not placed:
            # No artwork: a colour block keeps the composition rather than
            # leaving a hole where the logo should be.
            w.rect(canvas, cx - 9, 6, 18, 20, team.color, fill=True)

        # The abbreviation stays centred on the logo above it, and the rank
        # hangs off its left rather than being centred with it. Centring the
        # pair as a unit would pull the name off the logo's axis, and only for
        # ranked teams -- so a top-25 matchup would sit differently from an
        # unranked one for no reason the viewer can see.
        name = team.abbrev[:5]
        name_x = cx - text_width(f_name, name) // 2
        w.text(canvas, f_name, name_x, 44, w.WHITE, name)

        rank = _rank(d, side)
        if rank:
            # Ahead of the name here rather than after it: this look centres the
            # name on the logo above, and anything trailing it would push that
            # centring off. It does not move the name -- it hangs to the left.
            label = f"#{rank}"
            w.text(canvas, f_small, max(0, name_x - text_width(f_small, label) - 3),
                   44, w.YELLOW, label)

        if team.record:
            # 5x7: the rank sits at baseline 44 just above, and a 13px-tall
            # record would collide with it.
            w.text(canvas, f_small, cx - text_width(f_small, team.record) // 2, 52,
                   w.MUTED, team.record)


    # "@" between them, on the logos' own centre line rather than below them,
    # so it reads as separating the two rather than floating under both, with
    # the spread stacked underneath it in the same gap.
    # The "@" sits between the logos, vertically centred on them, and it is
    # drawn LARGE on purpose: an "@" is mostly interior detail, so at 4x6 or
    # even 7x13B it collapses into an indistinct blob and reads as a smudge
    # rather than a character. The 48px gap has room for the big one.
    w.text_center(canvas, f_at, 64, 2 + BIG_LOGO - 5, w.MUTED, "@")

    # Spread in the middle of the card, sharing the records' line -- centred
    # between the two of them rather than needing a line of its own.
    #
    # This only works because the logos moved out to 26/102: the space between
    # the records is 61px now against 47px before, which is the difference
    # between three separate readings and "0-0 LAR -3.5 0-0" running together
    # as one string. Measured against the actual records rather than assumed,
    # since a two-digit record ("10-2") eats into it.
    spread = (game.detail or {}).get("spread", "")
    if spread:
        widest_rec = max((text_width(f_small, tm.record or "")
                          for tm in (game.away, game.home)), default=0)
        # 5x7 -- deliberately the same size as the date line below it, and the
        # same as the records either side. 6x10 was tried and overwhelmed the
        # card next to a wide logo and name. 4x6 stays only as a last resort
        # for a long college line ("NCSU -30.5") that will not fit at 5x7;
        # shrinking beats truncating a number into something misleading.
        free = (right_cx - widest_rec // 2) - (left_cx + widest_rec // 2) - 12
        for f in (f_small, f_tiny):
            if text_width(f, spread) <= free:
                w.text_center(canvas, f, 64, 52, w.MUTED, spread)
                break

    when = " ".join(p for p in (day, date, at) if p)
    w.text_center(canvas, f_small, 64, 62, w.YELLOW, when[:21])


DRAWERS = {ROWS: draw_rows, MATCHUP: draw_matchup}


def draw(canvas, game, look: str = ROWS) -> None:
    DRAWERS.get(look, draw_rows)(canvas, game)
