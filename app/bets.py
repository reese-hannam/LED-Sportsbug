"""Player props and parlays: what was bet, and how it's going.

Deliberately sport-agnostic. A Bet names a player, a stat key, a direction and a
number; resolving that stat to a live value is the sport module's job, handed in
as a callable. That keeps this file from growing a branch per sport the way the
renderers would.

The status model is the part worth reading carefully. An over and an under are
not mirror images of each other:

  OVER   climbs toward the target and can only ever improve. Until the game ends
         it is LIVE (still possible) or HIT. It is never "missed" early -- a
         receiver on 40 yards in the first quarter has not lost anything yet.

  UNDER  is the opposite: it starts winning and can only get worse. The moment
         the value passes the target it is BUSTED, permanently and before the
         game ends. But it cannot be called a WIN until the game is final,
         because there is always another play.

Getting that asymmetry wrong is how a bet tracker ends up cheerfully showing a
green checkmark on something that is still live, so it's modelled explicitly
rather than falling out of a comparison.
"""

import math
import time
import uuid
from dataclasses import dataclass, field, asdict

OVER = "over"
UNDER = "under"

# Bet status.
LIVE = "live"        # still in play, not yet decided
HIT = "hit"          # won, settled
BUSTED = "busted"    # lost, settled
PENDING = "pending"  # game hasn't started / no stat recorded yet


def normalize_goal(direction: str, value: float) -> float:
    """A whole-number goal from whatever was typed.

    The goal is the number that has to be REACHED (over) or not passed (under),
    so it is always whole. People still type book lines out of habit, and those
    map cleanly: over 79.5 means 80 or more, under 1.5 means 1 or fewer.
    """
    value = float(value)
    return float(math.ceil(value)) if direction == OVER else float(math.floor(value))


def legacy_goal(direction: str, target: float) -> float:
    """The goal for a bet saved before goals existed, when it stored a LINE.

    The old model was strict: an over needed MORE than its line, so "over 4"
    meant five. Converting is floor(line)+1 for an over and floor(line) for an
    under -- which maps both half-point lines (79.5 -> 80, 1.5 -> 1) and whole
    ones (over 4 -> 5, under 2 -> 2) onto exactly the same outcomes they had.
    """
    target = float(target)
    return float(math.floor(target) + 1) if direction == OVER else float(math.floor(target))


@dataclass
class Bet:
    sport: str
    game_id: str
    player_id: str
    player_name: str
    team: str
    stat: str
    stat_label: str
    direction: str
    # The number to reach (over) or stay at-or-under (under), inclusive either
    # way: OVER 4 receptions hits on the fourth catch, and the progress bar is
    # full at exactly that moment. This used to be a book LINE (79.5) that the
    # value had to strictly exceed, which made "over 4" secretly mean five and
    # left a bar that filled at 80 captioned "/ 79.5".
    goal: float
    parlay_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    # When this bet stopped being able to change. Persisted, so the retention
    # clock keeps running across a power cycle rather than restarting.
    resolved_at: float | None = None

    @property
    def summary(self) -> str:
        return f"{self.player_name} {goal_text(self)} {self.stat_label}"


def goal_text(bet: "Bet") -> str:
    """"80+" for an over, "≤1" for an under -- how the goal reads to a person."""
    g = _num(bet.goal)
    return f"{g}+" if bet.direction == OVER else f"≤{g}"


@dataclass
class Parlay:
    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    # Set only when EVERY leg has resolved -- see BetBook.cleanup().
    resolved_at: float | None = None


@dataclass
class Progress:
    bet: Bet
    value: float | None       # current stat value; None = nothing recorded yet
    status: str
    fraction: float           # 0..1, how much of the target is consumed
    final: bool               # is the underlying game over

    @property
    def display(self) -> str:
        """'61 / 80' -- the line a progress bar gets captioned with. The number
        on the right is the goal itself, so the bar is full exactly when the
        left number reaches it."""
        got = "-" if self.value is None else _num(self.value)
        return f"{got} / {_num(self.bet.goal)}"


