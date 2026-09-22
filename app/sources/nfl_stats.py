"""NFL per-player stats and rosters -- ESPN's public box score / roster endpoints.

Why this exists: the scoreboard is growing player-prop tracking ("Jerry Jeudy
over 79.5 receiving yards"). That needs two things ESPN's game-summary payload
doesn't hand you directly: (1) a stable, sport-wide catalog of "prop-able"
stats so the UI can offer a consistent picker regardless of position, and
(2) per-player numbers pulled out of ESPN's box score, which reports each stat
category as a list of column labels plus a parallel list of string values per
athlete -- including compound cells like "15/24" (completions/attempts) and
"0-0" (sacks-yards) that have to be split rather than treated as one number.

A prop tracker polls this continuously for however many players are being
watched in a game. Without caching, N tracked props in one game would mean N
requests every poll; instead the parsed summary is cached per event_id so
tracking an entire game's worth of props costs ONE request.
"""

import time

import requests

SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
ROSTER = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team}/roster"
DEPTHCHART = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team}/depthcharts"

# Per-game summary is good for this long before it's refetched. Long enough
# that tracking several props in one game costs one request per cycle, not one
# per prop; short enough that a live stat update shows up quickly.
SUMMARY_TTL = 20.0

# Rosters change on the order of days (trades, cuts, signings), not seconds --
# an hour keeps the player picker fresh without hammering ESPN every load.
ROSTER_TTL = 3600.0

# How long an EMPTY result is remembered. Deliberately short: an empty roster
# is almost never true -- it's a timeout or a hiccup -- and caching it for the
# full hour is exactly how one failed request mid-game left a team with "no
# roster found" in the bet form until the hour ran out. Long enough to stop a
# genuinely-down endpoint being hammered on every keystroke, no longer.
EMPTY_TTL = 30.0


def _fresh(entry, ttl: float) -> bool:
    """Is a cached (fetched_at, value) still good? Empty values expire fast."""
    if not entry:
        return False
    fetched_at, value = entry
    return time.time() - fetched_at < (ttl if value else EMPTY_TTL)

# One entry per prop-able stat. Keys are stable and snake_case -- they are
# what gets stored alongside a tracked prop, so they must never be renamed.
# Every stat you can bet on, what to call it, and who it applies to.
#
# Three fields earn their place beyond key/label:
#
#   short      what the LED panel shows. The panel used to print the over/under
#              line instead ("O200"), which at 4x6 reads as "0200" -- and the
#              target is already spelled out in the value column ("74 / 200"),
#              so the line was the one thing on the row that was redundant. The
#              stat is what you actually can't infer.
#   positions  which roster positions the stat is offered for. A quarterback has
#              no business being offered "Extra Points Made".
#   components a combined market -- summed from other keys rather than read from
#              the box score. "Rush + Rec Yards" is one of the most-bet markets
#              there is and no feed reports it as a field.
#
# Position codes are ESPN's own, from the team roster endpoint: QB RB FB WR TE
# PK P LS OT G C DE DT LB CB S. The curated depth-chart roster says "K" where
# the full roster says "PK", so both are listed wherever kicking applies.

_OFF_SKILL = ("RB", "FB", "WR", "TE")
_RUSHERS = ("QB", "RB", "FB", "WR")
_DEFENDERS = ("DE", "DT", "LB", "CB", "S")
_KICKERS = ("PK", "K")

