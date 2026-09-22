"""Bet tracking screens, 128x64.

Two layouts, because a straight prop and a parlay want different things:

  PROPS -- two bets per screen, each with a progress bar. The bar is the point:
  from across a room you should be able to see how close something is without
  reading any numbers.

    +------------------------------------------+
    | J.JEUDY  O80                    61 / 80  |
    | [##################......................] |
    |------------------------------------------|
    | N.CHUBB  O46                    52 / 46  |
    | [########################################] |
    +------------------------------------------+

  PARLAY -- one parlay per screen, every leg on its own line with a status mark,
  because what matters is which leg is dragging and whether it's already dead.

    +------------------------------------------+
    | PARLAY                        2/4 LEGS   |
    | + J.JEUDY  O80                  85 / 80  |
    | + N.CHUBB  O46                  52 / 46  |
    | . D.NJOKU  O4                    2 / 4   |
    | x A.COOPER O61                  12 / 61  |
    +------------------------------------------+

Status is carried by color first and a glyph second -- color reads instantly,
the glyph is what makes it survive being photographed or viewed by someone who
can't separate the red from the green.
"""

from ..bets import LIVE, HIT, BUSTED, PENDING, parlay_status
from ..matrix import font, text_width
from . import widgets as w

SLOT_H = 32          # a props screen is two of these
BETS_PER_PAGE = 2
LEGS_PER_PAGE = 4

STATUS_COLOR = {
    LIVE: (255, 200, 0),
    HIT: (0, 220, 60),
    BUSTED: (230, 40, 40),
    PENDING: (90, 90, 90),
}
STATUS_MARK = {LIVE: "·", HIT: "+", BUSTED: "x", PENDING: "·"}


# Breathing room between a name and the value to its right.
GUTTER = 4


def _stat_text(bet):
    """What identifies a bet on the panel: the STAT, not the line.

    This used to print the over/under line -- "J.JEUDY O200". At 4x6 a capital
    O against digits reads as a zero, so "O200" looked like "0200", and the
    line it was conveying is already spelled out in the value column beside it
    ("74 / 200"). The stat is the part you genuinely cannot infer from anything
    else on the row, so it gets the space instead.

    Unders are marked, overs are not. Almost every prop is an over, so marking
    the common case would spend pixels on every row to say nothing; the rare
    one has to be unmistakable, because direction decides whether a rising
    number is good news.
    """
    from ..tracker import stat_short
    short = stat_short(bet.sport, bet.stat)
    return f"U {short}" if bet.direction != "over" else short


def _fit_label(name, suffix, f, x, f_val, value, right=126, gutter=GUTTER):
    """Fit "NAME REC YDS" beside a right-aligned value, degrading sensibly.

    Truncating to a fixed character count -- which this used to do -- assumes
    every value is the same width. It isn't: "2 / 4" and "74 / 200" differ by
    twelve pixels, and a long name next to a long value overlapped it.

    When the pair won't fit the stat is dropped rather than the name being cut
    mid-word, since a half-rendered stat name is worse than none. An under's
    "U" is kept a step longer than the stat it belongs to: losing it would make
    an under look exactly like an over, which inverts what the row means.
    """
    limit = right - text_width(f_val, value) - gutter - x

    for candidate in _degrade(name, suffix):
        if text_width(f, candidate) <= limit:
            return candidate

    out = name
    while out and text_width(f, out) > limit:
        out = out[:-1]
    return out


# Trailing generational suffixes. Dropped before abbreviating, or "Harold
# Fannin Jr." condenses to "H.JR" -- the one part of the name that identifies
# nobody.
_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}


def _panel_name(name: str) -> str:
    """"Shedeur Sanders" -> "S.SANDERS", the way a broadcast lower third does.

    Full names crowd the stat off the row -- "SHEDEUR SANDERS" alone is 60 of
    the ~80 pixels a leg line has -- and the stat is the part that can't be
    guessed. The surname carries nearly all the identifying power, so it's the
    initial that goes.
    """
    parts = [p for p in (name or "").replace(".", " ").split() if p]
    while len(parts) > 1 and parts[-1].upper().strip(".") in _SUFFIXES:
        parts.pop()
    if len(parts) < 2:
        return (name or "").upper()
    return f"{parts[0][0].upper()}.{parts[-1].upper()}"


def _degrade(name, suffix):
    """Fallbacks in order of preference, widest first.

    The STAT outranks the full first name: shortening "SHEDEUR SANDERS" to
    "S.SANDERS" still says who, while dropping "PASS YDS" loses the only thing
    on the row you couldn't work out for yourself.
    """
    short = _panel_name(name)
    out = []
    if suffix:
        out.append(f"{name} {suffix}")
        if short != name:
            out.append(f"{short} {suffix}")
        if suffix.startswith("U "):
            out += [f"{name} U", f"{short} U"]
    out.append(name)
    if short != name:
        out.append(short)
    return out


