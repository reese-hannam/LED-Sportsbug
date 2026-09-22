"""Download completed NFL games from ESPN and save them for offline replay.

    python tools/record_nfl.py --seasontype pre --year 2026 --week 3
    python tools/record_nfl.py --seasontype pre --year 2026 --week 3 --limit 5
    python tools/record_nfl.py --event 401873286
    python tools/record_nfl.py --seasontype pre --year 2026 --week 3 --force

Saves each game's full summary JSON to data/nfl/<event_id>.json and keeps
data/nfl/index.json up to date (merged, keyed on event id). Re-running the same
command is a no-op -- already-recorded games count toward --limit.

Also saves each game's normalized play-by-play (see app/sources/nfl_plays.py)
to data/nfl/plays/<event_id>.json, so fantasy-alert replay can run offline
without hitting ESPN's core API live.
"""

import argparse
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.sources.nfl_plays import fetch_all_raw, parse_play  # noqa: E402

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
# Absolute so recordings land in the project regardless of where this is run from.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "nfl")
INDEX_PATH = os.path.join(DATA_DIR, "index.json")
PLAYS_DIR = os.path.join(DATA_DIR, "plays")

SEASONTYPE_WORDS = {"pre": 1, "reg": 2, "post": 3}


def load_index():
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH) as f:
            return json.load(f)
    return []


def save_index(entries):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(INDEX_PATH, "w") as f:
        json.dump(sorted(entries, key=lambda e: e["id"]), f, indent=2)


def fetch_slate(year, seasontype, week):
    params = {"dates": year, "seasontype": seasontype}
    if week:
        params["week"] = week
    r = requests.get(SCOREBOARD, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("events", [])


def record_plays(event_id, force):
    """Download and save one game's normalized core-API plays."""
    path = os.path.join(PLAYS_DIR, f"{event_id}.json")
    if os.path.exists(path) and not force:
        return

    session = requests.Session()
    try:
        raw_items = fetch_all_raw(session, event_id)
    except Exception as e:
        print(f"  {event_id} ERROR fetching plays: {e}")
        return

    plays = [parse_play(item) for item in raw_items]
    os.makedirs(PLAYS_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(plays, f)


def record_event(event_id, force, season_type=None, week=None, short_name=None):
    """Download and save one game's summary. Returns an index entry or None."""
    path = os.path.join(DATA_DIR, f"{event_id}.json")

    if os.path.exists(path) and not force:
        print(f"  {event_id} skip (already recorded, use --force to overwrite)")
        return None

    try:
        r = requests.get(SUMMARY, params={"event": event_id}, timeout=20)
        r.raise_for_status()
        summary = r.json()
    except requests.RequestException as e:
        print(f"  {event_id} ERROR fetching summary: {e}")
        return None

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(summary, f)
    size = os.path.getsize(path)

    record_plays(event_id, force)

    header = summary.get("header", {})
    competition = (header.get("competitions") or [{}])[0]
    competitors = competition.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    home_name = (home.get("team") or {}).get("abbreviation", "?")
    away_name = (away.get("team") or {}).get("abbreviation", "?")
    # Away first, to match the "AWAY @ HOME" ordering of short_name.
    final_score = f"{away.get('score', '?')}-{home.get('score', '?')}"

    drives = (summary.get("drives") or {}).get("previous", [])
    plays = sum(len(d.get("plays", [])) for d in drives)

    entry = {
        "id": str(event_id),
        "short_name": short_name or f"{away_name} @ {home_name}",
        "date": competition.get("date"),
        "season_type": season_type,
        "week": week,
        "home": home_name,
        "away": away_name,
        "final_score": final_score,
        "plays": plays,
        "file": os.path.join("data", "nfl", f"{event_id}.json"),
    }

    matchup = short_name or f"{away_name}@{home_name}"
    print(f"  {event_id} {matchup:<16} plays={plays:<4} bytes={size}")
    return entry


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, help="4-digit season year")
    p.add_argument("--seasontype", help="pre|reg|post or 1|2|3")
    p.add_argument("--week", type=int)
    p.add_argument("--event", help="record a single event id instead of a slate")
    p.add_argument("--limit", type=int, default=3, help="max games to record from a slate")
    p.add_argument("--force", action="store_true", help="re-download even if file exists")
    a = p.parse_args()

    index = {e["id"]: e for e in load_index()}

    if a.event:
        entry = record_event(a.event, a.force)
        if entry:
            index[entry["id"]] = entry
        save_index(list(index.values()))
        return

    if a.seasontype:
        seasontype = SEASONTYPE_WORDS.get(a.seasontype, None)
        if seasontype is None:
            try:
                seasontype = int(a.seasontype)
            except ValueError:
                p.error("--seasontype must be pre/reg/post or 1/2/3")
    else:
        seasontype = None

    if not (a.year and seasontype):
        p.error("--year and --seasontype are required unless --event is given")

    events = fetch_slate(a.year, seasontype, a.week)
    print(f"slate: {len(events)} events, recording up to {a.limit} completed games\n")

    recorded = taken = 0
    for e in events:
        if taken >= a.limit:
            break
        state = e["status"]["type"]["state"]
        if state != "post":
            print(f"  {e['id']} skip ({e['shortName']}, status={state})")
            continue
        # Count already-recorded games toward the limit so re-running is a no-op
        # rather than quietly pulling down another --limit games each time.
        taken += 1
        entry = record_event(
            e["id"], a.force, season_type=seasontype, week=a.week, short_name=e["shortName"]
        )
        if entry:
            index[entry["id"]] = entry
            recorded += 1

    save_index(list(index.values()))
    print(f"\nrecorded {recorded} new game(s), index has {len(index)} total entries")


if __name__ == "__main__":
    main()