def _num(v: float) -> str:
    return str(int(v)) if float(v) == int(v) else f"{v:g}"


def evaluate(bet: Bet, value: float | None, game_final: bool) -> Progress:
    """Where this bet stands, given the player's current stat value.

    Both comparisons are INCLUSIVE of the goal: an over hits on reaching it,
    an under survives sitting on it. That's what makes the bar honest -- full
    means done, for an over, at the same instant the status flips to HIT.
    """
    goal = bet.goal

    if value is None:
        # No stat recorded yet. If the game is already over, an over never got
        # there and an under coasted home on zero.
        if game_final:
            status = BUSTED if bet.direction == OVER else HIT
        else:
            status = PENDING
        return Progress(bet, None, status, 0.0, game_final)

    if bet.direction == OVER:
        if value >= goal:
            status = HIT
        elif game_final:
            status = BUSTED
        else:
            status = LIVE
    else:
        if value > goal:
            # Gone. An under cannot recover, so this settles immediately.
            status = BUSTED
        elif game_final:
            status = HIT
        else:
            status = LIVE

    if goal > 0:
        fraction = max(0.0, min(1.0, value / goal))
    else:
        # A goal of zero: "under 0 interceptions" is spent by the first one, and
        # an over of zero is met before a snap is taken.
        fraction = 1.0 if (bet.direction == OVER or value > 0) else 0.0
    return Progress(bet, value, status, fraction, game_final)


# How long a settled bet stays on the panel and in the list before it's swept
# away. Long enough to see how it finished, short enough that tomorrow's slate
# isn't cluttered with yesterday's.
RETENTION_SECONDS = 3600.0

# Terminal statuses: a bet in one of these can never change again. HIT and
# BUSTED are both terminal by construction -- see evaluate(), which only returns
# them when the outcome is actually decided.
TERMINAL = (HIT, BUSTED)


def is_resolved(progress: "Progress | None") -> bool:
    return bool(progress and progress.status in TERMINAL)


def parlay_status(legs: list[Progress]) -> str:
    """A parlay needs every leg. One busted leg kills the whole thing."""
    if not legs:
        return PENDING
    if any(p.status == BUSTED for p in legs):
        return BUSTED
    if all(p.status == HIT for p in legs):
        return HIT
    if any(p.status in (LIVE, HIT) for p in legs):
        return LIVE
    return PENDING


