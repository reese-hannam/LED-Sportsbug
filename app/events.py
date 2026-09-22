"""Turn a stream of game snapshots into discrete events.

The sources hand back whole-game state on every poll, not a change feed, so
anything event-shaped -- a score, later a turnover or a big play -- has to be
found by diffing consecutive snapshots.

Two rules matter here:
  * A game seen for the first time is seeded SILENTLY. Otherwise every restart
    mid-game would fire an alert for a touchdown that happened an hour ago.
  * Every event carries a stable `key`, so the same score can't alert twice when
    a poll returns identical data or an API briefly flaps a value back and forth.
"""

from dataclasses import dataclass, field

from .sources.base import Game, Team
from .sources.playtext import summarize_scoring, describe_score

SCORE = "score"

# Where a scoring alert sits against the fantasy and bet alerts, when one play
# fires several. Lower goes first.
#
# The game's own score LEADS its play. A touchdown is the cause; your fantasy
# quarterback throwing it and your parlay leg landing are consequences, and
# reading the consequence first ("S.SANDERS 8-YARD TD PASS", then "CLE
# TOUCHDOWN") makes the second alert look like a second score. These used to be
# tuned per domain in isolation -- fantasy touchdowns were 6 against a score's
# 10 -- which put them in exactly the wrong order.
#
# The one thing still louder is a completed parlay (priority 1), which is rare
# enough and final enough to be worth leading with.
SCORE_PRIORITY = 2          # touchdown-sized
SCORE_PRIORITY_MINOR = 3    # field goal, extra point, a run in

# Points scored -> what to call it. NFL reports the PAT as its own +1 play, so a
# touchdown usually arrives as 6 then 1 rather than 7.
NFL_LABELS = {1: "EXTRA POINT", 2: "SAFETY", 3: "FIELD GOAL", 6: "TOUCHDOWN",
              7: "TOUCHDOWN", 8: "TOUCHDOWN"}


@dataclass
class Event:
    kind: str
    game_id: str
    sport: str
    # The team that scored, and who they're playing.
    team: Team
    opponent: Team
    points: int
    label: str
    # Full context so the renderer can draw a scoreboard without a second lookup.
    away: Team
    home: Team
    period: str
    key: str
    # Game clock when it happened, and what actually happened. Both are what make
    # the alert tell you something the score line doesn't.
    clock: str = ""
    description: str = ""
    # The same thing split into two short lines that fit the panel without
    # scrolling: who scored, and how.
    scorer: str = ""
    how: str = ""
    # MLB only: who came around to score on the play.
    scored: str = ""
    priority: int = 50
    detail: dict = field(default_factory=dict)


def score_label(sport: str, points: int) -> str:
    if sport in ("nfl", "cfb"):
        return NFL_LABELS.get(points, "SCORE")
    if points == 1:
        return "RUN"
    return f"{points} RUNS"


class EventDetector:
    """Diffs successive game lists and yields events worth showing."""

    def __init__(self):
        # (sport, game_id) -> (away_score, home_score). Keyed by sport as well
        # as id because one detector now sees every active sport, and MLB game
        # ids and ESPN event ids come from unrelated numbering schemes -- they
        # don't overlap today, but nothing guarantees they won't.
        self._scores: dict[tuple[str, str], tuple[int, int]] = {}
        self._seen_keys: set[str] = set()

    def reset(self) -> None:
        """Called when the source changes -- old scores mean nothing now."""
        self._scores.clear()
        self._seen_keys.clear()

    def detect(self, games: list[Game]) -> list[Event]:
        events: list[Event] = []

        for g in games:
            current = (g.away.score, g.home.score)
            ident = (g.sport, g.id)
            previous = self._scores.get(ident)
            self._scores[ident] = current

            # First sighting: record the score, say nothing.
            if previous is None:
                continue
            if current == previous:
                continue

            last_play = g.detail.get("last_play", "")
            scorer, how = describe_score(last_play)

            for side, prev, now in (
                ("away", previous[0], current[0]),
                ("home", previous[1], current[1]),
            ):
                delta = now - prev
                # Negative deltas are stat corrections, not scores.
                if delta <= 0:
                    continue

                team = g.away if side == "away" else g.home
                opponent = g.home if side == "away" else g.away
                key = f"{g.sport}:{g.id}:{side}:{now}"
                if key in self._seen_keys:
                    continue
                self._seen_keys.add(key)

                events.append(Event(
                    kind=SCORE,
                    game_id=g.id,
                    sport=g.sport,
                    team=team,
                    opponent=opponent,
                    points=delta,
                    label=score_label(g.sport, delta),
                    away=g.away,
                    home=g.home,
                    period=g.period,
                    key=key,
                    clock=g.detail.get("clock", ""),
                    description=summarize_scoring(last_play),
                    scorer=scorer,
                    how=how,
                    priority=SCORE_PRIORITY if delta >= 6 else SCORE_PRIORITY_MINOR,
                    detail=dict(g.detail),
                ))

        return events