STAT_CATALOG = [
    # -- passing ----------------------------------------------------------
    {"key": "pass_yds", "label": "Passing Yards", "short": "PASS YDS",
     "category": "passing", "positions": ("QB",)},
    {"key": "pass_td", "label": "Passing TDs", "short": "PASS TD",
     "category": "passing", "positions": ("QB",)},
    {"key": "pass_cmp", "label": "Completions", "short": "COMP",
     "category": "passing", "positions": ("QB",)},
    {"key": "pass_att", "label": "Pass Attempts", "short": "PASS ATT",
     "category": "passing", "positions": ("QB",)},
    {"key": "pass_int", "label": "Interceptions Thrown", "short": "INT",
     "category": "passing", "positions": ("QB",)},
    {"key": "pass_sacked", "label": "Times Sacked", "short": "SACKED",
     "category": "passing", "positions": ("QB",)},

    # -- rushing ----------------------------------------------------------
    {"key": "rush_yds", "label": "Rushing Yards", "short": "RUSH YDS",
     "category": "rushing", "positions": _RUSHERS},
    {"key": "rush_att", "label": "Rushing Attempts", "short": "CARRIES",
     "category": "rushing", "positions": _RUSHERS},
    {"key": "rush_td", "label": "Rushing TDs", "short": "RUSH TD",
     "category": "rushing", "positions": _RUSHERS},
    {"key": "rush_long", "label": "Longest Rush", "short": "LNG RUSH",
     "category": "rushing", "positions": _RUSHERS},

    # -- receiving --------------------------------------------------------
    {"key": "rec", "label": "Receptions", "short": "RECS",
     "category": "receiving", "positions": _OFF_SKILL},
    {"key": "rec_yds", "label": "Receiving Yards", "short": "REC YDS",
     "category": "receiving", "positions": _OFF_SKILL},
    {"key": "rec_td", "label": "Receiving TDs", "short": "REC TD",
     "category": "receiving", "positions": _OFF_SKILL},
    {"key": "rec_tgts", "label": "Targets", "short": "TARGETS",
     "category": "receiving", "positions": _OFF_SKILL},
    {"key": "rec_long", "label": "Longest Reception", "short": "LNG REC",
     "category": "receiving", "positions": _OFF_SKILL},

    # -- combined markets -------------------------------------------------
    {"key": "rush_rec_yds", "label": "Rushing + Receiving Yards",
     "short": "RU+RE YD", "category": "combined", "positions": _OFF_SKILL,
     "components": ("rush_yds", "rec_yds")},
    {"key": "pass_rush_yds", "label": "Passing + Rushing Yards",
     "short": "PA+RU YD", "category": "combined", "positions": ("QB",),
     "components": ("pass_yds", "rush_yds")},
    {"key": "total_yds", "label": "Pass + Rush + Rec Yards",
     "short": "TOT YDS", "category": "combined",
     "positions": ("QB",) + _OFF_SKILL,
     "components": ("pass_yds", "rush_yds", "rec_yds")},
    # Anytime TD: the single most-bet touchdown market, and deliberately NOT
    # "total TDs" under another name. It settles on ANY touchdown the player
    # himself scores -- run, catch, kick or punt return, fumble return -- and a
    # quarterback's PASSING touchdowns never count toward it, because he threw
    # them rather than scored them. `fixed_goal` is there because the only
    # question the bet asks is "at least one?": the form doesn't ask for a
    # number, and a bar reading "0 / 1" says everything.
    #
    # def_td rather than int_td for the defensive side: ESPN counts a pick six
    # in BOTH the interceptions and the defensive tables, and summing both
    # would show one touchdown as two.
    {"key": "anytime_td", "label": "Anytime TD", "short": "ANY TD",
     "category": "touchdowns", "positions": ("QB",) + _OFF_SKILL,
     "components": ("rush_td", "rec_td", "kr_td", "pr_td", "def_td"),
     "fixed_goal": 1},
    {"key": "total_td", "label": "Rushing + Receiving TDs", "short": "TOT TD",
     "category": "combined", "positions": _OFF_SKILL,
     "components": ("rush_td", "rec_td")},
    {"key": "pass_rush_td", "label": "Passing + Rushing TDs", "short": "TOT TD",
     "category": "combined", "positions": ("QB",),
     "components": ("pass_td", "rush_td")},

    # -- kicking ----------------------------------------------------------
    {"key": "fg_made", "label": "Field Goals Made", "short": "FG MADE",
     "category": "kicking", "positions": _KICKERS},
    {"key": "xp_made", "label": "Extra Points Made", "short": "XP MADE",
     "category": "kicking", "positions": _KICKERS},
    {"key": "kick_pts", "label": "Kicking Points", "short": "KICK PTS",
     "category": "kicking", "positions": _KICKERS},
    {"key": "fg_long", "label": "Longest Field Goal", "short": "LNG FG",
     "category": "kicking", "positions": _KICKERS},

    # -- defense ----------------------------------------------------------
    {"key": "def_tackles", "label": "Tackles + Assists", "short": "TACKLES",
     "category": "defense", "positions": _DEFENDERS},
    {"key": "def_solo", "label": "Solo Tackles", "short": "SOLO TKL",
     "category": "defense", "positions": _DEFENDERS},
    {"key": "def_sacks", "label": "Sacks", "short": "SACKS",
     "category": "defense", "positions": _DEFENDERS},
    {"key": "def_tfl", "label": "Tackles For Loss", "short": "TFL",
     "category": "defense", "positions": _DEFENDERS},
    {"key": "def_pd", "label": "Passes Defended", "short": "PASS DEF",
     "category": "defense", "positions": _DEFENDERS},
    {"key": "def_int", "label": "Interceptions", "short": "INT",
     "category": "defense", "positions": _DEFENDERS},

    # -- misc -------------------------------------------------------------
    {"key": "fum_lost", "label": "Fumbles Lost", "short": "FUM LOST",
     "category": "misc", "positions": ("QB",) + _OFF_SKILL},
]

