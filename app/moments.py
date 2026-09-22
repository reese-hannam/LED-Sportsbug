"""Moments: everything one play caused, played as one uninterrupted sequence.

The problem this solves: a single play used to reach the panel as several
unrelated alerts from several unrelated feeds, on several unrelated clocks.
The scoreboard said the down and distance had moved on; the bet alert for the
catch arrived 10-30 seconds later from the box score; the fantasy alert came
from the play feed on a third cadence; and nothing ordered them. Two parlays
sharing a player merged into one alert and one ticket vanished.

A MOMENT is keyed by the play itself, and holds its cards in reading order:

    score  ->  parlay 1's legs (+ its HIT)  ->  parlay 2's ...  ->  straight
    bets  ->  fantasy

Two pieces produce them:

  PlayEngine       walks each game's play list. For every NEW play it knows
                   exactly which bets moved and by how much -- the stat is
                   derived from the play (sources/nfl_playstats.py) rather than
                   waiting for the box score -- and which fantasy starters were
                   in it. The box score still reconciles the numbers; it just
                   no longer decides WHEN you hear about them.

  MomentAssembler  lets a scoring play and the scoreboard's score alert find
                   each other, so the touchdown card leads its own play's bet
                   and fantasy cards instead of trailing them or arriving on
                   its own later. It is also the one place the broadcast delay
                   is applied -- once, to the whole moment.

The queue (alerts.py) then plays moments without ever splitting one.
"""

import time
from dataclasses import dataclass

from .bets import evaluate, HIT, BUSTED
from .bet_events import (PROGRESS, LEG_HIT, BET_HIT, PARLAY_HIT, STYLE_OFF,
                         DEFAULT_PAGE_SECONDS, bet_card, parlay_hit_card,
                         sequence, add_tickets)
from .events import Event
from .fantasy_events import FantasyEventDetector, FantasyEvent, TOUCHDOWN
from .sources.nfl_playstats import deltas_with_pat, add_into, reconcile, DERIVED_STATS
from .sources.nfl_stats import apply_combos

# How long a scoring play and its score alert wait for each other. The
# scoreboard and the play feed are separate endpoints and either can lead by a
# poll or two; this is long enough to cover that and short enough that a
# score whose play never arrives isn't held noticeably.
ASSEMBLY_WINDOW = 8.0

# The extra point usually reaches the scoreboard as its own +1 a few seconds
# after the touchdown, but the play feed folds it into the touchdown's row
# ("... extra point is GOOD"). Once that touchdown's moment has been shown, a
# +1 inside this window is the PAT we already told you about, not a new score.
PAT_ABSORB_SECONDS = 45.0


@dataclass
class Moment:
    game_id: str
    key: str
    cards: list
    detected_at: float
    scoring: bool = False
    # The score AFTER this play, from the play feed -- what a scoreboard alert
    # is matched against.
    away_score: int | None = None
    home_score: int | None = None
    # Set by the poller: can a score alert for this game be expected at all?
    # (A favourite team, alerts on.) Only then is it worth waiting for one.
    expect_score: bool = False
    matched: bool = False
    text: str = ""

    @classmethod
    def single(cls, event, now: float | None = None) -> "Moment":
        """One stand-alone alert -- a test, or a score with no play behind it."""
        return cls(game_id=str(getattr(event, "game_id", "") or ""),
                   key=f"single:{getattr(event, 'key', id(event))}",
                   cards=[event], detected_at=time.time() if now is None else now)

    @property
    def important(self) -> bool:
        """Worth keeping when the queue is backed up. Routine progress isn't."""
        for c in self.cards:
            if isinstance(c, Event):
                return True
            kind = getattr(c, "kind", "")
            if kind in (LEG_HIT, BET_HIT, PARLAY_HIT):
                return True
            if isinstance(c, FantasyEvent) and kind == TOUCHDOWN:
                return True
        return False

    @property
    def carries_touchdown(self) -> bool:
        return any(isinstance(c, Event) and c.points >= 6 for c in self.cards)


