"""Resolve every bet against live player stats.

The per-sport modules do the fetching and know their own field names; this just
picks the right one and turns a stat value into a Progress. Both providers cache
per game, so N props in the same game cost ONE request, not N -- which is the
whole reason bets are grouped by game here rather than fetched one at a time.

This is the BOX-SCORE side of bet tracking. It drives baseball props outright,
and for NFL it is the reconciling source: NFL props are driven play by play
(sources/nfl_playstats.py, moments.PlayEngine), so an alert lands with the play
that caused it, and box_value() is what that engine checks its numbers against.

Replay never reads the box score. A recording only carries the FINAL one, so
tracking against it would settle every bet the instant a replay started. NFL
props in replay come from the recorded plays instead, which is what makes a
replay a faithful rehearsal of a live game's bet alerts.
"""

from .bets import Progress, evaluate
from .sources.base import FINAL
from .sources.mlb_stats import MLBPlayerStats
from .sources.nfl_stats import NFLPlayerStats


class BetTracker:
    def __init__(self):
        self._nfl = NFLPlayerStats()
        self._mlb = MLBPlayerStats()

    def _stats_for(self, sport: str, game_id: str, player_id: str) -> dict:
        try:
            if sport == "nfl":
                return self._nfl.stats_for(game_id, player_id) or {}
            if sport == "mlb":
                return self._mlb.stats_for(game_id, player_id) or {}
        except Exception:
            # A stat lookup must never be able to take the scoreboard down.
            pass
        return {}

    def box_value(self, sport: str, game_id: str, player_id: str, stat: str):
        """One stat off the cached box score -- the reconciling side of NFL
        props, which are otherwise driven play by play (see moments.py)."""
        return self._stats_for(sport, game_id, player_id).get(stat)

    def progress(self, book, games, sport: str | None = None) -> list[Progress]:
        by_id = {g.id: g for g in games}
        out: list[Progress] = []

        for bet in book.bets:
            if sport and bet.sport != sport:
                continue
            game = by_id.get(bet.game_id)
            final = bool(game and game.state == FINAL)
            value = self._stats_for(bet.sport, bet.game_id, bet.player_id).get(bet.stat)
            out.append(evaluate(bet, value, final))

        return out

    def players_in(self, sport: str, game_id: str) -> list[dict]:
        """Everyone with recorded stats in a game -- for the picker."""
        try:
            if sport == "nfl":
                return self._nfl.players_in(game_id) or []
            if sport == "mlb":
                return self._mlb.players_in(game_id) or []
        except Exception:
            pass
        return []


def roster(sport: str, team_abbrev: str) -> list[dict]:
    """Full team roster for the picker's "other player" escape hatch."""
    try:
        if sport == "nfl":
            from .sources.nfl_stats import roster as nfl_roster
            return nfl_roster(team_abbrev) or []
        if sport == "mlb":
            from .sources.mlb_stats import roster as mlb_roster
            return mlb_roster(team_abbrev) or []
    except Exception:
        pass
    return []


def curated_roster(sport: str, team_abbrev: str) -> list[dict]:
    """The players people actually bet on, grouped by position -- starters at
    the skill positions plus the kicker and pass rushers, not a full 53-man
    roster. NFL only, since that's where roster depth actually matters for
    props; MLB's lineup is small enough that the full roster already works."""
    try:
        if sport == "nfl":
            from .sources.nfl_stats import curated_roster as nfl_curated
            return nfl_curated(team_abbrev) or []
    except Exception:
        pass
    return []


def _catalog(sport: str) -> list[dict]:
    if sport == "nfl":
        from .sources.nfl_stats import STAT_CATALOG
        return STAT_CATALOG
    if sport == "mlb":
        from .sources.mlb_stats import STAT_CATALOG
        return STAT_CATALOG
    return []


def stat_catalog(sport: str, position: str | None = None) -> list[dict]:
    """Every bettable stat, optionally narrowed to one roster position.

    Nobody writes a prop on a quarterback's extra points, and wading past them
    to reach passing yards is the whole complaint. So the picker asks for the
    stats that belong to the position it's showing.

    An UNKNOWN position returns everything rather than nothing. Position codes
    come from a roster feed, so a new or odd one is always possible -- and a
    picker that silently offers no stats at all is a far worse failure than one
    that offers a few too many.
    """
    catalog = _catalog(sport)
    if not position:
        return catalog
    pos = str(position).upper()
    known = {p for s in catalog for p in s.get("positions", ())}
    if pos not in known:
        return catalog
    return [s for s in catalog if pos in s.get("positions", ())]


def stat_positions(sport: str) -> list[str]:
    """Every position code the catalog knows about, for the UI to reason with."""
    return sorted({p for s in _catalog(sport) for p in s.get("positions", ())})


def stat_short(sport: str, key: str) -> str:
    """Panel label for a stat key -- what the LED shows instead of the line.

    Falls back to the key itself so a bet placed under an older build still
    renders something meaningful rather than a blank column.
    """
    for s in _catalog(sport):
        if s["key"] == key:
            return s.get("short") or s["label"].upper()
    return key.replace("_", " ").upper()