# label -> stat key, per box-score category. Only labels we actually track are
# listed; the rest (AVG, PCT, RTG, QBR, ...) are averages or ratings, which are
# not things a prop is written on.
_LABEL_MAP = {
    "passing": {"YDS": "pass_yds", "TD": "pass_td", "INT": "pass_int"},
    "rushing": {"CAR": "rush_att", "YDS": "rush_yds", "TD": "rush_td",
                "LONG": "rush_long"},
    "receiving": {"REC": "rec", "YDS": "rec_yds", "TD": "rec_td",
                  "TGTS": "rec_tgts", "LONG": "rec_long"},
    "defensive": {"TOT": "def_tackles", "SOLO": "def_solo", "SACKS": "def_sacks",
                  "TFL": "def_tfl", "PD": "def_pd", "TD": "def_td"},
    "interceptions": {"INT": "def_int", "TD": "int_td"},
    # Return touchdowns count toward an anytime-TD bet. Yardage here isn't a
    # market anyone writes, so only the TD column is read.
    "kickReturns": {"TD": "kr_td"},
    "puntReturns": {"TD": "pr_td"},
    "fumbles": {"LOST": "fum_lost"},
    "kicking": {"PTS": "kick_pts", "LONG": "fg_long"},
}

_VALID_KEYS = {s["key"] for s in STAT_CATALOG}


def _to_float(value: str):
    """Returns a float, or None if the cell is blank/missing/unparseable.

    ESPN uses '--' (and sometimes '') for "did not record". That must stay
    absent, never become 0 -- a bet resolver needs to tell "caught 0 passes"
    apart from "didn't play the position at all".
    """
    if value is None:
        return None
    value = value.strip()
    if not value or value == "--":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _split_made(value: str):
    """Left half of a compound cell: '1/1' -> 1.0, '2-3' -> 2.0, '7' -> 7.0.

    ESPN uses BOTH separators, and not where you'd guess: kicking is
    'made/attempted' ("1/1") while a quarterback's sacks are
    'sacks-yardsLost' ("1-9"). The previous version tried to parse the whole
    cell as a float FIRST and gave up when that failed -- which it always does
    for a compound value -- so it returned None for every one of them. Field
    goals and extra points therefore never resolved at all: the prop sat at
    "- / 1.5" all game and settled on the wrong side.

    Split first, parse second.
    """
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else str(value)
    for sep in ("/", "-"):
        if sep in text:
            return _to_float(text.split(sep, 1)[0])
    return _to_float(text)


