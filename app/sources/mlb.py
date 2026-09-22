"""MLB source -- statsapi.mlb.com.

One schedule call with ?hydrate=linescore,team returns the entire slate including
balls, strikes, outs and runners on base. No key, no per-game fan-out.
"""

import datetime
import time

import requests

from .base import Game, Team, Source, PRE, LIVE, FINAL
from .colors import team_color

SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
BOXSCORE = "https://statsapi.mlb.com/api/v1/game/{pk}/boxscore"
PLAYBYPLAY = "https://statsapi.mlb.com/api/v1/game/{pk}/playByPlay"

# Same ?fields= trick: the full play-by-play is ~700KB, this returns ~50KB.
PBP_FIELDS = (
    "allPlays,result,event,rbi,awayScore,homeScore,about,isScoringPlay,"
    "matchup,batter,fullName,runners,movement,end,details,runner"
)

# statsapi honours ?fields= and trims the response to just these keys. The full
# boxscore is ~167KB; this brings it under 20KB, which matters on a Pi over wifi.
BOX_FIELDS = "teams,away,home,players,person,id,fullName,stats,batting,summary"

# One batter line is good for this long before it's refetched. Long enough
# that a game stays warm across a full rotation cycle.
BATTER_TTL = 20.0


def _state(abstract: str) -> str:
    return {"Live": LIVE, "Final": FINAL, "Preview": PRE}.get(abstract, PRE)


def _period(ls: dict, state: str, scheduled: int = 9) -> str:
    """'T7' / 'B9', or 'F' / 'F/10' once the game is over."""
    inning = ls.get("currentInning")
    if state == FINAL:
        return "F" if not inning or inning <= scheduled else f"F/{inning}"
    if not inning:
        return ""
    half = "T" if ls.get("isTopInning", True) else "B"
    # Mid/End are between halves -- show the half that just finished.
    if ls.get("inningState") in ("Middle", "End"):
        half = "M" if ls.get("inningState") == "Middle" else "E"
    return f"{half}{inning}"


def short_name(full: str) -> str:
    """'Jose Ramirez' -> 'J.RAMIREZ', to fit the panel."""
    parts = (full or "").split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].upper()
    return f"{parts[0][0].upper()}.{parts[-1].upper()}"


class BatterLines:
    """Current batter's line for the night, e.g. 'J.RAMIREZ 2-4'.

    This needs a per-game boxscore call, which the whole-slate schedule request
    doesn't carry -- so it's fetched LAZILY, only for the game actually on the
    panel, and cached. Pulling it for all fifteen games every poll would be a lot
    of bandwidth to render one line nobody is looking at.
    """

    def __init__(self):
        self._cache: dict[str, tuple[float, str]] = {}
        self._session = requests.Session()

    def get(self, game_pk: str, batter_id, batter_name: str) -> str:
        if not game_pk or not batter_id:
            return ""

        key = f"{game_pk}:{batter_id}"
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < BATTER_TTL:
            return hit[1]

        line = short_name(batter_name)
        try:
            r = self._session.get(
                BOXSCORE.format(pk=game_pk), params={"fields": BOX_FIELDS}, timeout=10
            )
            r.raise_for_status()
            teams = r.json().get("teams", {})
            for side in ("away", "home"):
                player = (teams.get(side, {}).get("players") or {}).get(f"ID{batter_id}")
                if not player:
                    continue
                # statsapi preformats this as '0-3 | RBI' -- exactly the line the
                # broadcast shows, so use it rather than rebuilding from counts.
                summary = (player.get("stats", {}).get("batting") or {}).get("summary", "")
                if summary:
                    line = f"{line} {summary.replace(' | ', ' · ')}"
                break
        except (requests.RequestException, ValueError, KeyError):
            # A missing batter line must never take the scoreboard down; the name
            # alone is still useful.
            pass

        self._cache[key] = (time.time(), line)
        return line


