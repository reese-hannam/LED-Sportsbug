"""Replay a completed NFL game as if it were live.

ESPN keeps per-play `wallclock` timestamps on finished games, so a recorded
summary can be played back with real pacing instead of a fixed tick. That means
the NFL bug -- and later the alert and animation engines -- can be exercised at
any time of year without waiting for a live game.

The recorded plays carry `shortDownDistanceText` and `possessionText`, the same
preformatted fields the live scoreboard's `situation` object uses, so a replayed
Game is shape-identical to a live one. Renderers cannot tell the difference.
"""

import datetime
import json
import os
import time

from .playtext import parse_timeout
from .base import Game, Team, Source, LIVE, FINAL
from .colors import team_color

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "nfl",
)

# Real gaps between plays include halftime and TV timeouts. Cap them so a replay
# doesn't sit on an unchanged screen for minutes.
MAX_GAP_SECONDS = 15.0
# Used when a recording has no wallclock timestamps at all.
FALLBACK_GAP_SECONDS = 6.0


def _parse_wallclock(s: str):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class Recording:
    """A completed game flattened into a timeline of play states."""

    def __init__(self, payload: dict):
        comp = payload["header"]["competitions"][0]

        self.teams: dict[str, dict] = {}
        for c in comp["competitors"]:
            self.teams[str(c["id"])] = {
                "abbrev": c["team"]["abbreviation"],
                "home_away": c["homeAway"],
                "final": int(c.get("score", 0) or 0),
            }

        self.home_id = next(i for i, t in self.teams.items() if t["home_away"] == "home")
        self.away_id = next(i for i, t in self.teams.items() if t["home_away"] == "away")
        self.short_name = f"{self.teams[self.away_id]['abbrev']} @ {self.teams[self.home_id]['abbrev']}"
        self.game_id = str(payload["header"]["id"])

        self.plays = self._flatten(payload)
        self.duration = self.plays[-1]["offset"] if self.plays else 0.0

    def _flatten(self, payload: dict) -> list[dict]:
        raw = []
        for drive in (payload.get("drives") or {}).get("previous", []):
            raw.extend(drive.get("plays", []))
        # sequenceNumber is a numeric STRING ('3900', '10600'); sorting it as text
        # puts '10600' before '3900' and scrambles the game. Sort numerically, and
        # fall back to file order (already chronological) if it's ever non-numeric.
        def order(item):
            i, p = item
            seq = str(p.get("sequenceNumber", ""))
            return (p.get("period", {}).get("number", 0), int(seq) if seq.isdigit() else i)

        raw = [p for _, p in sorted(enumerate(raw), key=order)]

        # Timeouts aren't a field on recorded plays -- they have to be counted from
        # the play stream. Three per team per half, replenished at the start of Q3.
        remaining = {tid: 3 for tid in self.teams}

        plays, offset, prev_clock = [], 0.0, None
        half = 1
        for p in raw:
            wc = _parse_wallclock(p.get("wallclock", ""))
            if prev_clock and wc:
                offset += min(MAX_GAP_SECONDS, max(0.0, (wc - prev_clock).total_seconds()))
            elif plays:
                offset += FALLBACK_GAP_SECONDS
            prev_clock = wc or prev_clock

            period = (p.get("period") or {}).get("number", 0)
            if period >= 3 and half == 1:
                half = 2
                remaining = {tid: 3 for tid in self.teams}

            charged = parse_timeout(p)
            if charged:
                for tid, info in self.teams.items():
                    if info["abbrev"] == charged and remaining[tid] > 0:
                        remaining[tid] -= 1

            start = p.get("start", {}) or {}
            plays.append({
                "timeouts": dict(remaining),
                "offset": offset,
                "period": period,
                "clock": (p.get("clock") or {}).get("displayValue", ""),
                "home_score": int(p.get("homeScore", 0) or 0),
                "away_score": int(p.get("awayScore", 0) or 0),
                "possession_id": str((start.get("team") or {}).get("id", "")),
                # Preformatted by ESPN exactly as the live `situation` object gives them.
                "down_distance": start.get("shortDownDistanceText", ""),
                "yardline": start.get("possessionText", ""),
                "yards_to_endzone": start.get("yardsToEndzone"),
                "scoring_play": bool(p.get("scoringPlay")),
                "text": p.get("text", ""),
            })
        return plays

    def index_at(self, elapsed: float) -> int:
        lo, hi = 0, len(self.plays) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.plays[mid]["offset"] <= elapsed:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def play_at(self, elapsed: float) -> tuple[dict | None, bool]:
        """The most recent play at `elapsed` seconds in, plus whether the game is over."""
        if not self.plays:
            return None, True
        if elapsed >= self.duration:
            return self.plays[-1], True
        return self.plays[self.index_at(elapsed)], False


