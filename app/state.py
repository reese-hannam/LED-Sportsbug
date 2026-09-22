"""Shared state between the polling thread, the render loop, and the web UI.

The render loop must never block on the network, so the poller writes a finished
game list here and the renderer reads whatever is current at 30fps. The web UI
only ever mutates settings; it never touches the matrix directly.
"""

import datetime
import threading
import time
import uuid

from . import config
from .config import SPORTS
from .bets import BetBook, parlay_status, goal_text
from .fantasy import FantasyBook
from .fantasy_events import (CATEGORIES as F_CATEGORIES, DEFAULT_STYLES as F_STYLES,
                             CATEGORY_LABEL as F_LABEL, DURATION as F_DURATION,
                             MIN_DURATION, MAX_DURATION)
from .bet_events import (CATEGORIES as B_CATEGORIES, DEFAULT_STYLES as B_STYLES,
                         CATEGORY_LABEL as B_LABEL, DURATION as B_DURATION,
                         DEFAULT_PAGE_SECONDS as B_PAGE_SECONDS,
                         LEGS_PER_PAGE as B_LEGS_PER_PAGE)
from .delay import DelayBuffer
from .render.schedule_bug import LOOKS as SCHEDULE_LOOKS, LOOK_LABEL as SCHEDULE_LABEL, ROWS as SCHEDULE_ROWS
from .render import lifestyle_clock, lifestyle_countdown, lifestyle_faces
from .render import lifestyle_theme as theme
from .sources.base import Game, PRE, sort_games
from .sources.replay import list_recordings
from .sources.teams import teams as team_roster


def _logo_counts() -> dict:
    """How much artwork is actually on disk, so the UI can say so rather than
    offering a toggle that silently does nothing."""
    try:
        from .render import logos
        return {sport: logos.available(sport) for sport in SPORTS}
    except Exception:
        return {}


def _cfb_conferences() -> list:
    """Conference list for the category picker and the favourites grouping."""
    try:
        from .sources.cfb_meta import CONFERENCES
        return list(CONFERENCES)
    except Exception:
        return []


