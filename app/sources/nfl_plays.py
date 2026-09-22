"""NFL play-by-play -- ESPN's core API (a different host from the site API used
elsewhere in this project).

Why this exists: fantasy-football alerts need to know EXACTLY which player did
something on a given play (caught a pass, forced a fumble, kicked a FG), not
just that a stat category changed. The site-API game summary this project
already uses for box scores (see nfl_stats.py) reports aggregated stat lines,
not individual plays, so it can't tell you a specific reception just happened.
The core API's `plays` endpoint carries a `participants[]` list per play with
one entry per player involved and their exact role (passer, receiver, tackler,
...), keyed by the same athlete ids `roster()`/`curated_roster()` return. That
id match is what makes "flash an alert when a rostered player does something"
possible at all.

A live game needs alerts to show up quickly, so plays are polled on a short
TTL. But naive polling would replay a game's entire history as "new" on every
process restart -- NFLPlayFeed tracks which play ids have already been handed
out per event and only returns the delta, seeding silently on the first call
for a game (same rule app/events.py applies to scores).
"""

import json
import os
import re
import time

import requests

PLAYS = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/"
    "{event_id}/competitions/{event_id}/plays"
)

# Under the play poll (main.PLAY_POLL_SECONDS, 4s) so every poll actually
# refetches -- at 5s, every other poll was served from cache and a play could
# sit unnoticed for eight seconds. It still collapses repeat calls in one tick.
PLAYS_TTL = 3.0

# The endpoint's own page cap (verified: a full ~160-play game fit in one
# response at this limit). Fetch pages beyond the first only if `count`
# implies more than came back.
PAGE_LIMIT = 400

_ATHLETE_ID_RE = re.compile(r"/athletes/(\d+)")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECORDED_PLAYS_DIR = os.path.join(PROJECT_ROOT, "data", "nfl", "plays")


def _athlete_id_from_ref(ref: str):
    if not isinstance(ref, str):
        return None
    m = _ATHLETE_ID_RE.search(ref)
    return m.group(1) if m else None


_TEAM_ID_RE = re.compile(r"/teams/(\d+)")


def _team_id_from_ref(ref: str):
    if not isinstance(ref, str):
        return None
    m = _TEAM_ID_RE.search(ref)
    return m.group(1) if m else None


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_play(raw: dict) -> dict:
    """Normalize one raw core-API play into a flat dict. Never raises --
    missing/odd fields degrade to sensible defaults."""
    raw = raw if isinstance(raw, dict) else {}

    participants = []
    for p in raw.get("participants") or []:
        if not isinstance(p, dict):
            continue
        athlete_id = _athlete_id_from_ref((p.get("athlete") or {}).get("$ref"))
        if not athlete_id:
            continue
        participants.append({
            "athlete_id": athlete_id,
            "role": p.get("type") or "other",
            "order": _to_int(p.get("order"), 0),
        })

    return {
        "id": str(raw.get("id") or ""),
        "sequence": _to_int(raw.get("sequenceNumber"), None) if raw.get("sequenceNumber") is not None else None,
        "text": raw.get("text") or "",
        "short_text": raw.get("shortText") or "",
        "type": (raw.get("type") or {}).get("text") or "",
        "period": _to_int((raw.get("period") or {}).get("number"), 0),
        "clock": (raw.get("clock") or {}).get("displayValue") or "",
        "yards": _to_float(raw.get("statYardage")),
        "scoring": bool(raw.get("scoringPlay")),
        "turnover": bool(raw.get("isTurnover")),
        "away_score": _to_int(raw.get("awayScore"), 0),
        "home_score": _to_int(raw.get("homeScore"), 0),
        # Which team had the ball. A team DEFENSE has no athlete id to match
        # against, so fantasy defense scoring is worked out from possession
        # instead -- see match_defense() in app/fantasy_events.py.
        "team_id": _team_id_from_ref((raw.get("team") or {}).get("$ref")),
        "participants": participants,
    }


def athlete_ids(play: dict, roles: set | None = None) -> set:
    """The athlete ids in a normalized play, optionally filtered to `roles`."""
    out = set()
    for p in (play or {}).get("participants") or []:
        if roles is not None and p.get("role") not in roles:
            continue
        aid = p.get("athlete_id")
        if aid:
            out.add(aid)
    return out