def _parse_athlete_stats(category: str, labels: list, stats: list) -> dict:
    """Turns one athlete's (labels, stats) pair from a box score category into
    {stat_key: float}, splitting the compound cells ESPN reports."""
    out = {}
    for label, raw in zip(labels, stats):
        raw = raw if isinstance(raw, str) else str(raw)

        if category == "passing" and label == "C/ATT":
            if "/" in raw:
                cmp_, att = raw.split("/", 1)
                cmp_v, att_v = _to_float(cmp_), _to_float(att)
                if cmp_v is not None:
                    out["pass_cmp"] = cmp_v
                if att_v is not None:
                    out["pass_att"] = att_v
            continue

        if category == "kicking" and label == "FG":
            made = _split_made(raw)
            if made is not None:
                out["fg_made"] = made
            continue

        if category == "kicking" and label == "XP":
            made = _split_made(raw)
            if made is not None:
                out["xp_made"] = made
            continue

        # "1-9" here is sacks-taken minus yards-lost, not a range. Only the
        # left half is a prop.
        if category == "passing" and label == "SACKS":
            taken = _split_made(raw)
            if taken is not None:
                out["pass_sacked"] = taken
            continue

        key = _LABEL_MAP.get(category, {}).get(label)
        if not key:
            continue
        val = _to_float(raw)
        if val is not None:
            out[key] = val

    return out


# key -> the component keys it sums. Built once from the catalog rather than
# repeated here, so a combined market is declared in exactly one place.
_COMBOS = {s["key"]: s["components"] for s in STAT_CATALOG if s.get("components")}


def apply_combos(stats: dict) -> dict:
    """Add the combined markets to a parsed stat line.

    A missing component counts as zero -- a running back with no carries really
    did gain zero rushing yards, so his rush+rec total is just his receiving
    yards. But if EVERY component is missing the combo is left absent, because
    that means the player has no line at all, and a bet resolver has to be able
    to tell "gained nothing" from "didn't play". Turning the second into 0 is
    how an under settles wrong.
    """
    for key, components in _COMBOS.items():
        present = [stats[c] for c in components if stats.get(c) is not None]
        if present:
            stats[key] = float(sum(present))
    return stats


def _iter_team_categories(summary: dict):
    """Yields (team_abbrev, category_name, labels, athletes[]) for every stat
    category in the box score. Defensive against any missing/odd shape."""
    try:
        players = (summary or {}).get("boxscore", {}).get("players") or []
    except AttributeError:
        return
    for team_block in players:
        if not isinstance(team_block, dict):
            continue
        abbrev = ((team_block.get("team") or {}).get("abbreviation")) or "???"
        for stat in team_block.get("statistics") or []:
            if not isinstance(stat, dict):
                continue
            name = stat.get("name", "")
            labels = stat.get("labels") or []
            athletes = stat.get("athletes") or []
            yield abbrev, name, labels, athletes


def player_stats(summary: dict, player_id: str) -> dict:
    """{stat_key: float} for one athlete across every category they appear in.

    Only stats the player actually recorded are included -- missing/blank
    values never show up as 0.
    """
    result = {}
    player_id = str(player_id)
    try:
        for _abbrev, category, labels, athletes in _iter_team_categories(summary):
            for entry in athletes:
                if not isinstance(entry, dict):
                    continue
                athlete = entry.get("athlete") or {}
                if str(athlete.get("id")) != player_id:
                    continue
                stats = entry.get("stats") or []
                result.update(_parse_athlete_stats(category, labels, stats))
    except (TypeError, KeyError, ValueError):
        return {}
    # Combos are derived last, once every category has contributed -- rushing
    # and receiving are separate blocks, so neither alone can compute them.
    return apply_combos(result)