class BetBook:
    """The bets themselves, plus their grouping into parlays. Persistence is
    handled by AppState, which owns config.json -- this just serializes."""

    def __init__(self):
        self.bets: list[Bet] = []
        self.parlays: list[Parlay] = []

    # -- mutation -----------------------------------------------------------

    def add(self, bet: Bet) -> Bet:
        self.bets.append(bet)
        return bet

    def remove(self, bet_id: str) -> bool:
        before = len(self.bets)
        self.bets = [b for b in self.bets if b.id != bet_id]
        removed = len(self.bets) != before
        if removed:
            self._prune_empty_parlays()
        return removed

    def _prune_empty_parlays(self) -> None:
        """Drop parlays that have no legs left.

        A parlay is always created WITH legs, so a legless one is an orphan --
        every leg deleted out from under it. It renders nowhere, which means it
        is invisible in the control center and cannot be deleted there, but it
        still sits in config.json forever. Sweeping it is the only way it ever
        goes away.
        """
        live = {b.parlay_id for b in self.bets if b.parlay_id}
        self.parlays = [p for p in self.parlays if p.id in live]

    def add_parlay(self, name: str, bet_ids: list[str]) -> Parlay:
        p = Parlay(name=name or "Parlay")
        self.parlays.append(p)
        for b in self.bets:
            if b.id in bet_ids:
                b.parlay_id = p.id
        return p

    def remove_parlay(self, parlay_id: str, drop_legs: bool = False) -> None:
        self.parlays = [p for p in self.parlays if p.id != parlay_id]
        if drop_legs:
            self.bets = [b for b in self.bets if b.parlay_id != parlay_id]
        else:
            # Ungrouped legs survive as straight bets rather than vanishing.
            for b in self.bets:
                if b.parlay_id == parlay_id:
                    b.parlay_id = None

    def clear(self) -> None:
        self.bets.clear()
        self.parlays.clear()

    # -- queries ------------------------------------------------------------

    def for_sport(self, sport: str) -> list[Bet]:
        return [b for b in self.bets if b.sport == sport]

    def legs_of(self, parlay_id: str) -> list[Bet]:
        return [b for b in self.bets if b.parlay_id == parlay_id]

    def game_ids(self, sport: str | None = None) -> set[str]:
        """Which games actually need stat lookups -- so nothing else is fetched."""
        return {b.game_id for b in self.bets if sport is None or b.sport == sport}

    # -- retention ----------------------------------------------------------

    def cleanup(self, progress_by_id: dict, now: float | None = None) -> list[str]:
        """Stamp newly-settled bets, and sweep away anything settled an hour ago.

        The multi-game parlay is the case that makes this fiddly. A parlay's legs
        routinely sit in different games: one leg can settle at 4pm while another
        is still live in a game that kicks off at 8. Deleting the settled leg --
        or the parlay -- at 5pm would destroy a ticket that is still in play. So
        a parlay is only ever stamped once EVERY leg has resolved, and a leg is
        never swept on its own; it leaves only with its parlay.

        `resolved_at` is set once and never cleared. A game that drops off the
        slate makes its bet unreadable rather than unresolved, and clearing the
        stamp would restart the clock forever.
        """
        now = time.time() if now is None else now
        removed: list[str] = []

        # --- straight bets ---------------------------------------------------
        for bet in self.bets:
            if bet.parlay_id:
                continue  # legs are governed by their parlay
            if bet.resolved_at is None and is_resolved(progress_by_id.get(bet.id)):
                bet.resolved_at = now

        # --- parlays ---------------------------------------------------------
        for parlay in self.parlays:
            legs = self.legs_of(parlay.id)
            if not legs:
                continue
            if parlay.resolved_at is None and all(
                is_resolved(progress_by_id.get(b.id)) for b in legs
            ):
                parlay.resolved_at = now

        # --- sweep -----------------------------------------------------------
        expired_parlays = {
            p.id for p in self.parlays
            if p.resolved_at is not None and now - p.resolved_at >= RETENTION_SECONDS
        }
        for pid in expired_parlays:
            removed.extend(b.id for b in self.legs_of(pid))
            self.remove_parlay(pid, drop_legs=True)

        for bet in list(self.bets):
            if bet.parlay_id:
                continue
            if bet.resolved_at is not None and now - bet.resolved_at >= RETENTION_SECONDS:
                self.remove(bet.id)
                removed.append(bet.id)

        return removed

    def retention_left(self, bet_or_parlay) -> float | None:
        """Seconds until this is swept, or None if it hasn't settled yet."""
        stamped = getattr(bet_or_parlay, "resolved_at", None)
        if stamped is None:
            return None
        return max(0.0, RETENTION_SECONDS - (time.time() - stamped))

    # -- serialization ------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "bets": [asdict(b) for b in self.bets],
            "parlays": [asdict(p) for p in self.parlays],
        }

    def load(self, data: dict) -> None:
        self.clear()
        for raw in (data or {}).get("bets", []) or []:
            try:
                raw = dict(raw)
                # Saved before goals existed, holding a book LINE under
                # `target`. Convert it to the goal that settles identically,
                # rather than dropping a live bet over a renamed field.
                if "goal" not in raw and "target" in raw:
                    raw["goal"] = legacy_goal(raw.get("direction", OVER), raw["target"])
                raw.pop("target", None)
                self.bets.append(Bet(**raw))
            except (TypeError, ValueError):
                # A config written by an older/newer version shouldn't stop boot.
                continue
        for raw in (data or {}).get("parlays", []) or []:
            try:
                self.parlays.append(Parlay(**raw))
            except (TypeError, ValueError):
                continue
        # An older config can carry a parlay whose legs are long gone.
        self._prune_empty_parlays()
