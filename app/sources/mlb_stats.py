"""MLB player-prop stats -- statsapi.mlb.com.

Reuses the two techniques already established in mlb.py's BatterLines:
  - the `?fields=` query param to trim the boxscore response (full is ~170KB,
    trimmed to just the keys prop-tracking needs is far smaller)
  - a small TTL cache so tracking N props in one game costs ONE request, not N

Also adds a team-roster lookup (TTL ~1hr) to populate a player picker.
"""

import time

import requests


BOXSCORE = "https://statsapi.mlb.com/api/v1/game/{pk}/boxscore"
TEAMS = "https://statsapi.mlb.com/api/v1/teams"
ROSTER = "https://statsapi.mlb.com/api/v1/teams/{team_id}/roster"

# Same ?fields= trick as BatterLines in mlb.py. The full boxscore is ~170KB;
# this trims it to just what player_stats()/game_players() actually read.
BOX_FIELDS = (
    "teams,away,home,team,abbreviation,players,person,id,fullName,"
    "position,abbreviation,stats,batting,pitching,"
    "atBats,hits,doubles,triples,homeRuns,rbi,runs,baseOnBalls,strikeOuts,"
    "stolenBases,totalBases,plateAppearances,hitByPitch,sacFlies,"
    "earnedRuns,inningsPitched,outs,battersFaced"
)

STATS_TTL = 15.0
ROSTER_TTL = 3600.0

# statsapi uses AZ/ATH already, matching MLB_TEAMS -- but be defensive about
# common aliases anyway (e.g. ARI is a widely-seen alternate for AZ).
_ABBREV_ALIASES = {"ARI": "AZ", "OAK": "ATH"}

# See the note in nfl_stats.py for what `short`, `positions` and `components`
# are for. Baseball's split is simpler than football's: pitchers get pitching
# markets, everyone else gets batting ones.
#
# Position codes are statsapi's: P C 1B 2B 3B SS LF CF RF DH OF IF TWP.
# "TWP" is the two-way designation (Ohtani), which is why it appears on both
# sides rather than being forced into one.

_BATTERS = ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH", "OF", "IF", "TWP")
_PITCHERS = ("P", "SP", "RP", "TWP")

STAT_CATALOG = [
    # -- batting ----------------------------------------------------------
    {"key": "hits", "label": "Hits", "short": "HITS",
     "category": "batting", "positions": _BATTERS},
    {"key": "total_bases", "label": "Total Bases", "short": "TOT BASE",
     "category": "batting", "positions": _BATTERS},
    {"key": "rbi", "label": "RBI", "short": "RBI",
     "category": "batting", "positions": _BATTERS},
    {"key": "runs", "label": "Runs", "short": "RUNS",
     "category": "batting", "positions": _BATTERS},
    {"key": "home_runs", "label": "Home Runs", "short": "HR",
     "category": "batting", "positions": _BATTERS},
    {"key": "doubles", "label": "Doubles", "short": "2B",
     "category": "batting", "positions": _BATTERS},
    {"key": "triples", "label": "Triples", "short": "3B",
     "category": "batting", "positions": _BATTERS},
    {"key": "singles", "label": "Singles", "short": "1B",
     "category": "batting", "positions": _BATTERS},
    {"key": "walks", "label": "Walks", "short": "WALKS",
     "category": "batting", "positions": _BATTERS},
    {"key": "so", "label": "Strikeouts (Batter)", "short": "STRIKEO",
     "category": "batting", "positions": _BATTERS},
    {"key": "stolen_bases", "label": "Stolen Bases", "short": "STL BASE",
     "category": "batting", "positions": _BATTERS},

    # -- combined markets -------------------------------------------------
    {"key": "h_r_rbi", "label": "Hits + Runs + RBI", "short": "H+R+RBI",
     "category": "combined", "positions": _BATTERS,
     "components": ("hits", "runs", "rbi")},
    {"key": "runs_rbi", "label": "Runs + RBI", "short": "R+RBI",
     "category": "combined", "positions": _BATTERS,
     "components": ("runs", "rbi")},
    {"key": "hits_runs", "label": "Hits + Runs", "short": "HITS+R",
     "category": "combined", "positions": _BATTERS,
     "components": ("hits", "runs")},
    {"key": "hits_rbi", "label": "Hits + RBI", "short": "HITS+RBI",
     "category": "combined", "positions": _BATTERS,
     "components": ("hits", "rbi")},

    # -- pitching ---------------------------------------------------------
    {"key": "p_so", "label": "Strikeouts (Pitcher)", "short": "STRIKEO",
     "category": "pitching", "positions": _PITCHERS},
    {"key": "p_outs", "label": "Outs Recorded", "short": "OUTS",
     "category": "pitching", "positions": _PITCHERS},
    {"key": "p_earned_runs", "label": "Earned Runs", "short": "EARN RUN",
     "category": "pitching", "positions": _PITCHERS},
    {"key": "p_hits", "label": "Hits Allowed", "short": "HITS ALW",
     "category": "pitching", "positions": _PITCHERS},
    {"key": "p_walks", "label": "Walks Allowed", "short": "WALK ALW",
     "category": "pitching", "positions": _PITCHERS},
    {"key": "p_pitches", "label": "Pitches Thrown", "short": "PITCHES",
     "category": "pitching", "positions": _PITCHERS},
]

