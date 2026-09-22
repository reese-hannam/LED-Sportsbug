"""College football -- ESPN's public scoreboard, scoped to FBS.

Two things make this different from the NFL source rather than a copy of it.

First, coverage. The scoreboard's DEFAULT response is a curated 25 games, not
the full slate -- passing `groups=80` (FBS) is what actually returns all ~99.
That's easy to miss, because 25 games looks plausible until you notice your
team isn't in it.

Second, volume. 134 teams and ~99 simultaneous games is far too much to rotate
through on one panel, so this source carries the machinery to narrow it: every
game knows the rank and conference of both teams, and `matches_filters()` picks
out the ones worth showing. The filter is applied at DISPLAY time rather than
fetch time, because one request for the whole slate is cheaper and simpler than
one per selected conference -- and it means changing the filter is instant
rather than waiting for a poll.
"""

import requests

from .base import Game, Team, Source, PRE, LIVE, FINAL, parse_odds
from .cfb_meta import Rankings, conference_of

SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
)

# ESPN group 80 is FBS (Division I-A). Without this the endpoint returns a
# curated subset, NOT the full slate.
FBS_GROUP = 80

# ESPN uses 99 for "unranked" in curatedRank rather than omitting the field.
UNRANKED = 99

# Filter keys. Conferences are "conf:<id>"; these two are special.
TOP_25 = "top25"
TOP_50 = "top50"
ALL = "all"


def _state(espn_state: str) -> str:
    return {"in": LIVE, "post": FINAL, "pre": PRE}.get(espn_state, PRE)


def _period(status: dict) -> str:
    p = status.get("period", 0)
    if status.get("type", {}).get("state") == "post":
        return "F/OT" if p > 4 else "F"
    if not p:
        return ""
    return {1: "1ST", 2: "2ND", 3: "3RD", 4: "4TH"}.get(p, f"OT{p - 4}")


class CFBSource(Source):
    sport = "cfb"
    poll_interval = 15.0

    def __init__(self, week: int | None = None, year: int | None = None):
        self.week = week
        self.year = year
        self._session = requests.Session()
        self.rankings = Rankings()

    def fetch(self) -> list[Game]:
        params = {"groups": FBS_GROUP, "limit": 400}
        if self.year and self.week:
            params.update({"dates": self.year, "week": self.week})

        r = self._session.get(SCOREBOARD, params=params, timeout=15)
        r.raise_for_status()
        return [self._parse(e) for e in r.json().get("events", [])]

    def _parse(self, e: dict) -> Game:
        comp = e["competitions"][0]
        status = e.get("status", {})
        state = _state(status.get("type", {}).get("state", ""))
        sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}

        def team(side: str) -> Team:
            c = sides.get(side, {})
            t = c.get("team", {})
            recs = c.get("records") or []
            return Team(
                abbrev=t.get("abbreviation") or t.get("shortDisplayName", "???"),
                score=int(c.get("score", 0) or 0),
                color=_team_color(t),
                name=t.get("shortDisplayName") or t.get("name", ""),
                record=recs[0].get("summary", "") if recs else "",
            )

        def rank(side: str):
            c = sides.get(side, {})
            n = (c.get("curatedRank") or {}).get("current", UNRANKED)
            return None if not n or n >= UNRANKED else int(n)

        def conf(side: str) -> str:
            t = (sides.get(side, {}) or {}).get("team", {})
            # The scoreboard usually carries conferenceId directly; fall back to
            # the static map for the occasional entry that doesn't.
            return str(t.get("conferenceId") or conference_of(t.get("id", "")) or "")

        sit = comp.get("situation", {}) or {}
        poss_id = sit.get("possession")
        poss = ""
        for c in comp.get("competitors", []):
            if str(c.get("id")) == str(poss_id):
                poss = c.get("team", {}).get("abbreviation", "")

        def timeouts(key: str):
            value = sit.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        spread, over_under = parse_odds(comp)

        return Game(
            id=str(e["id"]),
            sport="cfb",
            away=team("away"),
            home=team("home"),
            state=state,
            period=_period(status),
            status_detail=status.get("type", {}).get("detail", ""),
            start_utc=e.get("date", ""),
            detail={
                "clock": status.get("displayClock", ""),
                "down_distance": sit.get("shortDownDistanceText", ""),
                "yardline": sit.get("possessionText", ""),
                "possession": poss,
                "red_zone": bool(sit.get("isRedZone")),
                "last_play": (sit.get("lastPlay") or {}).get("text", ""),
                "timeouts_away": timeouts("awayTimeouts"),
                "timeouts_home": timeouts("homeTimeouts"),
                # Pre-game betting line, drawn on the preview screen.
                "spread": spread,
                "over_under": over_under,
                # What the category filter keys off.
                "rank_away": rank("away"),
                "rank_home": rank("home"),
                # Needed for the top-50 check: the poll's "others receiving
                # votes" carry no curatedRank, so they're matched by team id.
                "id_away": str((sides.get("away", {}).get("team") or {}).get("id", "")),
                "id_home": str((sides.get("home", {}).get("team") or {}).get("id", "")),
                "conf_away": conf("away"),
                "conf_home": conf("home"),
            },
        )


def matches_filters(game: Game, filters, rankings, favorites=None) -> bool:
    """Should this game be shown, given the selected categories?

    Empty selection means everything -- a blank panel would be a worse default
    than a busy one. A favourite team's game always shows regardless of the
    filter, because deselecting a conference shouldn't hide the team you
    actually care about.
    """
    d = game.detail or {}

    if favorites:
        picks = {a.upper() for a in favorites}
        if game.away.abbrev.upper() in picks or game.home.abbrev.upper() in picks:
            return True

    if not filters or ALL in filters:
        return True

    wanted = set(filters)

    if TOP_25 in wanted or TOP_50 in wanted:
        limit = 50 if TOP_50 in wanted else 25
        top = rankings.top(limit)
        ids = {str(d.get("id_away", "")), str(d.get("id_home", ""))}
        # curatedRank is the cheaper check and is present on the scoreboard.
        for key in ("rank_away", "rank_home"):
            rank = d.get(key)
            if rank is not None and rank <= min(limit, 25):
                return True
        # Beyond 25 the poll's "others receiving votes" have no curatedRank, so
        # fall back to the ranked-team id set.
        if limit > 25 and top and ids & top:
            return True

    for key in ("conf_away", "conf_home"):
        conf = d.get(key)
        if conf and f"conf:{conf}" in wanted:
            return True

    return False


def _team_color(t: dict) -> tuple:
    """ESPN gives a hex string per team; fall back to a neutral if absent."""
    from .colors import led
    raw = (t.get("color") or "").lstrip("#")
    if len(raw) == 6:
        try:
            return led(tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4)))
        except ValueError:
            pass
    return (170, 170, 170)