def game_players(summary: dict) -> list:
    """Everyone with a recorded stat line in this game.

    [{"id", "name", "team", "categories": [...]}], one entry per athlete,
    merged across categories (a player can show up in both rushing and
    receiving, e.g. a running back).
    """
    players = {}
    try:
        for abbrev, category, _labels, athletes in _iter_team_categories(summary):
            for entry in athletes:
                if not isinstance(entry, dict):
                    continue
                athlete = entry.get("athlete") or {}
                pid = athlete.get("id")
                if not pid:
                    continue
                pid = str(pid)
                if pid not in players:
                    players[pid] = {
                        "id": pid,
                        "name": athlete.get("displayName", ""),
                        "team": abbrev,
                        "categories": [],
                    }
                if category not in players[pid]["categories"]:
                    players[pid]["categories"].append(category)
    except (TypeError, KeyError, ValueError):
        return []
    return list(players.values())


class NFLPlayerStats:
    """TTL-cached fetcher over the ESPN box score summary endpoint.

    A prop tracker calls .stats_for()/.players_in() once per tracked prop per
    poll cycle; caching the parsed summary per event_id collapses that back
    down to one HTTP request per game per TTL window.
    """

    def __init__(self, ttl: float = SUMMARY_TTL):
        self._ttl = ttl
        self._cache: dict = {}
        self._session = requests.Session()

    def _summary(self, event_id: str) -> dict:
        event_id = str(event_id)
        hit = self._cache.get(event_id)
        if hit and time.time() - hit[0] < self._ttl:
            return hit[1]

        summary = {}
        try:
            r = self._session.get(SUMMARY, params={"event": event_id}, timeout=10)
            r.raise_for_status()
            summary = r.json() or {}
        except (requests.RequestException, ValueError):
            # A failed poll must not crash the scoreboard -- fall back to
            # whatever was cached before, even if stale, rather than nothing.
            if hit:
                return hit[1]
            summary = {}

        self._cache[event_id] = (time.time(), summary)
        return summary

    def stats_for(self, event_id: str, player_id: str) -> dict:
        try:
            return player_stats(self._summary(event_id), player_id)
        except Exception:
            return {}

    def players_in(self, event_id: str) -> list:
        try:
            return game_players(self._summary(event_id))
        except Exception:
            return []


class _RosterCache:
    def __init__(self, ttl: float = ROSTER_TTL):
        self._ttl = ttl
        self._cache: dict = {}
        self._session = requests.Session()

    def get(self, team_abbrev: str) -> list:
        team_abbrev = (team_abbrev or "").lower().strip()
        if not team_abbrev:
            return []

        hit = self._cache.get(team_abbrev)
        if _fresh(hit, self._ttl):
            return hit[1]

        players = []
        try:
            r = self._session.get(ROSTER.format(team=team_abbrev), timeout=10)
            r.raise_for_status()
            payload = r.json() or {}
            for group in payload.get("athletes") or []:
                if not isinstance(group, dict):
                    continue
                for item in group.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    pid = item.get("id")
                    if not pid:
                        continue
                    position = (item.get("position") or {}).get("abbreviation", "")
                    players.append({
                        "id": str(pid),
                        "name": item.get("displayName") or item.get("fullName", ""),
                        "position": position,
                    })
        except (requests.RequestException, ValueError, KeyError, TypeError):
            # Keep serving the last good roster rather than an empty picker.
            if hit:
                return hit[1]
            players = []

        self._cache[team_abbrev] = (time.time(), players)
        return players


_roster_cache = _RosterCache()


def roster(team_abbrev: str) -> list:
    """[{"id", "name", "position"}] for a team, TTL-cached (~1hr)."""
    return _roster_cache.get(team_abbrev)