class NFLReplaySource(Source):
    sport = "nfl"
    # The replay advances on wall time, so poll often enough to look live.
    poll_interval = 1.0

    def __init__(self, path: str, speed: float = 8.0, loop: bool = True):
        self.recording = Recording(load_payload(path))
        self.speed = max(0.1, speed)
        self.loop = loop
        self.started = time.time()

    def restart(self) -> None:
        self.started = time.time()

    @property
    def elapsed(self) -> float:
        return (time.time() - self.started) * self.speed

    def fetch(self) -> list[Game]:
        elapsed = self.elapsed
        if self.loop and self.recording.duration and elapsed > self.recording.duration + 5:
            self.restart()
            elapsed = 0.0

        play, over = self.recording.play_at(elapsed)
        return [self._to_game(play, over, elapsed)]

    def _to_game(self, play: dict | None, over: bool, elapsed: float) -> Game:
        rec = self.recording

        def team(team_id: str, score: int) -> Team:
            info = rec.teams[team_id]
            return Team(
                abbrev=info["abbrev"],
                score=score,
                color=team_color("nfl", info["abbrev"]),
            )

        if play is None:
            home = team(rec.home_id, 0)
            away = team(rec.away_id, 0)
            return Game(id=rec.game_id, sport="nfl", away=away, home=home,
                        state=FINAL, period="F", status_detail="empty recording")

        home = team(rec.home_id, play["home_score"])
        away = team(rec.away_id, play["away_score"])

        period = play["period"]
        if over:
            label = "F/OT" if period > 4 else "F"
        else:
            label = {1: "1ST", 2: "2ND", 3: "3RD", 4: "4TH"}.get(period, f"OT{period - 4}")

        poss = rec.teams.get(play["possession_id"], {}).get("abbrev", "")
        ytg = play["yards_to_endzone"]

        return Game(
            id=rec.game_id,
            sport="nfl",
            away=away,
            home=home,
            state=FINAL if over else LIVE,
            period=label,
            status_detail="Final" if over else f"REPLAY {int(elapsed)}s/{int(rec.duration)}s",
            detail={
                # Interpolated so the panel shows a moving clock, not one that
                # jumps only when a play lands.
                "clock": play["clock"],
                "down_distance": play["down_distance"],
                "yardline": play["yardline"],
                "possession": poss,
                "red_zone": ytg is not None and ytg <= 20,
                "last_play": play["text"],
                "scoring_play": play["scoring_play"],
                "timeouts_away": play.get("timeouts", {}).get(rec.away_id),
                "timeouts_home": play.get("timeouts", {}).get(rec.home_id),
                "replay": True,
            },
        )


# -- recording discovery ----------------------------------------------------


def load_payload(path: str) -> dict:
    """Accept a full path, a bare filename, or just an event id."""
    for candidate in (path, os.path.join(DATA_DIR, path), os.path.join(DATA_DIR, f"{path}.json")):
        if os.path.isfile(candidate):
            with open(candidate) as f:
                return json.load(f)
    raise FileNotFoundError(f"no recording found for {path!r} (looked in {DATA_DIR})")


_index_cache: tuple[float, list[dict]] | None = None


def list_recordings() -> list[dict]:
    """Everything in data/nfl, newest first. Falls back to scanning if no index.

    Cached on the index file's mtime -- the control center asks for this every
    couple of seconds, and on the Pi that would be pointless SD-card reads.
    """
    global _index_cache
    index_path = os.path.join(DATA_DIR, "index.json")

    try:
        stamp = os.path.getmtime(index_path)
    except OSError:
        stamp = 0.0
    if _index_cache is not None and _index_cache[0] == stamp:
        return _index_cache[1]

    result = _read_recordings(index_path)
    _index_cache = (stamp, result)
    return result


def _read_recordings(index_path: str) -> list[dict]:
    if os.path.isfile(index_path):
        try:
            with open(index_path) as f:
                entries = json.load(f)
            if isinstance(entries, list):
                return sorted(entries, key=lambda e: e.get("date", ""), reverse=True)
        except (json.JSONDecodeError, OSError):
            pass

    found = []
    if os.path.isdir(DATA_DIR):
        for name in sorted(os.listdir(DATA_DIR)):
            if name.endswith(".json") and name != "index.json":
                found.append({"id": name[:-5], "short_name": name[:-5], "file": name})
    return found