class PlayEngine:
    """NFL bets and fantasy, driven play by play.

    Stateless about stats -- every step recomputes this game's lines from the
    full play list, so an edited or deleted play corrects itself -- but it
    remembers what it has already TOLD you, so nothing is announced twice.

    A game seen for the first time is seeded silently, same rule as scores:
    joining a game in the third quarter must not replay the first half.
    """

    def __init__(self):
        self._seen: dict[str, set] = {}          # game id -> play ids processed
        self._announced: dict[str, float | None] = {}   # bet id -> last value told
        self._hit: set[str] = set()              # bets whose HIT has been shown
        self._parlays_done: set[str] = set()
        self._fresh: set[str] = set()            # bets first seen this step
        self.fantasy = FantasyEventDetector()

    def following(self, game_id) -> bool:
        """Has this game's play list been processed? Its bets are then owned
        here, and the box-score baseline must not overwrite them."""
        return str(game_id) in self._seen

    def reset(self) -> None:
        self._seen.clear()
        self._announced.clear()
        self._hit.clear()
        self._parlays_done.clear()
        self._fresh.clear()
        self.fantasy.reset()

    def step(self, game, plays: list, book, *, official=None, others=None,
             game_final: bool = False, now: float | None = None,
             bet_styles=None, bet_durations=None, fantasy_book=None,
             fantasy_styles=None, fantasy_durations=None,
             parlay_context: bool = False, page_seconds: float = DEFAULT_PAGE_SECONDS):
        """Process a game's plays; return (progress for its bets, new moments).

        `plays`     every play so far, in order, team abbreviations resolved.
        `official`  (player_id, stat) -> box-score value or None.
        `others`    {bet_id: Progress} for bets in OTHER games -- a parlay's
                    legs are routinely spread across games, and "3 OF 4 HIT"
                    and a parlay completing both depend on them.
        """
        now = time.time() if now is None else now
        official = official or (lambda _pid, _stat: None)
        others = others or {}
        gid = str(game.id)
        bets = [b for b in book.bets if b.sport == "nfl" and str(b.game_id) == gid]

        seen = self._seen.get(gid)
        first = seen is None
        if first:
            seen = self._seen[gid] = set()
        # A bet added mid-game is seeded too: its first reading is its
        # starting point, not "+45 yards" out of nowhere.
        self._fresh = {b.id for b in bets if b.id not in self._announced}

        running: dict = {}

        def derived(bet):
            line = running.get(str(bet.player_id))
            if not line:
                return None
            return apply_combos(dict(line)).get(bet.stat)

        moments: list[Moment] = []
        for play in plays:
            pid = str(play.get("id") or "")
            is_new = bool(pid) and not first and pid not in seen
            before = {b.id: derived(b) for b in bets} if is_new else None
            add_into(running, deltas_with_pat(play))
            if pid:
                seen.add(pid)
            if not is_new:
                continue
            m = self._moment(game, play, pid, bets, before, derived, book, others, now,
                             bet_styles, bet_durations, fantasy_book, fantasy_styles,
                             fantasy_durations, parlay_context, page_seconds)
            if m is not None:
                moments.append(m)

        progress = [evaluate(b, reconcile(derived(b), official(b.player_id, b.stat)),
                             game_final) for b in bets]

        if first:
            self._seed(progress, book, others)
            return progress, []

        box = self._box_changes(gid, progress, book, others, now, bet_styles,
                                bet_durations, parlay_context, page_seconds)
        if box is not None:
            moments.append(box)
        self._seed([p for p in progress if p.bet.id in self._fresh], book, others)
        return progress, moments

    # -- one play -----------------------------------------------------------

    def _moment(self, game, play, pid, bets, before, derived, book, others, now,
                bet_styles, bet_durations, fantasy_book, fantasy_styles,
                fantasy_durations, parlay_context, page_seconds) -> Moment | None:
        gid = str(game.id)
        after = {b.id: derived(b) for b in bets}
        now_prog = {b.id: evaluate(b, after[b.id], False) for b in bets}
        view = dict(others)
        view.update(now_prog)

        events = []
        for b in bets:
            if b.id in self._fresh:
                continue
            v0, v1 = before[b.id], after[b.id]
            if v1 is None or v1 == v0:
                continue
            if evaluate(b, v0, False).status == BUSTED:
                continue   # an under that was already gone
            p1 = now_prog[b.id]
            delta = (v1 or 0.0) - (v0 or 0.0)
            if p1.status == HIT and b.id not in self._hit:
                kind = LEG_HIT if b.parlay_id else BET_HIT
                self._hit.add(b.id)
            elif delta > 0 and p1.status != HIT:
                kind = PROGRESS
            else:
                continue
            self._announced[b.id] = v1
            ev = bet_card(kind, b, p1, f"play:{gid}:{pid}:{b.id}", book, view,
                          bet_styles, bet_durations, before=v0, delta=delta)
            if ev.style != STYLE_OFF:
                events.append(ev)

        events.extend(self._parlay_hits(book, view, {b.id for b in bets},
                                        bet_styles, bet_durations))
        events = sequence(events, book)
        if parlay_context:
            events = add_tickets(events, book, view, page_seconds, f"play:{gid}:{pid}")

        fantasy = []
        if fantasy_book is not None:
            fantasy = self.fantasy.detect_play(play, fantasy_book, fantasy_styles,
                                               fantasy_durations)
            for f in fantasy:
                f.game_id = gid

        scoring = bool(play.get("scoring"))
        cards = events + fantasy
        # A scoring play with no cards of its own is still worth returning: the
        # assembler needs to know its play has arrived, so a waiting score
        # alert goes out now instead of sitting out the whole window.
        if not cards and not scoring:
            return None
        return Moment(game_id=gid, key=f"play:{gid}:{pid}", cards=cards,
                      detected_at=now, scoring=scoring,
                      away_score=play.get("away_score"), home_score=play.get("home_score"),
                      text=play.get("text", ""))

    def _parlay_hits(self, book, view, touched: set, styles, durations) -> list:
        """A parlay this step completed. Needs every leg, in ANY game, at HIT."""
        out = []
        for parlay in book.parlays:
            if parlay.id in self._parlays_done:
                continue
            legs = book.legs_of(parlay.id)
            if not legs or not any(b.id in touched for b in legs):
                continue
            if all(view.get(b.id) is not None and view[b.id].status == HIT for b in legs):
                self._parlays_done.add(parlay.id)
                ev = parlay_hit_card(parlay, book, view, f"parlay:{parlay.id}",
                                     styles, durations)
                if ev is not None and ev.style != STYLE_OFF:
                    out.append(ev)
        return out

    # -- the box score ------------------------------------------------------

    def _box_changes(self, gid, progress, book, others, now, styles, durations,
                     parlay_context, page_seconds) -> Moment | None:
        """What the box score changed that no play accounted for.

        Three cases, handled differently on purpose:

          * a play-driven stat the box score merely CAUGHT UP on -- updated
            silently. The play already told you; "+4 yards" out of nowhere
            seconds later would read as a second play.
          * a stat the plays can't see at all (a defender's tackles) -- the box
            score is its only source, so its changes are announced from here.
          * any bet the box score pushes to HIT that the plays hadn't -- always
            announced. A missed play must never mean a missed LEG HIT.
        """
        view = dict(others)
        view.update({p.bet.id: p for p in progress})
        events = []
        for p in progress:
            b = p.bet
            if b.id in self._fresh:
                continue
            last = self._announced.get(b.id)
            became_hit = p.status == HIT and b.id not in self._hit
            moved = p.value is not None and (last is None or p.value > last)
            if not (became_hit or moved):
                continue
            if not became_hit and (b.stat in DERIVED_STATS or p.status == HIT
                                   or evaluate(b, last, False).status == BUSTED):
                self._announced[b.id] = p.value
                continue
            kind = PROGRESS
            if became_hit:
                kind = LEG_HIT if b.parlay_id else BET_HIT
                self._hit.add(b.id)
            ev = bet_card(kind, b, p, f"box:{gid}:{b.id}:{p.value}", book, view,
                          styles, durations, before=last,
                          delta=(p.value or 0.0) - (last or 0.0))
            if p.value is not None:
                self._announced[b.id] = p.value
            if ev.style != STYLE_OFF:
                events.append(ev)

        events.extend(self._parlay_hits(book, view, {p.bet.id for p in progress},
                                        styles, durations))
        if not events:
            return None
        events = sequence(events, book)
        if parlay_context:
            events = add_tickets(events, book, view, page_seconds, f"box:{gid}:{now:.3f}")
        return Moment(game_id=gid, key=f"box:{gid}:{now:.3f}", cards=events, detected_at=now)

    def _seed(self, progress, book, others) -> None:
        """Record where things stand without announcing any of it."""
        view = dict(others)
        view.update({p.bet.id: p for p in progress})
        for p in progress:
            self._announced[p.bet.id] = p.value
            if p.status == HIT:
                self._hit.add(p.bet.id)
        for parlay in book.parlays:
            legs = book.legs_of(parlay.id)
            if legs and all(view.get(b.id) is not None and view[b.id].status == HIT
                            for b in legs):
                self._parlays_done.add(parlay.id)


