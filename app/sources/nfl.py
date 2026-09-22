"""NFL source -- ESPN's public scoreboard.

seasontype: 1=preseason, 2=regular, 3=post. Note that seasontype alone is ignored
unless dates/week accompany it -- a bare query always returns the current week.
Verified against 2026 preseason: ESPN carries full play-by-play, down/distance and
player box scores for preseason exactly as it does for the regular season.
"""

import requests

from .base import Game, Team, Source, PRE, LIVE, FINAL, parse_odds
from .colors import team_color

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

PRESEASON, REGULAR, POSTSEASON = 1, 2, 3


def _state(espn_state: str) -> str:
    return {"in": LIVE, "post": FINAL, "pre": PRE}.get(espn_state, PRE)


def _period(status: dict) -> str:
    p = status.get("period", 0)
    if status.get("type", {}).get("state") == "post":
        return "F/OT" if p > 4 else "F"
    if not p:
        return ""
    return {1: "1ST", 2: "2ND", 3: "3RD", 4: "4TH"}.get(p, f"OT{p - 4}")


class NFLSource(Source):
    sport = "nfl"
    poll_interval = 12.0

    def __init__(self, seasontype: int | None = None, year: int | None = None, week: int | None = None):
        self.seasontype = seasontype
        self.year = year
        self.week = week
        self._session = requests.Session()

    def fetch(self) -> list[Game]:
        params = {}
        # ESPN honors seasontype only when a year/week anchors it.
        if self.seasontype and self.year:
            params = {"dates": self.year, "seasontype": self.seasontype}
            if self.week:
                params["week"] = self.week

        r = self._session.get(SCOREBOARD, params=params, timeout=12)
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
            abbrev = t.get("abbreviation", "???")
            recs = c.get("records") or []
            return Team(
                abbrev=abbrev,
                score=int(c.get("score", 0) or 0),
                color=team_color("nfl", abbrev),
                name=t.get("name", ""),
                record=recs[0].get("summary", "") if recs else "",
            )

        # 'situation' is present only while a game is actually in progress.
        sit = comp.get("situation", {}) or {}
        poss_id = sit.get("possession")
        poss = ""
        for c in comp.get("competitors", []):
            if str(c.get("id")) == str(poss_id):
                poss = c.get("team", {}).get("abbreviation", "")

        # Timeouts live on `situation` and so exist only while a game is in
        # progress. Unverified against a live feed (the 2026 preseason ended
        # before this was written), hence the defensive reads -- a missing field
        # must render as "unknown", never as "zero timeouts left".
        def timeouts(key: str):
            value = sit.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        spread, over_under = parse_odds(comp)

        return Game(
            id=str(e["id"]),
            sport="nfl",
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
            },
        )