class AppState:
    def __init__(self, mode="mlb", rotate_seconds=10.0, brightness=60):
        self._lock = threading.Lock()
        self._mode = mode
        # Which sports the PANEL is currently cycling. `mode` is a different
        # thing: it's which sport's settings the control center is showing, so
        # you can edit your CFB filters while the panel runs baseball.
        #
        # Ordered, and the order is the rotation order -- all live NFL, then all
        # live MLB, rather than interleaving them. Sports jumping back and forth
        # mid-rotation is much harder to follow than one block per sport.
        self._active_sports: list[str] = [mode]
        # When nothing anywhere is live, cycle each active sport's upcoming
        # games instead of sitting on an idle screen. See `rotation()`.
        self.schedule_mode = True
        self.schedule_with_live: dict = {s_: False for s_ in SPORTS}
        # Which pre-game layout to draw. See render/schedule_bug.py.
        self.schedule_look = SCHEDULE_ROWS
        self.rotate_seconds = rotate_seconds
        self.brightness = brightness
        # When set, this game holds the screen until it's cleared.
        self.pinned_id: str | None = None

        # A scripted demo (app/demo.py) has the panel: its invented games
        # replace the real ones, the poller stands aside, and nothing it does
        # is written to config.json.
        self.demo_running = False
        self.demo_step = ""
        self._demo_games: list | None = None
        # Rotate through every game rather than just the live ones.
        self.include_finished = False
        # Team logos on the score bugs, in place of the colour spine. Per
        # sport -- see the note in render/logos.py.
        self.show_logos: dict = {"mlb": True, "nfl": True, "cfb": False}
        # Per sport: rotate only games involving a favourite team. Applies to
        # live and scheduled games alike -- see _blocks().
        self.favorites_only: dict = {s_: False for s_ in SPORTS}

        # NFL source parameters, settable from the UI.
        self.nfl_seasontype = 2
        self.nfl_year: int | None = None
        self.nfl_week: int | None = None
        # Recording id to replay instead of hitting the live NFL feed (None = live).
        self.replay_id: str | None = None
        self.replay_speed = 8.0
        # MLB date override (None = today).
        self.mlb_date: str | None = None

        # Favorite teams per sport. A scoring change in any game involving one
        # of these interrupts the display with an alert.
        self.favorites: dict[str, list[str]] = {s: [] for s in SPORTS}
        # College football only: which categories of game to rotate through
        # (top25 / top50 / conf:<id>). Empty means everything.
        self.cfb_filters: list[str] = []
        # Lazily created; only college football needs it.
        self._rankings = None
        self.alerts_enabled = True
        self.alert_seconds = 8.0

        # Broadcast delay: everything the panel and control center show is held
        # back by this many seconds, so it can never get ahead of a TV that has
        # its own lag. 0 = off. See app/delay.py.
        self.broadcast_delay = 0.0
        self._delay_buffer = DelayBuffer()

        # Bets and their live progress. Progress is computed by the poller (it
        # needs network) and buffered through the SAME delay as the game state --
        # a prop bar filling in early is exactly the spoiler the delay exists to
        # prevent.
        self.bets = BetBook()
        # Fantasy rosters, plus the per-category alert style (full/banner/off).
        self.fantasy = FantasyBook()
        self.fantasy_styles: dict = dict(F_STYLES)
        self.fantasy_durations: dict = dict(F_DURATION)
        self.bet_styles: dict = dict(B_STYLES)
        self.bet_durations: dict = dict(B_DURATION)
        # After a parlay's run of leg cards, show the whole ticket once, four
        # legs to a screen. Only after full-screen cards -- see
        # bet_events.add_tickets.
        self.parlay_context = False
        # Bet and parlay screens between games in the rotation. Alerts happen
        # either way; this is only whether bets also get screen time.
        self.bets_in_rotation = True
        self.parlay_page_seconds = B_PAGE_SECONDS
        self._bet_progress: list = []
        self._bet_progress_by_sport: dict[str, list] = {}
        self._bet_delay_buffer = DelayBuffer()

        # Per sport, because each has its own source polling on its own
        # interval -- a slow CFB fetch must not blank the MLB games.
        self._by_sport: dict[str, list[Game]] = {}
        self.last_poll = 0.0
        self.last_error = ""
        # Bumped whenever the source needs rebuilding; the poller watches this.
        self.source_generation = 0

        # Wi-Fi, written by app.wifi.watch(). Not persisted -- it's a fact
        # about right now, and a stale one at boot would be worse than none.
        self.network: dict = {"supported": False, "online": False,
                              "hotspot": False, "ssid": None, "ip": None}

        # Lifestyle section: on, the panel rotates these instead of games.
        # Sports polling, favourites and alerts all keep running underneath
        # regardless of this switch -- see main.py's poller and rotation.
        self.lifestyle_mode = False
        # Let a favourite team's score still interrupt the clock. Bet and
        # fantasy alerts never do -- see main.py's _alerts_for_mode.
        self.lifestyle_alerts = True
        self.clock_enabled = False
        self.clock_style = lifestyle_clock.STYLES[0]
        self.clock_seconds = False
        self.clock_show_date = False
        self.clock_analog_digital = False
        # Per lifestyle screen: colours and how much of the panel to fill.
        # See render/lifestyle_theme.py -- white and 100% by default.
        self.lifestyle_colors: dict = {s: dict(c) for s, c in theme.DEFAULTS.items()}
        self.lifestyle_scale: dict = {s: 100 for s in theme.SCREENS}
        self.weather_enabled = False
        self.weather_show_time = True
        self.weather_show_conditions = True
        self.weather_show_hilo = False
        self.weather_show_sun = True
        self.weather_location = ""
        self.weather_units = "f"
        # Geocoded once by the web layer and cached here so a restart doesn't
        # need to re-resolve a place name before the first forecast.
        self.weather_lat: float | None = None
        self.weather_lon: float | None = None
        self.weather_resolved_name = ""
        # Last fetched forecast, written by the poller -- see set_weather().
        self.weather: dict | None = None
        self.weather_fetched_at = 0.0
        self.weather_failed = False
        # Bumped when the forecast on file no longer matches what's asked for
        # (new place, different units); the poller refetches at once rather
        # than leaving a Fahrenheit number relabelled as Celsius on screen.
        self.weather_generation = 0
        self.message_enabled = False
        self.message_text = ""
        self.countdowns: list[dict] = []
        self.faces: list[dict] = []

    # -- favorites ----------------------------------------------------------

    def toggle_favorite(self, sport: str, abbrev: str) -> bool:
        """Returns whether the team is a favorite after the toggle."""
        abbrev = abbrev.upper()
        picks = self.favorites.setdefault(sport, [])
        with self._lock:
            if abbrev in picks:
                picks.remove(abbrev)
                on = False
            else:
                picks.append(abbrev)
                on = True
        self.persist()
        return on

    def is_favorite_game(self, game: Game) -> bool:
        picks = self.favorites.get(game.sport, [])
        return bool(picks) and (game.away.abbrev in picks or game.home.abbrev in picks)

    # -- persistence --------------------------------------------------------

    def persist(self) -> None:
        if self.demo_running:
            # The demo installs its own bets and settings for two minutes.
            # Writing those to config.json would make a film shoot permanent.
            return
        config.save({
            "mode": self.mode,
            "active_sports": self.active_sports,
            "schedule_mode": self.schedule_mode,
            "schedule_with_live": self.schedule_with_live,
            "schedule_look": self.schedule_look,
            "rotate_seconds": self.rotate_seconds,
            "brightness": self.brightness,
            "include_finished": self.include_finished,
            "show_logos": self.show_logos,
            "favorites_only": self.favorites_only,
            "favorites": self.favorites,
            "alerts_enabled": self.alerts_enabled,
            "alert_seconds": self.alert_seconds,
            "replay_speed": self.replay_speed,
            "broadcast_delay": self.broadcast_delay,
            "cfb_filters": self.cfb_filters,
            **self.bets.to_json(),
            **self.fantasy.to_json(),
            "fantasy_styles": self.fantasy_styles,
            "fantasy_durations": self.fantasy_durations,
            "bet_styles": self.bet_styles,
            "bet_durations": self.bet_durations,
            "parlay_context": self.parlay_context,
            "bets_in_rotation": self.bets_in_rotation,
            "parlay_page_seconds": self.parlay_page_seconds,
            "lifestyle_mode": self.lifestyle_mode,
            "lifestyle_alerts": self.lifestyle_alerts,
            "clock_enabled": self.clock_enabled,
            "clock_style": self.clock_style,
            "clock_seconds": self.clock_seconds,
            "clock_show_date": self.clock_show_date,
            "clock_analog_digital": self.clock_analog_digital,
            "lifestyle_colors": self.lifestyle_colors,
            "lifestyle_scale": self.lifestyle_scale,
            "weather_enabled": self.weather_enabled,
            "weather_show_time": self.weather_show_time,
            "weather_show_conditions": self.weather_show_conditions,
            "weather_show_hilo": self.weather_show_hilo,
            "weather_show_sun": self.weather_show_sun,
            "weather_location": self.weather_location,
            "weather_units": self.weather_units,
            "weather_lat": self.weather_lat,
            "weather_lon": self.weather_lon,
            "weather_resolved_name": self.weather_resolved_name,
            "message_enabled": self.message_enabled,
            "message_text": self.message_text,
            "countdowns": self.countdowns,
            "faces": self.faces,
        })

    def apply(self, data: dict) -> None:
        self._mode = data.get("mode", self._mode)
        stored_active = data.get("active_sports")
        if isinstance(stored_active, list):
            picked = [str(x) for x in stored_active if str(x) in SPORTS]
            # A config that somehow names no valid sport must not leave the
            # panel with nothing to rotate.
            self._active_sports = list(dict.fromkeys(picked)) or [self._mode]
        else:
            # Upgrading from a single-sport config: whatever mode was saved.
            self._active_sports = [self._mode]
        self.schedule_mode = bool(data.get("schedule_mode", self.schedule_mode))
        # Per sport, and tolerant of the bool this used to be: an existing
        # config.json written before it was split still loads, applying the old
        # single answer to every sport rather than silently resetting.
        stored_swl = data.get("schedule_with_live")
        if isinstance(stored_swl, bool):
            self.schedule_with_live = {s_: stored_swl for s_ in SPORTS}
        elif isinstance(stored_swl, dict):
            self.schedule_with_live = {s_: bool(stored_swl.get(s_, False)) for s_ in SPORTS}
        look = data.get("schedule_look")
        if look in SCHEDULE_LOOKS:
            self.schedule_look = look
        self.rotate_seconds = data.get("rotate_seconds", self.rotate_seconds)
        self.brightness = data.get("brightness", self.brightness)
        self.include_finished = data.get("include_finished", self.include_finished)
        stored_only = data.get("favorites_only")
        if isinstance(stored_only, dict):
            self.favorites_only = {s_: bool(stored_only.get(s_, False)) for s_ in SPORTS}

        stored_logos = data.get("show_logos")
        if isinstance(stored_logos, dict):
            self.show_logos = {s_: bool(stored_logos.get(s_, self.show_logos.get(s_)))
                               for s_ in SPORTS}
        elif stored_logos is not None:
            # An older config stored one bool for every sport.
            self.show_logos = {s_: bool(stored_logos) for s_ in SPORTS}
        self.alerts_enabled = data.get("alerts_enabled", self.alerts_enabled)
        self.alert_seconds = data.get("alert_seconds", self.alert_seconds)
        self.replay_speed = data.get("replay_speed", self.replay_speed)
        self.broadcast_delay = data.get("broadcast_delay", self.broadcast_delay)
        self.bets.load(data)
        self.fantasy.load(data)
        self.parlay_context = bool(data.get("parlay_context", self.parlay_context))
        self.bets_in_rotation = bool(data.get("bets_in_rotation", self.bets_in_rotation))
        try:
            self.parlay_page_seconds = max(MIN_DURATION, min(
                MAX_DURATION, float(data.get("parlay_page_seconds",
                                             self.parlay_page_seconds))))
        except (TypeError, ValueError):
            pass
        for key, cats, styles, durations, defaults in (
            ("fantasy", F_CATEGORIES, self.fantasy_styles, self.fantasy_durations, F_DURATION),
            ("bet", B_CATEGORIES, self.bet_styles, self.bet_durations, B_DURATION),
        ):
            stored_styles = data.get(f"{key}_styles") or {}
            stored_durations = data.get(f"{key}_durations") or {}
            for cat in cats:
                if stored_styles.get(cat) in ("full", "banner", "off"):
                    styles[cat] = stored_styles[cat]
                try:
                    val = float(stored_durations.get(cat, defaults[cat]))
                    durations[cat] = max(MIN_DURATION, min(MAX_DURATION, val))
                except (TypeError, ValueError):
                    pass
        favs = data.get("favorites") or {}
        for sport in SPORTS:
            self.favorites[sport] = list(favs.get(sport, []))
        stored_filters = data.get("cfb_filters")
        if isinstance(stored_filters, list):
            # Validated on the way in as well as at the API, so a hand-edited or
            # stale config can't carry a dead conference id forward -- it would
            # match nothing and quietly shrink the slate.
            from .sources.cfb_meta import CONFERENCES
            valid = {"all", "top25", "top50"}
            valid |= {f"conf:{c['id']}" for c in CONFERENCES}
            self.cfb_filters = [str(x) for x in stored_filters if str(x) in valid]

        self.lifestyle_mode = bool(data.get("lifestyle_mode", self.lifestyle_mode))
        self.lifestyle_alerts = bool(data.get("lifestyle_alerts", self.lifestyle_alerts))
        self.clock_enabled = bool(data.get("clock_enabled", self.clock_enabled))
        style = data.get("clock_style")
        if style in lifestyle_clock.STYLES:
            self.clock_style = style
        self.clock_seconds = bool(data.get("clock_seconds", self.clock_seconds))
        self.clock_show_date = bool(data.get("clock_show_date", self.clock_show_date))
        self.clock_analog_digital = bool(
            data.get("clock_analog_digital", self.clock_analog_digital))

        # Same validation the settings API uses -- a hand-edited config can't
        # put an unparseable colour or a 900% scale on the panel. Anything
        # rejected here just keeps its default rather than raising: a bad
        # config file should cost you one setting, not the whole display.
        theme.apply_colors(self.lifestyle_colors, data.get("lifestyle_colors"))
        theme.apply_scale(self.lifestyle_scale, data.get("lifestyle_scale"))

        self.weather_enabled = bool(data.get("weather_enabled", self.weather_enabled))
        for key in ("weather_show_time", "weather_show_conditions",
                    "weather_show_hilo", "weather_show_sun"):
            setattr(self, key, bool(data.get(key, getattr(self, key))))
        self.weather_location = str(data.get("weather_location") or "")
        units = data.get("weather_units")
        if units in ("f", "c"):
            self.weather_units = units
        lat, lon = data.get("weather_lat"), data.get("weather_lon")
        self.weather_lat = float(lat) if isinstance(lat, (int, float)) else None
        self.weather_lon = float(lon) if isinstance(lon, (int, float)) else None
        self.weather_resolved_name = str(data.get("weather_resolved_name") or "")

        self.message_enabled = bool(data.get("message_enabled", self.message_enabled))
        self.message_text = str(data.get("message_text") or "")[:200]

        stored_countdowns = data.get("countdowns")
        if isinstance(stored_countdowns, list):
            valid_cds = []
            for c in stored_countdowns:
                if not isinstance(c, dict) or "target_iso" not in c:
                    continue
                try:
                    datetime.datetime.fromisoformat(str(c["target_iso"]))
                except ValueError:
                    continue
                valid_cds.append({
                    "id": str(c.get("id") or uuid.uuid4().hex[:8]),
                    "label": str(c.get("label") or "")[:40],
                    "target_iso": str(c["target_iso"]),
                })
            self.countdowns = valid_cds

        stored_faces = data.get("faces")
        if isinstance(stored_faces, list):
            # A hand-edited or stale config can name a layout that no longer
            # exists or a countdown that's since been deleted -- cleaned here
            # the same way cfb_filters is above, so a bad face can't crash a
            # render instead of just showing blank slots.
            valid_ids = {c["id"] for c in self.countdowns}
            faces = []
            for f in stored_faces:
                if not isinstance(f, dict) or f.get("layout") not in lifestyle_faces.LAYOUTS:
                    continue
                n = lifestyle_faces.slot_count(f["layout"])
                slots = f.get("slots") or []
                cleaned = []
                for i in range(n):
                    slot = slots[i] if i < len(slots) and isinstance(slots[i], dict) else {}
                    kind = slot.get("kind")
                    if kind not in lifestyle_faces.COMPLICATIONS:
                        cleaned.append({})
                        continue
                    entry = {"kind": kind}
                    if kind == "countdown":
                        cid = slot.get("countdown_id")
                        entry["countdown_id"] = cid if cid in valid_ids else None
                    cleaned.append(entry)
                faces.append({
                    "id": str(f.get("id") or uuid.uuid4().hex[:8]),
                    "name": str(f.get("name") or "Face")[:24],
                    "layout": f["layout"],
                    "slots": cleaned,
                    "active": bool(f.get("active", True)),
                })
            self.faces = faces

    # -- mode ---------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """Focus a sport's settings in the control center.

        This no longer changes what the panel shows -- `active_sports` does
        that -- so switching tabs to check your fantasy roster doesn't take
        baseball off the display.
        """
        if mode not in SPORTS:
            raise ValueError(f"unknown mode: {mode}")
        with self._lock:
            self._mode = mode

    # -- active sports ------------------------------------------------------

    @property
    def active_sports(self) -> list[str]:
        with self._lock:
            return list(self._active_sports)

    def set_active_sports(self, sports) -> list[str]:
        """Which sports the panel cycles. Order is preserved as rotation order.

        Turning every sport off would leave the panel with nothing to draw, so
        an empty selection falls back to the focused sport rather than a blank
        screen.
        """
        picked = [s for s in dict.fromkeys(sports) if s in SPORTS]
        if not picked:
            picked = [self._mode]
        with self._lock:
            if picked == self._active_sports:
                return list(picked)
            dropped = set(self._active_sports) - set(picked)
            self._active_sports = picked
            # Drop the de-selected sports' games immediately; leaving them up
            # for a poll cycle looks like the toggle didn't work.
            for sport in dropped:
                self._by_sport.pop(sport, None)
            self._delay_buffer.clear()
            if self.pinned_id and not any(
                g.id == self.pinned_id for g in self._merged()
            ):
                self.pinned_id = None
            self.last_poll = 0.0
            self.source_generation += 1
        return list(picked)

    def _merged(self) -> list[Game]:
        """All active sports' games, in active-sport order. Caller holds the lock."""
        out: list[Game] = []
        for sport in self._active_sports:
            out.extend(self._by_sport.get(sport, []))
        return out

    def invalidate_source(self, sport: str | None = None) -> None:
        """Force the poller to rebuild. `sport` limits the games dropped, so
        changing the NFL week doesn't blank the baseball half of the panel."""
        with self._lock:
            self.source_generation += 1
            if sport is None:
                self._by_sport.clear()
            else:
                self._by_sport.pop(sport, None)
            self._delay_buffer.clear()
            self.last_poll = 0.0

    # -- games --------------------------------------------------------------

    def set_games(self, sport: str, games: list[Game], generation: int) -> None:
        """Replace one sport's games. Other active sports are left alone."""
        with self._lock:
            # A poll that started before a sport was switched off must not
            # resurrect it.
            if generation != self.source_generation:
                return
            if sport not in self._active_sports:
                return
            self._by_sport[sport] = sort_games(games)
            self._delay_buffer.push(self._merged())
            self.last_poll = time.time()
            self.last_error = ""

    def set_error(self, msg: str) -> None:
        with self._lock:
            self.last_error = msg

    @property
    def games(self) -> list[Game]:
        """What the panel and control center should show RIGHT NOW -- i.e. what
        was actually true `broadcast_delay` seconds ago. This is the one place
        the delay is applied; everything downstream (rotation, pinning, the web
        snapshot) is automatically delayed by using this instead of raw state.

        The poller's own event detection reads its freshly-fetched list directly,
        never this property, so scoring lookups always match against the true
        current score even while the display lags behind it.
        """
        with self._lock:
            if self._demo_games is not None:
                # A demo run owns the panel. Its games aren't delayed; they're
                # invented, and they are the whole point of what's on screen.
                return list(self._demo_games)
            buffered = self._delay_buffer.at(self.broadcast_delay)
            return list(buffered) if buffered is not None else self._merged()

    def set_demo_games(self, games: list | None) -> None:
        """Install the demo's games, or None to hand the panel back."""
        with self._lock:
            self._demo_games = list(games) if games is not None else None

    def display_age(self) -> float | None:
        """How stale what's on screen actually is, for the UI to report back."""
        with self._lock:
            return self._delay_buffer.age(self.broadcast_delay)

    # -- bet progress -------------------------------------------------------

    def set_bet_progress(self, sport: str, progress: list) -> None:
        """Replace one sport's bet progress, leaving the other sports' alone."""
        with self._lock:
            self._bet_progress_by_sport[sport] = list(progress)
            merged = [p for sp in self._active_sports
                      for p in self._bet_progress_by_sport.get(sp, [])]
            self._bet_progress = merged
            self._bet_delay_buffer.push(merged)

    @property
    def bet_progress(self) -> list:
        """Delayed the same as game state -- see the note in __init__."""
        with self._lock:
            buffered = self._bet_delay_buffer.at(self.broadcast_delay)
            return list(buffered) if buffered is not None else list(self._bet_progress)

    def _cfb_narrow(self, games: list[Game]) -> list[Game]:
        """Apply the college category filter. Favourites always survive it."""
        if not self.cfb_filters:
            return games
        from .sources.cfb import matches_filters
        if self._rankings is None:
            from .sources.cfb_meta import Rankings
            self._rankings = Rankings()
        picks = self.favorites.get("cfb", [])
        narrowed = [g for g in games
                    if matches_filters(g, self.cfb_filters, self._rankings, picks)]
        # Never filter the panel down to nothing; an empty screen reads as
        # broken, so fall back to the unfiltered slate.
        return narrowed or games

    def _blocks(self) -> list[list[Game]]:
        """Each active sport's games, in selection order, already narrowed.

        The single place every "which games" rule is applied, so the panel and
        the control center cannot disagree about what's in play -- they did
        once, and the list showing all 99 college games while the panel rotated
        25 made it look like favourites weren't surviving the filter.

        Two rules, per sport:

          * `favorites_only` cuts the sport to games involving a favourite. It
            supersedes the college category filter rather than combining with
            it -- "only my teams" is already the narrowest thing you can ask
            for, and intersecting it with a conference could only ever remove
            teams you explicitly picked.
          * otherwise college narrows by the selected categories.

        With `favorites_only` on and NO favourites set for that sport, the
        sport contributes nothing. That reads as harsh, but the alternative --
        quietly showing everything -- makes the switch look broken, and the
        control center says so next to the toggle.
        """
        out = []
        for sport in self.active_sports:
            games = [g for g in self.games if g.sport == sport]
            if self.favorites_only.get(sport):
                games = [g for g in games if self.is_favorite_game(g)]
            elif sport == "cfb":
                games = self._cfb_narrow(games)
            out.append(games)
        return out

    def visible_games(self) -> list[Game]:
        """Every game the panel could show -- what the control center lists.

        The same slice as `rotation()` before the live-only step, so the Games
        list matches what the panel is actually cycling through, including any
        favourite that the category filter would otherwise have excluded.
        """
        return [g for b in self._blocks() for g in b]

    def rotation(self) -> list[Game]:
        """What the display cycles through, in order.

        Sports are kept in BLOCKS, in the order they were selected: every live
        NFL game, then every live MLB game. Interleaving them would change
        sport every ten seconds, which is far harder to follow than one league
        at a time.

        College football is narrowed first by the selected categories (top 25 /
        top 50 / conferences) -- a full FBS Saturday is ~99 games, useless to
        rotate through. Favourite teams always survive that filter.

        When nothing anywhere is live, `schedule_mode` cycles each sport's
        UPCOMING games instead -- today's slate for baseball, the week's
        schedule for football -- rather than leaving the panel idle.

        `schedule_with_live` widens that to the case where something IS live,
        and it is per SPORT: the live games come first within their sport, then
        that sport's upcoming ones, but only for the sports it is switched on
        for. That split is the point -- with two baseball games on you may want
        those two plus the NFL's week ahead, and specifically NOT the rest of
        today's baseball.

        Off everywhere by default, because the usual reason this is on a wall
        during a game is to watch THAT game.
        """
        blocks = self._blocks()
        everything = [g for b in blocks for g in b]
        if self.include_finished:
            return everything

        live = [g for b in blocks for g in b if g.is_live]
        if live:
            # Per block, not live-then-everything-upcoming: keeping each
            # sport's live and scheduled games together is what stops the
            # rotation flipping league every ten seconds. And per SPORT,
            # because "the two baseball games that are on, plus the NFL
            # schedule" is a different ask from "everything upcoming" -- you
            # are watching one league and browsing another.
            out = []
            for sport, b in zip(self.active_sports, blocks):
                out += [g for g in b if g.is_live]
                if self.schedule_with_live.get(sport):
                    out += [g for g in b if g.state == PRE]
            return out or live

        # Nothing live. Show what's coming up, still sport by sport, dropping
        # games that already finished -- yesterday's final is not a schedule.
        # If that leaves nothing, fall back to the full slate so the panel
        # still has something to draw.
        if self.schedule_mode:
            upcoming = [g for b in blocks for g in b if g.state == PRE]
            if upcoming:
                return upcoming
        return everything

    def pinned(self) -> Game | None:
        pid = self.pinned_id
        if not pid:
            return None
        return next((g for g in self.games if g.id == pid), None)

    # -- lifestyle ------------------------------------------------------

    def set_network(self, info: dict) -> None:
        """Called by the Wi-Fi watcher. `known` rides along so the control
        center gets everything it needs from the one snapshot poll rather
        than a second request on its own timer."""
        with self._lock:
            self.network = dict(info)

    def set_weather(self, data: dict | None) -> None:
        """Called by the poller with a fresh forecast, or None on a failed
        fetch. Never persisted -- it's refetched on a timer.

        A failure deliberately LEAVES the last good reading in place. Blanking
        it would drop the weather screen out of the rotation entirely over one
        timed-out request, which reads as the feature breaking; an hour-old
        temperature is far better than no screen, and the control center
        reports the reading's age so a genuinely dead feed is still visible.
        """
        with self._lock:
            if data is None:
                self.weather_failed = True
                return
            self.weather = data
            self.weather_fetched_at = time.time()
            self.weather_failed = False

    def invalidate_weather(self, clear: bool = False) -> None:
        """Force the poller to refetch now. `clear` drops the current reading
        too, for when it's about somewhere else entirely."""
        with self._lock:
            self.weather_generation += 1
            if clear:
                self.weather = None
                self.weather_fetched_at = 0.0

    def set_weather_location(self, lat: float, lon: float, name: str) -> None:
        with self._lock:
            self.weather_lat = lat
            self.weather_lon = lon
            self.weather_resolved_name = name
            # Yesterday's weather for a city you just left is worse than a
            # blank screen for one poll, so this reading goes rather than
            # being kept the way a failed fetch's is.
            self.weather = None
            self.weather_fetched_at = 0.0
            self.weather_generation += 1
        self.persist()

    def add_countdown(self, label: str, target_iso: str) -> dict:
        cd = {"id": uuid.uuid4().hex[:8], "label": (label or "").strip()[:40],
              "target_iso": target_iso}
        with self._lock:
            self.countdowns.append(cd)
        self.persist()
        return cd

    def remove_countdown(self, countdown_id: str) -> bool:
        with self._lock:
            before = len(self.countdowns)
            self.countdowns = [c for c in self.countdowns if c["id"] != countdown_id]
            removed = before != len(self.countdowns)
            if removed:
                # A face slot pointing at a deleted countdown must not keep
                # naming it -- see lifestyle_faces._draw_slot's lookup.
                for face in self.faces:
                    for slot in face.get("slots") or []:
                        if slot.get("kind") == "countdown" and slot.get("countdown_id") == countdown_id:
                            slot["countdown_id"] = None
        if removed:
            self.persist()
        return removed

    def add_face(self, name: str, layout: str, slots: list) -> dict:
        face = {
            "id": uuid.uuid4().hex[:8],
            "name": (name or "").strip()[:24] or "Face",
            "layout": layout,
            "slots": slots,
            "active": True,
        }
        with self._lock:
            self.faces.append(face)
        self.persist()
        return face

    def remove_face(self, face_id: str) -> bool:
        with self._lock:
            before = len(self.faces)
            self.faces = [f for f in self.faces if f["id"] != face_id]
            removed = before != len(self.faces)
        if removed:
            self.persist()
        return removed

    def set_face_active(self, face_id: str, active: bool) -> bool:
        with self._lock:
            face = next((f for f in self.faces if f["id"] == face_id), None)
            if face is None:
                return False
            face["active"] = bool(active)
        self.persist()
        return True

    def lifestyle_screens(self) -> list:
        """Everything the panel cycles when `lifestyle_mode` is on, as
        (key, kind, payload) -- the lifestyle equivalent of rotation().

        A countdown that's more than a day past its target is left off the
        panel (see lifestyle_countdown.is_stale) but never deleted -- the
        same "stale, not gone" treatment _bets_json gives a bet whose game
        fell off the slate.
        """
        out = []
        if self.clock_enabled:
            out.append(("clock", "clock", None))
        if self.weather_enabled and self.weather is not None:
            out.append(("weather", "weather", None))
        if self.message_enabled and self.message_text.strip():
            out.append(("message", "message", None))
        for cd in self.countdowns:
            if not lifestyle_countdown.is_stale(cd):
                out.append(("countdown:" + cd["id"], "countdown", cd))
        for face in self.faces:
            if face.get("active"):
                out.append(("face:" + face["id"], "face", face))
        return out

    # -- serialization for the web UI ---------------------------------------

    def _fantasy_json(self) -> dict:
        """Teams with their rosters grouped by position, for the fantasy tab."""
        return {
            "teams": [
                {
                    "id": t.id,
                    "name": t.name,
                    "groups": [
                        {
                            "position": pos,
                            "players": [
                                {"id": p.id, "athlete_id": p.athlete_id, "name": p.name,
                                 "position": p.position, "team": p.team,
                                 "starting": p.starting}
                                for p in group
                            ],
                        }
                        for pos, group in self.fantasy.grouped_roster(t.id)
                    ],
                    "starters": sum(1 for p in self.fantasy.roster_of(t.id) if p.starting),
                    "total": len(self.fantasy.roster_of(t.id)),
                }
                for t in self.fantasy.teams
            ],
            "tracked": len(self.fantasy.tracked()),
        }

    def _bets_json(self) -> list[dict]:
        """Every bet, with its live progress folded in where we have it."""
        progress = {p.bet.id: p for p in self.bet_progress}
        # A bet whose game is no longer on the slate can't move again. It stays
        # listed -- losing a bet silently would be worse -- but it's flagged so
        # the control center can say why it's frozen, and it gets no panel time.
        on_slate = {g.id for g in self.games}
        out = []
        for bet in self.bets.bets:
            p = progress.get(bet.id)
            out.append({
                "id": bet.id,
                "sport": bet.sport,
                "game_id": bet.game_id,
                "player_name": bet.player_name,
                "team": bet.team,
                "stat": bet.stat,
                "stat_label": bet.stat_label,
                "direction": bet.direction,
                "goal": bet.goal,
                "goal_text": goal_text(bet),
                "parlay_id": bet.parlay_id,
                "summary": bet.summary,
                "value": p.value if p else None,
                "status": p.status if p else "pending",
                "fraction": p.fraction if p else 0.0,
                "display": p.display if p else None,
                "stale": bet.game_id not in on_slate,
                "resolved_at": bet.resolved_at,
                "expires_in": self.bets.retention_left(bet),
            })
        return out

    def _parlays_json(self) -> list[dict]:
        progress = {p.bet.id: p for p in self.bet_progress}
        out = []
        for parlay in self.bets.parlays:
            legs = [progress[b.id] for b in self.bets.legs_of(parlay.id) if b.id in progress]
            out.append({
                "id": parlay.id,
                "name": parlay.name,
                "leg_ids": [b.id for b in self.bets.legs_of(parlay.id)],
                "status": parlay_status(legs) if legs else "pending",
                "hit": sum(1 for p in legs if p.status == "hit"),
                "total": len(self.bets.legs_of(parlay.id)),
                "expires_in": self.bets.retention_left(parlay),
            })
        return out

    def snapshot(self, now_showing: str | None = None, alert_info: dict | None = None,
                 now_screen: str | None = None) -> dict:
        games = self.visible_games()
        return {
            # The rotation key of whatever is on the panel ("clock",
            # "face:ab12cd"). In lifestyle mode there's no game id to report,
            # so without this the control center could only say "—".
            "now_screen": now_screen,
            "demo": {"running": self.demo_running, "step": self.demo_step},
            "mode": self.mode,
            "active_sports": self.active_sports,
            "schedule_mode": self.schedule_mode,
            "schedule_with_live": self.schedule_with_live,
            "schedule_look": self.schedule_look,
            "schedule_looks": [{"key": k, "label": SCHEDULE_LABEL[k]} for k in SCHEDULE_LOOKS],
            "sports": list(SPORTS),
            "rotate_seconds": self.rotate_seconds,
            "brightness": self.brightness,
            "pinned_id": self.pinned_id,
            "include_finished": self.include_finished,
            "show_logos": self.show_logos,
            "favorites_only": self.favorites_only,
            "logo_counts": _logo_counts(),
            "now_showing": now_showing,
            "last_poll": self.last_poll,
            "stale_seconds": round(time.time() - self.last_poll, 1) if self.last_poll else None,
            "last_error": self.last_error,
            "nfl": {
                "seasontype": self.nfl_seasontype,
                "year": self.nfl_year,
                "week": self.nfl_week,
            },
            "replay_id": self.replay_id,
            "replay_speed": self.replay_speed,
            "recordings": list_recordings(),
            "mlb_date": self.mlb_date,
            "favorites": self.favorites.get(self.mode, []),
            "cfb_filters": self.cfb_filters,
            "cfb_conferences": _cfb_conferences(),
            "all_favorites": self.favorites,
            "roster": team_roster(self.mode),
            "alerts_enabled": self.alerts_enabled,
            "alert_seconds": self.alert_seconds,
            "alert": alert_info,
            "broadcast_delay": self.broadcast_delay,
            "broadcast_delay_actual": self.display_age(),
            "bets": self._bets_json(),
            "parlays": self._parlays_json(),
            "fantasy": self._fantasy_json(),
            "fantasy_styles": self.fantasy_styles,
            "fantasy_durations": self.fantasy_durations,
            "fantasy_categories": [
                {"key": c, "label": F_LABEL[c]} for c in F_CATEGORIES
            ],
            "bet_styles": self.bet_styles,
            "bet_durations": self.bet_durations,
            "bet_categories": [
                {"key": c, "label": B_LABEL[c]} for c in B_CATEGORIES
            ],
            "parlay_context": self.parlay_context,
            "bets_in_rotation": self.bets_in_rotation,
            "parlay_page_seconds": self.parlay_page_seconds,
            "parlay_legs_per_page": B_LEGS_PER_PAGE,
            "duration_range": [MIN_DURATION, MAX_DURATION],
            "network": self.network,
            "lifestyle_mode": self.lifestyle_mode,
            "lifestyle_alerts": self.lifestyle_alerts,
            "clock_enabled": self.clock_enabled,
            "clock_style": self.clock_style,
            "clock_seconds": self.clock_seconds,
            "clock_show_date": self.clock_show_date,
            "clock_analog_digital": self.clock_analog_digital,
            "lifestyle_colors": self.lifestyle_colors,
            "lifestyle_scale": self.lifestyle_scale,
            "color_presets": [{"name": n, "hex": h} for n, h in theme.PRESETS],
            "scale_range": [theme.MIN_SCALE, theme.MAX_SCALE],
            "theme_screens": list(theme.SCREENS),
            "weather_show_time": self.weather_show_time,
            "weather_show_conditions": self.weather_show_conditions,
            "weather_show_hilo": self.weather_show_hilo,
            "weather_show_sun": self.weather_show_sun,
            "clock_styles": [{"key": k, "label": lifestyle_clock.LABEL[k]}
                             for k in lifestyle_clock.STYLES],
            "weather_enabled": self.weather_enabled,
            "weather_location": self.weather_location,
            "weather_resolved_name": self.weather_resolved_name,
            "weather_units": self.weather_units,
            "weather": self.weather,
            "weather_age": (round(time.time() - self.weather_fetched_at)
                            if self.weather_fetched_at else None),
            "weather_failed": self.weather_failed,
            "message_enabled": self.message_enabled,
            "message_text": self.message_text,
            "countdowns": [
                {**cd, "remaining_seconds": lifestyle_countdown.remaining_seconds(cd),
                 "stale": lifestyle_countdown.is_stale(cd)}
                for cd in self.countdowns
            ],
            "faces": self.faces,
            "face_layouts": [
                {"key": k, "label": lifestyle_faces.LABEL[k], "slots": lifestyle_faces.slot_count(k)}
                for k in lifestyle_faces.LAYOUTS
            ],
            "face_complications": [
                {"key": k, "label": lifestyle_faces.COMPLICATION_LABEL[k]}
                for k in lifestyle_faces.COMPLICATIONS
            ],
            "counts": {
                "live": sum(1 for g in games if g.is_live),
                "total": len(games),
            },
            "games": [
                {
                    "id": g.id,
                    "sport": g.sport,
                    "away": {"abbrev": g.away.abbrev, "score": g.away.score,
                             "color": "#%02x%02x%02x" % g.away.color, "record": g.away.record},
                    "home": {"abbrev": g.home.abbrev, "score": g.home.score,
                             "color": "#%02x%02x%02x" % g.home.color, "record": g.home.record},
                    "state": g.state,
                    "period": g.period,
                    "status_detail": g.status_detail,
                    "start_utc": g.start_utc,
                    "detail": g.detail,
                    "favorite": self.is_favorite_game(g),
                }
                for g in games
            ],
        }
