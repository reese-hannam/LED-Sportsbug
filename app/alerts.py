"""Alert queue: what interrupts the panel, and in what order.

Alerts arrive as MOMENTS -- everything one play caused, already in reading
order: the score, then parlay 1's legs, then parlay 2's, straight bets, then
fantasy (see moments.py). The queue's one job is to play them without ever
splitting one. Once a moment starts, every card in it shows before anything
else does -- which is what makes a play read as one play instead of a
touchdown, a return to the game, and then a second alert that looks like a
second touchdown.

Between moments, order is arrival order. Moments are detected in play order,
so first-come is also first-happened; there is no priority reshuffling that
could show the fourth quarter's parlay hit ahead of the third quarter's catch.

A backlog that outgrows what can be shown is trimmed by whole moments, routine
progress first -- a stale 4-yard catch from two minutes ago is worse than no
alert at all, but a leg landing is never quietly dropped while progress sits
ahead of it.
"""

import time
from collections import deque

from .moments import Moment

# Beyond either of these the queue is dropping moments; a panel minutes behind
# the game is actively misleading.
MAX_MOMENTS = 12
MAX_BACKLOG_SECONDS = 120.0


def game_of(event) -> str:
    """The game an alert belongs to, whatever kind of alert it is."""
    gid = getattr(event, "game_id", None)
    if gid:
        return str(gid)
    bet = getattr(event, "bet", None)
    return str(getattr(bet, "game_id", "") or "")


class Alert:
    def __init__(self, event, duration: float):
        self.event = event
        self.duration = duration
        self.started: float | None = None

    def start(self) -> None:
        self.started = time.time()

    @property
    def elapsed(self) -> float:
        return 0.0 if self.started is None else time.time() - self.started

    @property
    def expired(self) -> bool:
        return self.started is not None and self.elapsed >= self.duration


class AlertQueue:
    def __init__(self, duration: float = 8.0):
        # Used by any card with no duration of its own -- a scoring alert,
        # which picks up the alert-seconds slider this way.
        self.duration = duration
        self._moments: deque[Moment] = deque()
        self._cards: deque = deque()      # the rest of the moment on screen
        self._active: Alert | None = None

    def push(self, event) -> None:
        """A single stand-alone alert (a test, or anything without a play)."""
        self.push_moment(Moment.single(event))

    def push_all(self, events: list) -> None:
        for e in events:
            self.push(e)

    def push_moment(self, moment: Moment) -> None:
        if not moment.cards:
            return
        self._moments.append(moment)
        self._trim()

    def current(self) -> Alert | None:
        """The card that should be on screen right now, if any."""
        if self._active is not None and self._active.expired:
            self._active = None
        if self._active is None:
            if not self._cards and self._moments:
                self._cards = deque(self._moments.popleft().cards)
            if self._cards:
                card = self._cards.popleft()
                # Read at START time, not push time, so a slider change made
                # while a score waited still applies to it.
                duration = getattr(card, "duration", None) or self.duration
                self._active = Alert(card, duration)
                self._active.start()
        return self._active

    def _seconds(self, moment: Moment) -> float:
        return sum(getattr(c, "duration", None) or self.duration for c in moment.cards)

    def _trim(self) -> None:
        while self._moments and (
                len(self._moments) > MAX_MOMENTS
                or sum(self._seconds(m) for m in self._moments) > MAX_BACKLOG_SECONDS):
            victim = next((m for m in self._moments if not m.important), self._moments[0])
            self._moments.remove(victim)

    def dismiss(self) -> None:
        self._active = None

    def clear(self) -> None:
        self._active = None
        self._cards.clear()
        self._moments.clear()

    @property
    def pending_count(self) -> int:
        return len(self._cards) + sum(len(m.cards) for m in self._moments)