# -- curated roster: who people actually bet on ------------------------------
#
# A 53-man roster is mostly irrelevant to props. Almost every bet is on the
# starting QB, the top few RBs/WRs, a TE, the kicker, or occasionally a pass
# rusher for a sack prop -- never the 2nd/3rd string QB or a long-snapper.
# Rather than guess who's a "starter" from the flat roster list (which isn't
# depth-ordered), this reads ESPN's actual depth chart, where each position
# slot's athletes are already ranked 1st/2nd/3rd string.
#
# Some positions (WR, DE) are split across multiple depth-chart slots -- an
# offense lists wr1/wr2/wr3 as three separate stacks, one per receiver spot,
# rather than one ranked WR list. _interleave() merges those by RANK first
# (all three starters, then all three backups, ...) so "top N" means the best
# N players at the position, not the deepest bench at just the first slot.

# (output position label, [depth-chart slot keys to merge], how many to keep)
CURATED_POSITIONS = [
    ("QB", ["qb"], 1),
    ("RB", ["rb"], 3),
    ("WR", ["wr1", "wr2", "wr3"], 4),
    ("TE", ["te"], 2),
    ("K", ["pk"], 1),
    ("DE", ["lde", "rde"], 2),
]


def _interleave(slots: list) -> list:
    """Merge several depth-ordered lists by rank: all rank-0s, then rank-1s..."""
    out = []
    for rank in range(max((len(s) for s in slots), default=0)):
        for s in slots:
            if rank < len(s):
                out.append(s[rank])
    return out


class _DepthChartCache:
    def __init__(self, ttl: float = ROSTER_TTL):
        self._ttl = ttl
        self._cache: dict = {}
        self._session = requests.Session()

    def _slots(self, team_abbrev: str) -> dict:
        """slot_key -> depth-ordered list of {"id","name"}."""
        team_abbrev = (team_abbrev or "").lower().strip()
        if not team_abbrev:
            return {}

        hit = self._cache.get(team_abbrev)
        if _fresh(hit, self._ttl):
            return hit[1]

        slots: dict = {}
        try:
            r = self._session.get(DEPTHCHART.format(team=team_abbrev), timeout=10)
            r.raise_for_status()
            payload = r.json() or {}
            for formation in payload.get("depthchart") or []:
                for key, pos in (formation.get("positions") or {}).items():
                    athletes = [
                        {"id": str(a["id"]), "name": a.get("displayName", "")}
                        for a in pos.get("athletes") or []
                        if a.get("id")
                    ]
                    if athletes:
                        # A slot key (e.g. "qb") can repeat across formations
                        # (goal-line, nickel, ...) -- keep the longest list seen.
                        if key not in slots or len(athletes) > len(slots[key]):
                            slots[key] = athletes
        except (requests.RequestException, ValueError, KeyError, TypeError):
            if hit:
                return hit[1]

        self._cache[team_abbrev] = (time.time(), slots)
        return slots

    def curated(self, team_abbrev: str) -> list:
        slots = self._slots(team_abbrev)
        out = []
        seen_ids = set()
        for label, slot_keys, count in CURATED_POSITIONS:
            merged = _interleave([slots.get(k, []) for k in slot_keys])
            taken = 0
            for player in merged:
                if taken >= count:
                    break
                if player["id"] in seen_ids:
                    continue  # a player rostered at two slots (rare) isn't listed twice
                seen_ids.add(player["id"])
                out.append({"id": player["id"], "name": player["name"], "position": label})
                taken += 1
        return out


_depthchart_cache = _DepthChartCache()


def curated_roster(team_abbrev: str) -> list:
    """The dozen or so players people actually bet on, grouped by position in
    a fixed, sensible order (QB, RB, WR, TE, K, DE) -- starter(s) only, taken
    from the real depth chart. Falls back to an empty list (never raises) so
    the picker can fall through to the full roster if this is unavailable."""
    try:
        return _depthchart_cache.curated(team_abbrev)
    except Exception:
        return []
