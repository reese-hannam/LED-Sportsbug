"""Fantasy alerts, in two weights.

The user picks per category which of these a play gets, because the right
answer changes by day and by category: a touchdown probably deserves the whole
screen, a routine 4-yard catch probably doesn't deserve to hide the game you're
watching.

  FULL     takes the panel, like a scoring alert.
  BANNER   a 16px strip along the bottom, drawn OVER whatever is already
           showing. You keep the score bug and still learn your guy caught one.

Both carry the same four facts in the same order -- what kind of alert, which
fantasy team(s), who, and what happened -- so the eye learns one pattern.
"""

from ..matrix import font, text_width
from ..sources.colors import led
from . import widgets as w

BAND_H = 15
BANNER_H = 16
BANNER_Y = 64 - BANNER_H

AMBER = (255, 176, 0)
BAND_BG = (150, 96, 0)


def _fit(text: str, f, width: int) -> str:
    """Trim to fit, without leaving a dangling partial word where avoidable."""
    if text_width(f, text) <= width:
        return text
    while text and text_width(f, text) > width:
        text = text[:-1]
    return text


# In the multi-player layout each row shares the width with a name, so the
# description has to give ground. These are the phrases that actually overflow.
_ABBREV = (
    ("RECEIVING TD", "REC TD"),
    ("RUSHING TD", "RUSH TD"),
    ("PUNT RETURN TD", "PUNT RET TD"),
    ("KICK RETURN TD", "KICK RET TD"),
    ("FUMBLE RECOVERY", "FUM REC"),
    ("INTERCEPTION", "INT"),
    ("FIELD GOAL", "FG"),
    ("RECEPTION", "REC"),
)


def _compact(desc: str) -> str:
    out = (desc or "").upper()
    for long, short in _ABBREV:
        out = out.replace(long, short)
    return out


def _teams(e, f, width: int) -> str:
    """Whose player this is: every roster, readable.

    A player started on two rosters is ONE alert naming both. When both won't
    fit whole, each gets an equal share of the line, trimmed on word
    boundaries -- "YUNG GUNZ + THE OTHER" rather than the second name cut to
    "LE". Three or more can't share one line legibly, so they read as the first
    and a count.
    """
    teams = [t.upper() for t in (getattr(e, "teams", None) or [])]
    joined = " + ".join(teams)
    if text_width(f, joined) <= width or len(teams) < 2:
        return w.fit_words(f, joined, width)
    if len(teams) == 2:
        share = (width - text_width(f, " + ")) // 2
        return " + ".join(w.fit_words(f, name, share) for name in teams)
    return w.fit_words(f, f"{teams[0]} & {len(teams) - 1} MORE", width)


def _short_name(name: str) -> str:
    """'George Pickens' -> 'G.PICKENS' when the full name won't fit."""
    parts = (name or "").split()
    if len(parts) < 2:
        return (name or "").upper()
    return f"{parts[0][0]}.{parts[-1]}".upper()


def draw_full(canvas, alert):
    """Whole-screen fantasy alert."""
    e = alert.event
    f_band = font("7x13B")
    f_big = font("9x15B")
    f_small = font("5x7")
    f_tiny = font("4x6")

    # --- banner ---------------------------------------------------------
    w.rect(canvas, 0, 0, 128, BAND_H, BAND_BG, fill=True)
    w.text_center(canvas, f_band, 64, 11, (255, 255, 255), "FANTASY")

    # --- which team(s) --------------------------------------------------
    # 5x7, not 4x6: whose player this is has to be readable from across a
    # room, and a player started on two rosters names both here.
    w.text_center(canvas, f_small, 64, 25, AMBER, _teams(e, f_small, 124))

    # --- several of your players in one play ------------------------------
    players = getattr(e, "players", None) or [(e.player_name, e.description)]
    if len(players) > 1:
        # A QB and his receiver, say. Give each a compact row rather than
        # blowing one name up and hiding the other.
        y = 38
        for name, desc in players[:3]:
            w.text(canvas, f_small, 3, y, (255, 255, 255),
                   _fit(_short_name(name), f_small, 46))
            w.text_right(canvas, f_tiny, 126, y, (185, 185, 185),
                         _fit(_compact(desc), f_tiny, 78))
            y += 11
        if len(players) > 3:
            w.text_right(canvas, f_tiny, 125, y, AMBER, f"+{len(players) - 3} MORE")
        _countdown(canvas, alert)
        return

    # --- who ------------------------------------------------------------
    name = (e.player_name or "").upper()
    if text_width(f_big, name) > 126:
        name = _short_name(e.player_name)
    if text_width(f_big, name) > 126:
        # Still too wide even shortened -- drop to the smaller face rather
        # than clipping a name mid-letter.
        w.text_center(canvas, f_band, 64, 41, (255, 255, 255), _fit(name, f_band, 126))
    else:
        w.text_center(canvas, f_big, 64, 42, (255, 255, 255), name)

    # --- what happened ---------------------------------------------------
    desc = _fit((e.description or "").upper(), f_small, 126)
    w.text_center(canvas, f_small, 64, 56, (185, 185, 185), desc)

    _countdown(canvas, alert)


def _countdown(canvas, alert):
    remaining = max(0.0, 1.0 - (alert.elapsed / alert.duration if alert.duration else 1.0))
    w.hline(canvas, 0, 127, 63, (30, 30, 30))
    if remaining > 0:
        w.hline(canvas, 0, int(127 * remaining), 63, AMBER)


def draw_banner(canvas, alert):
    """Bottom strip drawn over the current screen -- the light-touch variant."""
    e = alert.event
    f_small = font("5x7")

    y = BANNER_Y
    w.rect(canvas, 0, y, 128, BANNER_H, (14, 14, 16), fill=True)

    # The top rule doubles as the countdown, shrinking as the strip's time
    # runs out -- the bottom row is needed for the second line of text.
    remaining = max(0.0, 1.0 - (alert.elapsed / alert.duration if alert.duration else 1.0))
    w.hline(canvas, 0, 127, y, led(AMBER, 90))
    if remaining > 0:
        w.hline(canvas, 0, int(127 * remaining), y, AMBER)

    # Line one says WHOSE: the fantasy team(s). The bottom bar used to leave
    # this out entirely -- only the full-screen style named the team -- so a
    # routine catch by a player on one of two rosters didn't say which.
    w.text(canvas, f_small, 2, y + 8, AMBER, _teams(e, f_small, 124))

    players = getattr(e, "players", None) or [(e.player_name, e.description)]
    lead_name, lead_desc = players[0]
    line = f"{_short_name(lead_name)} {_compact(lead_desc)}"
    if len(players) > 1:
        line += f" +{len(players) - 1}"
    w.text(canvas, f_small, 2, y + 15, (235, 235, 235), _fit(line, f_small, 124))


def draw(canvas, alert):
    """Route by the style the user chose for this category."""
    if getattr(alert.event, "style", "full") == "banner":
        draw_banner(canvas, alert)
    else:
        draw_full(canvas, alert)