def fetch_all_raw(session: requests.Session, event_id: str) -> list:
    """Fetch every play for an event, handling pagination beyond the first page."""
    items = []
    page = 1
    while True:
        try:
            r = session.get(
                PLAYS.format(event_id=event_id),
                params={"limit": PAGE_LIMIT, "page": page},
                timeout=10,
            )
            r.raise_for_status()
            payload = r.json() or {}
        except (requests.RequestException, ValueError):
            break

        page_items = payload.get("items") or []
        items.extend(page_items)

        count = payload.get("count") or 0
        page_count = payload.get("pageCount") or 1
        if page >= page_count or len(items) >= count or not page_items:
            break
        page += 1

    return items


class NFLPlayFeed:
    """TTL-cached, incremental play fetcher.

    One HTTP request per event per TTL window regardless of how many players
    are tracked -- .new_plays() is the entry point a poll loop should call;
    every tracked player checks the same cached list instead of triggering
    its own request.
    """

    def __init__(self, ttl: float = PLAYS_TTL):
        self._ttl = ttl
        self._cache: dict = {}  # event_id -> (fetched_at, [normalized plays])
        self._seen: dict = {}   # event_id -> set of play ids already returned
        self._session = requests.Session()

    def all_plays(self, event_id: str) -> list:
        event_id = str(event_id)
        hit = self._cache.get(event_id)
        if hit and time.time() - hit[0] < self._ttl:
            return hit[1]

        raw_items = self._fetch(event_id)
        plays = [parse_play(item) for item in raw_items]
        self._cache[event_id] = (time.time(), plays)
        return plays

    def _fetch(self, event_id: str) -> list:
        try:
            return fetch_all_raw(self._session, event_id)
        except Exception:
            hit = self._cache.get(event_id)
            return [] if not hit else []

    def new_plays(self, event_id: str) -> list:
        event_id = str(event_id)
        plays = self.all_plays(event_id)

        if event_id not in self._seen:
            # First time this event is seen: seed silently so a process
            # restart mid-game doesn't replay the whole game as fresh alerts.
            self._seen[event_id] = {p["id"] for p in plays}
            return []

        seen = self._seen[event_id]
        fresh = [p for p in plays if p["id"] not in seen]
        seen.update(p["id"] for p in fresh)
        return fresh

    def reset(self, event_id=None) -> None:
        """Clear seen-state, e.g. when switching source/replay games."""
        if event_id is None:
            self._seen.clear()
            self._cache.clear()
        else:
            event_id = str(event_id)
            self._seen.pop(event_id, None)
            self._cache.pop(event_id, None)


def load_recorded(event_id: str) -> list:
    """Normalized plays from a local recording (see tools/record_nfl.py), so
    replay can drive fantasy alerts offline. [] if nothing was recorded."""
    path = os.path.join(RECORDED_PLAYS_DIR, f"{event_id}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f) or []
    except (OSError, ValueError):
        return []


# -- team id -> abbreviation -------------------------------------------------
#
# Plays reference the possessing team by numeric id, but everything else in this
# project keys on abbreviations (CLE, BUF). ESPN's team ids are stable across
# seasons, so this is fetched once and cached for the process.

TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"

_team_abbrev: dict = {}


def team_abbrev(team_id) -> str:
    """'5' -> 'CLE'. Empty string if unknown or unreachable."""
    if not team_id:
        return ""
    team_id = str(team_id)
    if not _team_abbrev:
        try:
            r = requests.get(TEAMS_URL, timeout=10)
            r.raise_for_status()
            leagues = (r.json().get("sports") or [{}])[0].get("leagues") or [{}]
            for entry in leagues[0].get("teams") or []:
                t = entry.get("team") or {}
                if t.get("id") and t.get("abbreviation"):
                    _team_abbrev[str(t["id"])] = t["abbreviation"].upper()
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
            return ""
    return _team_abbrev.get(team_id, "")


def with_team_abbrev(play: dict) -> dict:
    """Add a resolved `team` abbreviation to a normalized play."""
    play = dict(play or {})
    play["team"] = team_abbrev(play.get("team_id"))
    return play
