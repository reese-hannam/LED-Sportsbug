"""Hold the panel back so it never gets ahead of a broadcast.

Broadcast delay varies wildly by how someone watches -- a couple of seconds on
an antenna, tens of seconds on a streaming box. There is no way to know which,
so it's a setting: the user dials in whatever their setup actually lags by, and
this holds EVERYTHING the panel shows back by that many seconds -- not just the
alert popup. Seeing the score change on the LED before the play has aired on the
TV is a spoiler on its own, with or without a popup.

Two independent things need delaying, so there are two small classes:

  DelayBuffer    -- for continuous state (the game itself: score, count, clock).
                    Every poll is timestamped; the display asks for whatever was
                    true `delay` seconds ago.

  DelayedRelease -- for discrete events (a scoring alert). An event is detected
                    the instant it happens, but shouldn't reach the alert queue
                    until the broadcast has had time to catch up to that moment.

Both are safe by construction rather than exact: if the delay requested is
shorter than how often the data actually updates, what's shown is however stale
the last poll made it -- never LESS delayed than asked, sometimes a bit more.
"""

import time
from collections import deque

# Bounds memory regardless of how high the delay slider goes. Must exceed the
# highest delay the UI allows (5 minutes) with margin for a slow poll interval,
# or the buffer would evict entries before they're old enough to serve.
MAX_BUFFER_SECONDS = 330.0


class DelayBuffer:
    """Timestamped snapshots of one thing; hands back whatever was current as of
    `now - delay`."""

    def __init__(self):
        self._entries: deque[tuple[float, object]] = deque()

    def clear(self) -> None:
        self._entries.clear()

    def push(self, value, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._entries.append((now, value))
        cutoff = now - MAX_BUFFER_SECONDS
        while len(self._entries) > 1 and self._entries[0][0] < cutoff:
            self._entries.popleft()

    def _entry_at(self, delay: float, now: float | None = None):
        if not self._entries:
            return None
        now = time.time() if now is None else now
        target = now - max(0.0, delay)
        result = self._entries[0]
        for entry in self._entries:
            if entry[0] > target:
                break
            result = entry
        return result

    def at(self, delay: float, now: float | None = None):
        """The value that was current `delay` seconds ago, or None if empty."""
        entry = self._entry_at(delay, now)
        return entry[1] if entry else None

    def age(self, delay: float, now: float | None = None) -> float | None:
        """How stale the returned value actually is -- for a UI to report back
        when the requested delay is finer than the data's own update rate."""
        entry = self._entry_at(delay, now)
        if entry is None:
            return None
        now = time.time() if now is None else now
        return max(0.0, now - entry[0])


class DelayedRelease:
    """Holds timestamped items until `delay` seconds have passed, then releases
    them. Delay is read at release time, not at push time, so sliding the
    setting while something is waiting takes effect immediately."""

    def __init__(self):
        self._pending: list[tuple[float, object]] = []

    def __len__(self) -> int:
        return len(self._pending)

    def push(self, item, now: float | None = None) -> None:
        self._pending.append((time.time() if now is None else now, item))

    def release(self, delay: float, now: float | None = None) -> list:
        now = time.time() if now is None else now
        ready = [item for ts, item in self._pending if now - ts >= delay]
        self._pending = [(ts, item) for ts, item in self._pending if now - ts < delay]
        return ready

    def clear(self) -> None:
        self._pending.clear()