class MomentAssembler:
    """Holds moments and score alerts until they're due, pairing them up.

    A scoring play's moment and the scoreboard's score alert arrive from
    different endpoints in either order. Whichever comes first waits -- up to
    ASSEMBLY_WINDOW -- for the other, and once matched the score card goes to
    the FRONT of the moment, so a touchdown reads: TOUCHDOWN, then what it did
    to your parlays, then your fantasy team.

    This is also the only place the broadcast delay applies: a moment is held
    until `delay` seconds after it was detected, as one unit.
    """

    def __init__(self, window: float = ASSEMBLY_WINDOW):
        self.window = window
        self._moments: list[Moment] = []
        # (detected_at, event, hold): `hold` means a scoring play for this game
        # is being tracked, so it's worth waiting for one to pair with.
        self._scores: list[tuple] = []
        # Recently released touchdown moments: (released_at, game, away, home).
        self._released_tds: list[tuple] = []

    def __len__(self) -> int:
        return len(self._moments) + len(self._scores)

    def clear(self) -> None:
        self._moments.clear()
        self._scores.clear()
        self._released_tds.clear()

    def add_moment(self, m: Moment) -> None:
        self._moments.append(m)
        # A score that got here first pairs up immediately.
        for entry in list(self._scores):
            if self._matches(m, entry[1]):
                self._attach(m, entry[1])
                self._scores.remove(entry)

    def add_score(self, ev, now: float | None = None, hold: bool = True) -> None:
        now = time.time() if now is None else now
        if self._absorbed(ev, now):
            return
        m = next((m for m in self._moments if self._matches(m, ev)), None)
        if m is not None:
            self._attach(m, ev)
            return
        self._scores.append((now, ev, hold))

    def release(self, delay: float, now: float | None = None) -> list[Moment]:
        """Everything due, oldest first. Moments are never split or merged here."""
        now = time.time() if now is None else now
        wait_for_pair = max(delay, self.window)
        out: list[Moment] = []

        for m in list(self._moments):
            age = now - m.detected_at
            if age < delay:
                continue
            # A scoring play for a game whose score WILL alert waits for it,
            # so the score leads rather than trailing a moment later.
            if m.scoring and m.expect_score and not m.matched and age < wait_for_pair:
                continue
            self._moments.remove(m)
            if m.carries_touchdown:
                self._released_tds.append((now, m.game_id, m.away_score, m.home_score))
            if m.cards:
                out.append(m)

        for entry in list(self._scores):
            ts, ev, hold = entry
            if now - ts < (wait_for_pair if hold else delay):
                continue
            self._scores.remove(entry)
            if self._absorbed(ev, now):
                continue
            out.append(Moment(game_id=str(ev.game_id), key=f"score:{ev.key}",
                              cards=[ev], detected_at=ts))

        self._released_tds = [r for r in self._released_tds
                              if now - r[0] < PAT_ABSORB_SECONDS]
        # Stable: moments from one play-list step share a timestamp and keep
        # play order.
        out.sort(key=lambda m: m.detected_at)
        return out

    # -- pairing ------------------------------------------------------------

    @staticmethod
    def _matches(m: Moment, ev) -> bool:
        """Is `ev` the scoreboard's view of the play in `m`?

        Same game, and the play's post-play score already contains the event's
        new score. That covers the usual split -- the scoreboard reports the
        touchdown as +6 and the PAT as +1 a moment later, while the play row
        already carries +7.
        """
        if not (m.scoring and m.game_id == str(ev.game_id)):
            return False
        if m.away_score is None or m.home_score is None:
            return False
        return m.away_score >= ev.away.score and m.home_score >= ev.home.score

    @staticmethod
    def _attach(m: Moment, ev) -> None:
        scores = [c for c in m.cards if isinstance(c, Event)]
        # A second, smaller score on a play that already has its card is the
        # PAT that play's own row already describes.
        if scores and ev.points < 6:
            m.matched = True
            return
        m.cards.insert(len(scores), ev)
        m.matched = True

    def _absorbed(self, ev, now: float) -> bool:
        """A PAT covered by a touchdown moment that has already been shown."""
        if ev.points >= 6:
            return False
        for ts, gid, away, home in self._released_tds:
            if (gid == str(ev.game_id) and now - ts < PAT_ABSORB_SECONDS
                    and away is not None and away >= ev.away.score
                    and home is not None and home >= ev.home.score):
                return True
        return False
