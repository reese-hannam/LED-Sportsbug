"""Settings that should survive a restart.

Favorite teams especially -- re-picking them every boot would make the device
annoying to actually live with. Kept as one small JSON file; on the Pi this
becomes the same file the web UI writes, so nothing else needs to change.
"""

import json
import os
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

DEFAULTS = {
    "mode": "mlb",
    # Empty on purpose: AppState reads that as "carry the saved `mode` over",
    # which is what upgrading a single-sport config should do.
    "active_sports": [],
    "schedule_mode": True,
    "schedule_with_live": {"mlb": False, "nfl": False, "cfb": False},
    # How a not-yet-started game is drawn: "rows" or "matchup".
    "schedule_look": "rows",
    "rotate_seconds": 10.0,
    "brightness": 60,
    "include_finished": False,
    # College is off by default: at the 12px the rank column leaves room for,
    # most college crests read as a coloured blob. Turn it on and judge.
    "show_logos": {"mlb": True, "nfl": True, "cfb": False},
    # Narrow a sport to just your favourite teams' games. Per sport, so the
    # whole NFL slate and only your two baseball teams is a valid setup.
    "favorites_only": {"mlb": False, "nfl": False, "cfb": False},
    "favorites": {"mlb": [], "nfl": [], "cfb": []},
    "alerts_enabled": True,
    "alert_seconds": 8.0,
    "replay_speed": 8.0,
    "broadcast_delay": 0.0,
    "bets": [],
    "parlays": [],
    "fantasy_teams": [],
    "fantasy_players": [],
    "fantasy_styles": {},
    "fantasy_durations": {},
    "bet_styles": {},
    "bet_durations": {},
    # Follow a full-screen parlay-leg alert with the whole ticket, paged.
    "parlay_context": False,
    "bets_in_rotation": True,
    "parlay_page_seconds": 4.0,
    "cfb_filters": [],
    # Lifestyle section: on, the panel rotates these instead of games. Sports
    # polling and alerts keep running underneath either way -- see AppState.
    "lifestyle_mode": False,
    # In lifestyle mode, let a favourite team's score still interrupt. Nothing
    # else sports-related gets through -- see main.py's _alerts_for_mode.
    "lifestyle_alerts": True,
    "clock_enabled": False,
    "clock_style": "segment",
    "clock_seconds": False,
    "clock_show_date": False,
    # Analog only: a digital readout in a window on the lower dial.
    "clock_analog_digital": False,
    # Per screen: {"main": "#RRGGBB", "accent": "#RRGGBB"} and a size percent.
    # White by default -- a colour should be a choice, not something to undo.
    "lifestyle_colors": {},
    "lifestyle_scale": {},
    "weather_enabled": False,
    "weather_show_time": True,
    "weather_show_conditions": True,
    "weather_show_hilo": False,
    "weather_show_sun": True,
    "weather_location": "",
    "weather_units": "f",
    # Geocoded once by /api/weather/location and cached here so a restart
    # doesn't need to re-resolve a place name before the first forecast.
    "weather_lat": None,
    "weather_lon": None,
    "weather_resolved_name": "",
    "message_enabled": False,
    "message_text": "",
    "countdowns": [],
    "faces": [],
}

# Every sport the app knows about. College football is scores + favourite-team
# alerts only -- no fantasy or betting -- so it appears here but not in the
# bet/fantasy plumbing.
SPORTS = ("mlb", "nfl", "cfb")


def load() -> dict:
    data = dict(DEFAULTS)
    data["favorites"] = {s: [] for s in SPORTS}
    try:
        with open(CONFIG_PATH) as f:
            stored = json.load(f)
    except (OSError, json.JSONDecodeError):
        return data

    if not isinstance(stored, dict):
        return data
    for key, value in stored.items():
        if key not in DEFAULTS:
            continue
        if key == "favorites" and isinstance(value, dict):
            for sport in SPORTS:
                picks = value.get(sport)
                if isinstance(picks, list):
                    data["favorites"][sport] = [str(a).upper() for a in picks]
        else:
            data[key] = value
    return data


def save(data: dict) -> None:
    """Atomic write -- a truncated config after a power cut would be worse than none."""
    payload = {k: data.get(k, v) for k, v in DEFAULTS.items()}
    try:
        fd, tmp = tempfile.mkstemp(dir=PROJECT_ROOT, prefix=".config-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        # A read-only filesystem shouldn't take the display down.
        pass