def _fit_two(name, stat, f_name, f_stat, x, f_val, value, right=126, gutter=GUTTER):
    """Lay a big name and a small stat on one row: ("NAME", "STAT", stat_x).

    The props row has more vertical room than a parlay leg, so the name gets
    the larger face and the stat a smaller one beside it. That's a hierarchy
    rather than a compromise -- you scan for the player, then read what the bet
    is -- and the smaller face is what makes the stat fit at all next to a name
    like "SHEDEUR SANDERS".
    """
    limit = right - text_width(f_val, value) - gutter
    for candidate in ([name, _panel_name(name)] if _panel_name(name) != name else [name]):
        nw = text_width(f_name, candidate)
        stat_x = x + nw + 5
        if stat and stat_x + text_width(f_stat, stat) <= limit:
            return candidate, stat, stat_x
    # No room for the stat at any name length; keep the name, drop the stat.
    out = name
    while out and x + text_width(f_name, out) > limit:
        out = out[:-1]
    return out, "", 0


def _bar_colors(status):
    on = STATUS_COLOR.get(status, w.DIM)
    return on, (36, 36, 40)


def draw_props(canvas, page):
    """`page` is a list of at most BETS_PER_PAGE Progress objects."""
    f_name = font("5x7")
    f_val = font("4x6")

    for i, prog in enumerate(page[:BETS_PER_PAGE]):
        top = i * SLOT_H
        bet = prog.bet
        on, off = _bar_colors(prog.status)

        name_text, stat_text, stat_x = _fit_two(
            bet.player_name, _stat_text(bet), f_name, f_val, 2, f_val, prog.display)
        w.text(canvas, f_name, 2, top + 10, w.WHITE, name_text)
        if stat_text:
            w.text(canvas, f_val, stat_x, top + 10, (150, 150, 155), stat_text)
        w.text_right(canvas, f_val, 126, top + 10, on, prog.display)

        w.progress(canvas, 2, top + 14, 124, 10, prog.fraction, rgb_on=on, rgb_off=off)

        # A busted under has a full bar, which would otherwise look like a win.
        if prog.status == BUSTED:
            w.text_right(canvas, f_val, 124, top + 22, (255, 255, 255), "BUST")

        if i == 0 and len(page) > 1:
            w.hline(canvas, 0, 127, top + SLOT_H - 2, (40, 40, 40))


def page_count(legs) -> int:
    """How many screens a parlay's legs need."""
    return max(1, -(-len(legs) // LEGS_PER_PAGE))


def draw_parlay(canvas, parlay, legs, page=None, highlight=()):
    """One parlay: header plus a line per leg.

    `page` selects which slice of legs to show. Left as None it keeps the
    rotation screen's original behaviour -- the first four legs and a
    "+N more" note -- because that screen gets one slot and can't page.
    Passed an index, it shows that page and a page counter instead; that's what
    the alert uses when it cycles the whole ticket.

    `highlight` is the set of bet ids this screen is about, drawn with a marker
    so the leg that just moved is findable without re-reading every line.
    """
    f_head = font("5x7")
    f_leg = font("4x6")

    status = parlay_status(legs)
    hit = sum(1 for p in legs if p.status == HIT)
    pages = page_count(legs)

    head_rgb = STATUS_COLOR.get(status, w.DIM)
    w.text(canvas, f_head, 2, 9, head_rgb, (parlay.name or "PARLAY").upper()[:14])
    w.text_right(canvas, f_leg, 126, 9, head_rgb, f"{hit}/{len(legs)} LEGS")
    w.hline(canvas, 0, 127, 12, (40, 40, 40))

    if page is None:
        shown = legs[:LEGS_PER_PAGE]
    else:
        page = max(0, min(page, pages - 1))
        shown = legs[page * LEGS_PER_PAGE:(page + 1) * LEGS_PER_PAGE]

    for i, prog in enumerate(shown):
        y = 23 + i * 11
        bet = prog.bet
        rgb = STATUS_COLOR.get(prog.status, w.DIM)
        moved = bet.id in highlight

        # The moved leg gets a lit band behind it. Position alone wouldn't do
        # it -- the legs are in ticket order, so it can be anywhere.
        if moved:
            w.rect(canvas, 0, y - 7, 128, 10, (38, 32, 8), fill=True)

        w.text(canvas, f_leg, 2, y, rgb, STATUS_MARK.get(prog.status, "."))
        name = w.WHITE if prog.status != BUSTED else (150, 150, 150)
        w.text(canvas, f_leg, 9, y, name,
               _fit_label(bet.player_name, _stat_text(bet),
                          f_leg, 9, f_leg, prog.display))
        w.text_right(canvas, f_leg, 126, y, rgb, prog.display)

    if page is None:
        if len(legs) > LEGS_PER_PAGE:
            w.text_right(canvas, f_leg, 126, 63, w.DIM, f"+{len(legs) - LEGS_PER_PAGE} more")
    elif pages > 1:
        _page_pips(canvas, 63 - 2, page, pages)


def _page_pips(canvas, y, page, pages):
    """Which screen of the ticket this is. Pips rather than "2/3" because the
    counter would compete with the leg values for the same corner."""
    gap = 6
    x0 = 64 - (pages * gap) // 2
    for i in range(pages):
        w.dot(canvas, x0 + i * gap, y, w.DIM if i != page else (255, 190, 0),
              filled=(i == page))


def draw_empty(canvas, sport):
    w.text(canvas, font("7x13B"), 4, 24, w.YELLOW, "BETS")
    w.text(canvas, font("5x7"), 4, 40, w.DIM, f"none for {sport.upper()}")


def _short(v: float) -> str:
    return str(int(v)) if float(v) == int(v) else f"{v:g}"


def paginate(progresses, per_page=BETS_PER_PAGE):
    return [progresses[i:i + per_page] for i in range(0, len(progresses), per_page)]