_COMBOS = {s["key"]: s["components"] for s in STAT_CATALOG if s.get("components")}


def apply_combos(stats: dict) -> dict:
    """Add the combined markets. See nfl_stats.apply_combos -- same rule: a
    missing component is zero, but all-missing stays absent."""
    for key, components in _COMBOS.items():
        present = [stats[c] for c in components if stats.get(c) is not None]
        if present:
            stats[key] = float(sum(present))
    return stats


def _outs_from_innings_pitched(ip) -> int | None:
    """'6.1' means 6 innings + 1 out, i.e. 19 outs -- NOT 6.1 innings."""
    try:
        s = str(ip)
        whole, _, frac = s.partition(".")
        return int(whole) * 3 + int(frac or 0)
    except (ValueError, TypeError):
        return None


def _batting_stats(batting: dict) -> dict[str, float]:
    if not batting:
        return {}
    out: dict[str, float] = {}

    def put(key: str, src_key: str):
        if src_key in batting and batting[src_key] is not None:
            out[key] = float(batting[src_key])

    put("hits", "hits")
    put("home_runs", "homeRuns")
    put("rbi", "rbi")
    put("runs", "runs")
    put("total_bases", "totalBases")
    put("walks", "baseOnBalls")
    put("so", "strikeOuts")
    put("stolen_bases", "stolenBases")
    put("doubles", "doubles")
    put("triples", "triples")

    # Singles aren't a direct statsapi field -- derive from hits minus
    # extra-base hits, but only when all the inputs are actually present.
    if all(k in batting and batting[k] is not None for k in ("hits", "doubles", "triples", "homeRuns")):
        out["singles"] = float(batting["hits"]) - float(batting["doubles"]) - float(batting["triples"]) - float(batting["homeRuns"])

    return out


def _pitching_stats(pitching: dict) -> dict[str, float]:
    if not pitching:
        return {}
    out: dict[str, float] = {}

    def put(key: str, src_key: str):
        if src_key in pitching and pitching[src_key] is not None:
            out[key] = float(pitching[src_key])

    put("p_so", "strikeOuts")
    put("p_earned_runs", "earnedRuns")
    put("p_hits", "hits")
    put("p_walks", "baseOnBalls")
    put("p_pitches", "numberOfPitches")

    # Prefer statsapi's own precomputed `outs` if present; otherwise derive
    # it from the "6.1" style inningsPitched string.
    if "outs" in pitching and pitching["outs"] is not None:
        out["p_outs"] = float(pitching["outs"])
    elif "inningsPitched" in pitching:
        outs = _outs_from_innings_pitched(pitching["inningsPitched"])
        if outs is not None:
            out["p_outs"] = float(outs)

    return out


def _iter_players(boxscore: dict):
    """Yield (player_dict, team_abbrev) for every player entry in a boxscore."""
    try:
        teams = boxscore.get("teams") or {}
    except AttributeError:
        return
    for side in ("away", "home"):
        team = teams.get(side) or {}
        abbrev = ((team.get("team") or {}).get("abbreviation")) or ""
        players = team.get("players") or {}
        for player in players.values():
            yield player, abbrev


def player_stats(boxscore: dict, player_id) -> dict[str, float]:
    """{stat_key: float} for one player from a parsed boxscore payload.

    Absent/empty stat blocks are skipped entirely -- never coerced to 0.
    """
    if not boxscore or player_id is None:
        return {}
    try:
        for player, _abbrev in _iter_players(boxscore):
            if str(player.get("person", {}).get("id")) != str(player_id):
                continue
            stats = player.get("stats") or {}
            result: dict[str, float] = {}
            result.update(_batting_stats(stats.get("batting") or {}))
            result.update(_pitching_stats(stats.get("pitching") or {}))
            return apply_combos(result)
        return {}
    except (AttributeError, TypeError, KeyError, ValueError):
        return {}