class ScoringPlays:
    """Who got the hit and who came around to score.

    MLB's play-by-play is fully structured, so none of this is text parsing:
    `result.event` names the play, `result.rbi` counts it, `matchup.batter` is
    who did it, and any runner whose `movement.end` is 'score' crossed the plate.

    Fetched only when a favorite's score actually changes -- not on a schedule --
    so the 50KB cost is paid a handful of times a game rather than every poll.
    """

    def __init__(self):
        self._session = requests.Session()

    def describe(self, game_pk: str, away_score: int, home_score: int):
        """Returns (batter, how, scored) for the play that produced this score."""
        try:
            r = self._session.get(
                PLAYBYPLAY.format(pk=game_pk), params={"fields": PBP_FIELDS}, timeout=10
            )
            r.raise_for_status()
            plays = r.json().get("allPlays", [])
        except (requests.RequestException, ValueError):
            return ("", "", "")

        # Identify the exact play by the score it produced. Matching on "most
        # recent scoring play" would pick the wrong one if two land between polls.
        match = None
        for p in plays:
            if not (p.get("about") or {}).get("isScoringPlay"):
                continue
            res = p.get("result") or {}
            if res.get("awayScore") == away_score and res.get("homeScore") == home_score:
                match = p
        if match is None:
            return ("", "", "")

        res = match["result"]
        batter = short_name((match.get("matchup", {}).get("batter") or {}).get("fullName", ""))

        event = (res.get("event") or "").upper()
        rbi = res.get("rbi") or 0
        how = event
        if rbi:
            how = f"{event} · {rbi} RBI"

        scorers = [
            short_name((rn.get("details", {}).get("runner") or {}).get("fullName", ""))
            for rn in match.get("runners", [])
            if (rn.get("movement") or {}).get("end") == "score"
        ]
        # On a homer the batter scoring himself is implied by the word HOME RUN.
        if event == "HOME RUN":
            scorers = [s for s in scorers if s != batter]
        # Dedupe while keeping order -- a runner can appear on several movements.
        seen, unique = set(), []
        for s in scorers:
            if s and s not in seen:
                seen.add(s)
                unique.append(s)

        scored = ""
        if unique:
            scored = "SCORED " + ", ".join(unique)
            if len(scored) > 31:
                scored = f"SCORED {len(unique)} RUNNERS"

        return (batter, how[:30], scored)


class MLBSource(Source):
    sport = "mlb"
    poll_interval = 10.0

    def __init__(self, date: str | None = None):
        # Pinning a date is what makes replaying an old slate trivial.
        self.date = date
        self._session = requests.Session()

    def fetch(self) -> list[Game]:
        d = self.date or datetime.date.today().isoformat()
        r = self._session.get(
            SCHEDULE,
            params={"sportId": 1, "date": d, "hydrate": "linescore,team"},
            timeout=12,
        )
        r.raise_for_status()
        payload = r.json()

        games: list[Game] = []
        for day in payload.get("dates", []):
            for g in day.get("games", []):
                games.append(self._parse(g))
        return games

    def _parse(self, g: dict) -> Game:
        status = g.get("status", {})
        state = _state(status.get("abstractGameState", ""))
        ls = g.get("linescore", {}) or {}
        offense = ls.get("offense", {}) or {}

        def team(side: str) -> Team:
            t = g["teams"][side]
            abbrev = t["team"].get("abbreviation", "???")
            rec = t.get("leagueRecord", {})
            return Team(
                abbrev=abbrev,
                score=t.get("score", 0) or 0,
                color=team_color("mlb", abbrev),
                name=t["team"].get("teamName", ""),
                record=f"{rec.get('wins', 0)}-{rec.get('losses', 0)}" if rec else "",
            )

        detail_state = status.get("detailedState", "")
        # 'Warmup' and 'Pre-Game' report as abstract Live but have no real linescore yet.
        if state == LIVE and detail_state in ("Warmup", "Pre-Game", "Delayed Start"):
            state = PRE

        return Game(
            id=str(g["gamePk"]),
            sport="mlb",
            away=team("away"),
            home=team("home"),
            state=state,
            period=_period(ls, state, ls.get("scheduledInnings", 9)),
            status_detail=detail_state,
            start_utc=g.get("gameDate", ""),
            detail={
                "balls": ls.get("balls", 0) or 0,
                "strikes": ls.get("strikes", 0) or 0,
                "outs": ls.get("outs", 0) or 0,
                # statsapi only includes a base key when it's occupied.
                "bases": [b in offense for b in ("first", "second", "third")],
                "inning_state": ls.get("inningState", ""),
                "is_top": ls.get("isTopInning", True),
                "batter": (offense.get("batter") or {}).get("fullName", ""),
                "batter_id": (offense.get("batter") or {}).get("id"),
                "pitcher": (ls.get("defense", {}).get("pitcher") or {}).get("fullName", ""),
            },
        )
