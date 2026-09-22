"""Turn changes in bet progress into alert cards.

Same two rules as the score detector: a bet seen for the first time is seeded
SILENTLY (otherwise every restart would replay a whole afternoon of progress),
and every card carries a stable key so a repeated poll can't fire it twice.

Four tiers, deliberately different in weight:

  PROGRESS    the number moved but nothing is decided.
  LEG_HIT     a leg of a parlay landed; the parlay as a whole is still open.
  BET_HIT     a straight bet landed. Settled, and drawn distinct from a leg.
  PARLAY_HIT  every leg in. The loudest thing the panel does.

ONE CARD PER LEG, in ticket order, one parlay at a time. This replaced merging
same-parlay legs into a single alert, which had two failures in real use:

  * Two parlays with overlapping players -- McCaffrey 80+ rush+rec yards on one
    ticket and 100+ on another -- read as one alert showing the first ticket's
    bar, so the second parlay looked like it wasn't being tracked at all.
  * A play that moved several legs showed one of them in full and the rest as
    a caption, when the whole point is seeing each ticket move.

So a moment runs parlay 1's legs, then parlay 1's HIT if it just completed,
then parlay 2, then straight bets -- see sequence(). Every card names its
ticket, so which parlay you're looking at is never a guess.
"""

from dataclasses import dataclass, field

from .bets import Bet, Parlay, Progress, HIT, LIVE, PENDING

PROGRESS = "bet_progress"
LEG_HIT = "leg_hit"
BET_HIT = "bet_hit"
PARLAY_HIT = "parlay_hit"
# Not a user-facing category: the whole ticket, paged, shown once after a
# parlay's run of leg cards when "show the whole parlay" is on.
PARLAY_TICKET = "parlay_ticket"

CATEGORIES = [PARLAY_HIT, BET_HIT, LEG_HIT, PROGRESS]

CATEGORY_LABEL = {
    PARLAY_HIT: "Parlay hits",
    BET_HIT: "Single bet hits",
    LEG_HIT: "Parlay leg hits",
    PROGRESS: "Progress made",
}

PRIORITY = {PARLAY_HIT: 1, BET_HIT: 5, LEG_HIT: 8, PROGRESS: 80, PARLAY_TICKET: 81}
# Seconds per card. A leg card has to be readable -- ticket name, player, stat,
# bar -- but a big play can move half a dozen legs across two tickets, and
# they run back to back, so a routine one can't linger.
DURATION = {PARLAY_HIT: 9.0, BET_HIT: 5.5, LEG_HIT: 5.0, PROGRESS: 3.5}
MIN_DURATION = 3.0
MAX_DURATION = 20.0

# Legs per whole-parlay screen. Four is what fits at 128x64 while staying
# readable across a room; a fifth line would mean a smaller font.
LEGS_PER_PAGE = 4
DEFAULT_PAGE_SECONDS = 4.0

STYLE_FULL = "full"
STYLE_BANNER = "banner"
STYLE_OFF = "off"

# Full screen for everything: a card has to carry the ticket's name, and a
# sixteen-pixel strip has nowhere to put it next to the player and the bar.
DEFAULT_STYLES = {
    PARLAY_HIT: STYLE_FULL,
    BET_HIT: STYLE_FULL,
    LEG_HIT: STYLE_FULL,
    PROGRESS: STYLE_FULL,
}