def game_players(boxscore: dict) -> list[dict]:
    """Everyone in the box score with at least one recorded stat category."""
    out: list[dict] = []
    if not boxscore:
        return out
    try:
        for player, abbrev in _iter_players(boxscore):
            person = player.get("person") or {}
            stats = player.get("stats") or {}
            batting = stats.get("batting") or {}
            pitching = stats.get("pitching") or {}
            categories = []
            if batting:
                categories.append("batting")
            if pitching:
                categories.append("pitching")
            if not categories:
                continue
            out.append(
                {
                    "id": person.get("id"),
                    "name": person.get("fullName", ""),
                    "team": abbrev,
                    "position": (player.get("position") or {}).get("abbreviation", ""),
                    "categories": categories,
                }
            )
    except (AttributeError, TypeError, KeyError):
        return []
    return out


class MLBPlayerStats:
    """TTL-cached boxscore fetcher for player-prop tracking.

    One cached fetch per game serves every prop being tracked for that game,
    so N props in a single game cost ONE request, exactly like BatterLines.
    """

    def __init__(self, ttl: float = STATS_TTL):
        self._ttl = ttl
        self._cache: dict[str, tuple[float, dict]] = {}
        self._session = requests.Session()

    def _boxscore(self, game_pk) -> dict:
        key = str(game_pk)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self._ttl:
            return hit[1]

        box: dict = {}
        try:
            r = self._session.get(
                BOXSCORE.format(pk=game_pk), params={"fields": BOX_FIELDS}, timeout=10
            )
            r.raise_for_status()
            box = r.json() or {}
        except (requests.RequestException, ValueError):
            # A failed lookup must never take the scoreboard down -- fall
            # back to whatever's cached (even if stale) or an empty dict.
            if hit:
                return hit[1]
            box = {}

        self._cache[key] = (time.time(), box)
        return box

    def stats_for(self, game_pk, player_id) -> dict[str, float]:
        try:
            return player_stats(self._boxscore(game_pk), player_id)
        except Exception:
            return {}

    def players_in(self, game_pk) -> list[dict]:
        try:
            return game_players(self._boxscore(game_pk))
        except Exception:
            return []


class _Roster:
    """TTL-cached team roster lookup, keyed by this project's canonical
    abbreviations (the MLB_TEAMS keys in teams.py) -- so AZ/ATH just work.
    """

    def __init__(self, ttl: float = ROSTER_TTL):
        self._ttl = ttl
        self._session = requests.Session()
        self._team_ids: dict[str, int] | None = None
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    def _abbrev_to_id(self, abbrev: str) -> int | None:
        if self._team_ids is None:
            ids: dict[str, int] = {}
            try:
                r = self._session.get(TEAMS, params={"sportId": 1}, timeout=10)
                r.raise_for_status()
                for t in r.json().get("teams", []):
                    ab = t.get("abbreviation")
                    tid = t.get("id")
                    if ab and tid is not None:
                        ids[ab] = tid
            except (requests.RequestException, ValueError):
                ids = {}
            self._team_ids = ids
        return self._team_ids.get(abbrev)

    def get(self, team_abbrev: str) -> list[dict]:
        if not team_abbrev:
            return []
        abbrev = team_abbrev.upper()
        abbrev = _ABBREV_ALIASES.get(abbrev, abbrev)

        hit = self._cache.get(abbrev)
        if hit and time.time() - hit[0] < self._ttl:
            return hit[1]

        result: list[dict] = []
        try:
            team_id = self._abbrev_to_id(abbrev)
            if team_id is None:
                self._cache[abbrev] = (time.time(), result)
                return result
            r = self._session.get(ROSTER.format(team_id=team_id), timeout=10)
            r.raise_for_status()
            for entry in r.json().get("roster", []):
                person = entry.get("person") or {}
                result.append(
                    {
                        "id": person.get("id"),
                        "name": person.get("fullName", ""),
                        "position": (entry.get("position") or {}).get("abbreviation", ""),
                    }
                )
        except (requests.RequestException, ValueError, KeyError, TypeError):
            result = []

        self._cache[abbrev] = (time.time(), result)
        return result


_roster_cache = _Roster()


def roster(team_abbrev: str) -> list[dict]:
    """[{'id','name','position'}] for a team, TTL-cached (~1hr). Accepts AZ/ATH
    (this project's canonical abbreviations from teams.py) as well as common
    aliases like ARI/OAK.
    """
    return _roster_cache.get(team_abbrev)