@dataclass
class BetEvent:
    kind: str
    bet: Bet
    progress: Progress
    key: str
    delta: float = 0.0
    parlay: Parlay | None = None
    legs_hit: int = 0
    legs_total: int = 0
    # Where this leg sits on its ticket, 1-based -- "LEG 2 OF 6".
    leg_no: int = 0
    # The value BEFORE this change, so the bar animates from where it was
    # rather than refilling from empty on every card.
    before: float | None = None
    priority: int = 50
    duration: float = 5.0
    style: str = STYLE_FULL
    # Bottom-bar cards draw over whatever is showing rather than replacing it.
    overlay: bool = False
    # PARLAY_TICKET only: every leg of the ticket, paged LEGS_PER_PAGE to a
    # screen, each page held `page_seconds`.
    parlay_legs: list = field(default_factory=list)
    page_seconds: float = 0.0
    detail: dict = field(default_factory=dict)

    @property
    def context_pages(self) -> int:
        """How many screens a ticket card pages through."""
        if not self.parlay_legs:
            return 0
        return -(-len(self.parlay_legs) // LEGS_PER_PAGE)   # ceil

    def moved_ids(self) -> set:
        """Bet ids this card is about, for highlighting on a ticket card."""
        return set(self.detail.get("moved_ids") or [self.bet.id])

    @property
    def label(self) -> str:
        return {
            PROGRESS: "BET PROGRESS",
            LEG_HIT: "LEG HIT",
            BET_HIT: "BET HIT",
            PARLAY_HIT: "PARLAY HIT",
            PARLAY_TICKET: "PARLAY",
        }.get(self.kind, "BET")


def _make(kind: str, bet: Bet, prog: Progress, key: str,
          styles: dict | None = None, durations: dict | None = None, **kw) -> BetEvent:
    styles = {**DEFAULT_STYLES, **(styles or {})}
    durations = {**DURATION, **(durations or {})}
    style = styles.get(kind, STYLE_FULL)
    return BetEvent(
        kind=kind, bet=bet, progress=prog, key=key,
        priority=PRIORITY.get(kind, 50),
        duration=durations.get(kind, 5.0),
        style=style,
        overlay=(style == STYLE_BANNER),
        **kw,
    )


def bet_card(kind: str, bet: Bet, prog: Progress, key: str, book, by_bet: dict,
             styles: dict | None = None, durations: dict | None = None,
             before: float | None = None, delta: float = 0.0) -> BetEvent:
    """One card about one bet, with its ticket context filled in.

    `by_bet` is the freshest progress for EVERY bet, not just the ones in this
    game -- a parlay's other legs are routinely in other games, and "2 OF 4
    HIT" has to count them.
    """
    parlay = None
    legs_hit = legs_total = leg_no = 0
    if bet.parlay_id:
        parlay = next((p for p in book.parlays if p.id == bet.parlay_id), None)
        legs = book.legs_of(bet.parlay_id)
        legs_total = len(legs)
        legs_hit = sum(1 for b in legs
                       if by_bet.get(b.id) is not None and by_bet[b.id].status == HIT)
        leg_no = next((i + 1 for i, b in enumerate(legs) if b.id == bet.id), 0)
    return _make(kind, bet, prog, key, styles=styles, durations=durations,
                 parlay=parlay, legs_hit=legs_hit, legs_total=legs_total,
                 leg_no=leg_no, before=before, delta=delta)


def parlay_hit_card(parlay: Parlay, book, by_bet: dict, key: str,
                    styles: dict | None = None, durations: dict | None = None):
    legs = book.legs_of(parlay.id)
    tracked = [by_bet[b.id] for b in legs if b.id in by_bet]
    if not tracked:
        return None
    last = tracked[-1]
    return _make(PARLAY_HIT, last.bet, last, key, styles=styles, durations=durations,
                 parlay=parlay, legs_hit=len(legs), legs_total=len(legs))


def sequence(events: list[BetEvent], book) -> list[BetEvent]:
    """Order one moment's bet cards the way the ticket reads.

    Parlays in the order they were created, each one's legs in ticket order,
    followed by that parlay's own HIT when this moment completed it -- so a
    ticket plays through start to finish before the next begins. Straight bets
    come after every parlay. Sorting is stable, so two cards about the same
    leg (never expected, but cheap to guarantee) keep their arrival order.
    """
    parlay_rank = {p.id: i for i, p in enumerate(book.parlays)}
    leg_rank: dict[str, int] = {}
    for p in book.parlays:
        for i, b in enumerate(book.legs_of(p.id)):
            leg_rank[b.id] = i
    bet_rank = {b.id: i for i, b in enumerate(book.bets)}

    def rank(e: BetEvent):
        pid = e.parlay.id if e.parlay else e.bet.parlay_id
        if pid in parlay_rank:
            tail = {PARLAY_HIT: 1, PARLAY_TICKET: 2}.get(e.kind, 0)
            return (0, parlay_rank[pid], tail, leg_rank.get(e.bet.id, 0))
        return (1, 0, 0, bet_rank.get(e.bet.id, 0))

    return sorted(events, key=rank)


def add_tickets(events: list[BetEvent], book, by_bet: dict, page_seconds: float,
                key_prefix: str) -> list[BetEvent]:
    """After each parlay's run of cards, the whole ticket -- once.

    Knowing a leg moved is half of what you want; the other half is whether the
    ticket is still alive. This used to follow EVERY leg alert with the full
    ticket, which with one card per leg would repeat the same ticket after each
    leg of it. Now it closes the parlay's run instead.

    Skipped when the run ended in a PARLAY_HIT (that screen already lists every
    leg), when none of the run was full-screen (a bottom bar followed by a
    full-screen ticket would ambush the game you were watching), and for
    one-leg parlays, whose "whole ticket" is the card you just saw.
    """
    out: list[BetEvent] = []
    run: list[BetEvent] = []

    def close_run():
        if not run:
            return
        pid = run[0].parlay.id if run[0].parlay else run[0].bet.parlay_id
        parlay = run[0].parlay
        legs = book.legs_of(pid) if pid else []
        if (parlay is not None and len(legs) >= 2
                and not any(e.kind == PARLAY_HIT for e in run)
                and any(e.style == STYLE_FULL for e in run)):
            progress = [by_bet[b.id] for b in legs if b.id in by_bet]
            if progress:
                ticket = _make(PARLAY_TICKET, run[0].bet, run[0].progress,
                               f"{key_prefix}:ticket:{pid}",
                               parlay=parlay, parlay_legs=progress,
                               page_seconds=page_seconds, legs_total=len(legs),
                               legs_hit=sum(1 for p in progress if p.status == HIT))
                ticket.duration = ticket.context_pages * page_seconds
                ticket.detail["moved_ids"] = [e.bet.id for e in run]
                out.append(ticket)
        run.clear()

    current = None
    for e in events:
        pid = e.parlay.id if e.parlay else e.bet.parlay_id
        if pid != current:
            close_run()
            current = pid
        out.append(e)
        if pid:
            run.append(e)
    close_run()
    return out


class BetEventDetector:
    """Box-score-driven detection, for bets with no play feed behind them.

    Baseball, and any NFL stat the plays can't derive, arrive as snapshots --
    so this diffs one snapshot against the last. NFL props the plays DO cover
    go through moments.PlayEngine instead, which can tie a change to the play
    that caused it.

    Fed REAL-TIME progress. It used to read AppState.bet_progress, which is
    already held back by the broadcast delay, and then its output was held back
    again on the way to the queue -- so bet alerts ran two delays behind the
    game while score alerts ran one.
    """

    def __init__(self):
        # bet id -> (value, status)
        self._last: dict[str, tuple] = {}
        # parlay id -> status, so a parlay only celebrates once
        self._parlays: dict[str, str] = {}
        self._seen: set[str] = set()

    def reset(self) -> None:
        self._last.clear()
        self._parlays.clear()
        self._seen.clear()

    def detect(self, progresses: list[Progress], book,
               styles: dict | None = None, durations: dict | None = None,
               parlay_context: bool = False,
               page_seconds: float = DEFAULT_PAGE_SECONDS,
               by_bet: dict | None = None) -> list[BetEvent]:
        """Cards for whatever changed since the last call, in ticket order.

        `by_bet` is progress for every bet across sports, so a parlay mixing
        leagues counts its legs correctly; it defaults to just `progresses`.
        """
        events: list[BetEvent] = []
        view = dict(by_bet or {})
        view.update({p.bet.id: p for p in progresses})

        for prog in progresses:
            bet = prog.bet
            current = (prog.value, prog.status)
            previous = self._last.get(bet.id)
            self._last[bet.id] = current

            if previous is None:
                continue  # first sighting: record, say nothing
            prev_value, prev_status = previous
            if current == previous:
                continue

            if prog.status == HIT and prev_status != HIT:
                kind = LEG_HIT if bet.parlay_id else BET_HIT
                ev = bet_card(kind, bet, prog, f"hit:{bet.id}", book, view,
                              styles, durations, before=prev_value,
                              delta=(prog.value or 0) - (prev_value or 0))
            elif prog.value is not None and prog.status in (LIVE, PENDING):
                delta = prog.value - (prev_value or 0.0)
                if delta <= 0:
                    continue
                # Keyed on the value reached, so the same gain can't re-fire.
                ev = bet_card(PROGRESS, bet, prog, f"prog:{bet.id}:{prog.value}", book,
                              view, styles, durations, before=prev_value, delta=delta)
            else:
                continue
            if self._fresh(ev.key):
                events.append(ev)

        events.extend(self._parlay_events(book, view, styles, durations))
        events = [e for e in events if e.style != STYLE_OFF]
        events = sequence(events, book)
        if parlay_context:
            events = add_tickets(events, book, view, page_seconds, "box")
        return events

    def _parlay_events(self, book, view, styles=None, durations=None) -> list[BetEvent]:
        """A parlay completing is its own, bigger card than its last leg."""
        out = []
        for parlay in book.parlays:
            legs = book.legs_of(parlay.id)
            tracked = [view[b.id] for b in legs if b.id in view]
            if not legs or len(tracked) != len(legs):
                continue

            status = "hit" if all(p.status == HIT for p in tracked) else "open"
            previous = self._parlays.get(parlay.id)
            self._parlays[parlay.id] = status

            if previous is None or status != "hit" or previous == "hit":
                continue
            ev = parlay_hit_card(parlay, book, view, f"parlay:{parlay.id}",
                                 styles, durations)
            if ev and self._fresh(ev.key):
                out.append(ev)
        return out

    def _fresh(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True
