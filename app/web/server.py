"""Control center — the same web UI that will run on the Pi.

Deliberately dependency-light on the client side: one HTML file, no build step,
polling fetch. It has to be usable from a phone over the Pi's own Wi-Fi.
"""

import os
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..bets import Bet, OVER, UNDER, normalize_goal
from ..config import SPORTS
from ..fantasy import FantasyPlayer, POSITIONS, DEF_PREFIX
from ..fantasy_events import CATEGORIES, MIN_DURATION, MAX_DURATION
from ..bet_events import (CATEGORIES as BET_CATEGORIES, LEG_HIT, PROGRESS)
from ..events import Event, SCORE, score_label
from ..sources.base import Team
from ..tracker import roster as team_roster, curated_roster, stat_catalog
from ..sources.cfb_meta import CONFERENCES as cfb_confs, cfb_teams_by_conference


def cfb_conferences():
    return cfb_confs

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class NewBet(BaseModel):
    sport: str
    game_id: str
    player_id: str
    player_name: str
    team: str
    stat: str
    direction: str
    # The number to reach (over) or not pass (under). Whole numbers; a book
    # line typed out of habit is rounded to the goal it implies -- see
    # bets.normalize_goal. Optional only because a fixed-goal market like
    # anytime TD has nothing to ask.
    goal: float | None = None
    parlay_id: str | None = None


class NewParlay(BaseModel):
    name: str = "Parlay"
    bet_ids: list[str] = []


class NewFantasyTeam(BaseModel):
    name: str = "My Team"


class NewFantasyPlayer(BaseModel):
    team_id: str
    athlete_id: str
    name: str
    position: str
    team: str = ""


class AlertPrefs(BaseModel):
    styles: dict = {}
    durations: dict = {}


class ParlayContext(BaseModel):
    enabled: bool | None = None
    page_seconds: float | None = None


class ActiveSports(BaseModel):
    sports: list[str] = []


class CFBFilters(BaseModel):
    filters: list = []


class NewCountdown(BaseModel):
    label: str = ""
    target_iso: str


class FaceSlot(BaseModel):
    kind: str
    countdown_id: str | None = None


class NewFace(BaseModel):
    name: str = "Face"
    layout: str
    slots: list[FaceSlot] = []


class WeatherLocation(BaseModel):
    location: str


class WifiCredentials(BaseModel):
    ssid: str
    password: str | None = None


class Settings(BaseModel):
    show_logos: dict | None = None
    favorites_only: dict | None = None
    schedule_look: str | None = None
    rotate_seconds: float | None = None
    brightness: int | None = None
    include_finished: bool | None = None
    schedule_with_live: dict | None = None
    bets_in_rotation: bool | None = None
    nfl_seasontype: int | None = None
    nfl_year: int | None = None
    nfl_week: int | None = None
    replay_speed: float | None = None
    alerts_enabled: bool | None = None
    alert_seconds: float | None = None
    broadcast_delay: float | None = None
    mlb_date: str | None = None
    lifestyle_mode: bool | None = None
    lifestyle_alerts: bool | None = None
    clock_enabled: bool | None = None
    clock_style: str | None = None
    clock_seconds: bool | None = None
    clock_show_date: bool | None = None
    clock_analog_digital: bool | None = None
    lifestyle_colors: dict | None = None
    lifestyle_scale: dict | None = None
    weather_enabled: bool | None = None
    weather_show_time: bool | None = None
    weather_show_conditions: bool | None = None
    weather_show_hilo: bool | None = None
    weather_show_sun: bool | None = None
    weather_units: str | None = None
    message_enabled: bool | None = None
    message_text: str | None = None


def create_app(state, display, queue) -> FastAPI:
    """`display` carries live render info the state doesn't own (current game)."""
    app = FastAPI(title="LED Sports Bug", docs_url=None, redoc_url=None)

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    @app.get("/tv")
    def tv():
        """Fullscreen view of the panel, for a laptop or tablet.

        A viewer, not a second renderer: it consumes the frames the emulator
        already broadcasts, so it costs the render loop nothing.
        """
        return FileResponse(os.path.join(STATIC, "tv.html"))

    @app.get("/api/state")
    def get_state():
        return state.snapshot(
            now_showing=display.get("current_id"),
            alert_info=display.get("alert"),
            now_screen=display.get("current_screen"),
        )

    @app.post("/api/favorite/{sport}/{abbrev}")
    def favorite(sport: str, abbrev: str):
        if sport not in SPORTS:
            raise HTTPException(400, f"unknown sport: {sport}")
        on = state.toggle_favorite(sport, abbrev)
        return {"ok": True, "favorite": on, "favorites": state.favorites[sport]}

    @app.post("/api/favorites/{sport}/clear")
    def clear_favorites(sport: str):
        if sport not in SPORTS:
            raise HTTPException(400, f"unknown sport: {sport}")
        state.favorites[sport] = []
        state.persist()
        return {"ok": True, "favorites": []}

    @app.post("/api/alert/test")
    def test_alert(game_id: str | None = None):
        """Fire a synthetic alert so the popup can be checked without waiting.

        Deliberately bypasses broadcast_delay -- this is an explicit on-demand
        check, not a real event that could spoil anything.
        """
        games = state.games
        game = next((g for g in games if g.id == game_id), None) if game_id else None
        if game is None:
            game = next((g for g in games if state.is_favorite_game(g)), None)
        if game is None:
            game = games[0] if games else None
        if game is None:
            raise HTTPException(409, "no games loaded to build a test alert from")

        # College football scores like the NFL, so a test alert there should
        # read TOUCHDOWN, not the 1-point EXTRA POINT that baseball's "1 run" maps to.
        points = 7 if game.sport in ("nfl", "cfb") else 1
        queue.push(Event(
            kind=SCORE,
            game_id=game.id,
            sport=game.sport,
            team=game.home,
            opponent=game.away,
            points=points,
            label=score_label(game.sport, points),
            away=game.away,
            home=Team(game.home.abbrev, game.home.score + points,
                      game.home.color, game.home.name, game.home.record),
            period=game.period,
            key=f"test:{time.time()}",
            priority=5,
        ))
        return {"ok": True, "game": game.matchup}

    @app.post("/api/alert/dismiss")
    def dismiss_alert():
        queue.clear()
        return {"ok": True}

    # -- demo ---------------------------------------------------------------

    @app.post("/api/demo/start")
    def demo_start():
        """Take the panel through every feature, for filming or a first look.

        Everything it changes is restored when it finishes or is stopped, and
        nothing it does is written to config.json.
        """
        from .. import demo
        if not demo.start(state, queue):
            raise HTTPException(409, "the demo is already running")
        return {"ok": True, "seconds": round(demo.seconds())}

    @app.post("/api/demo/stop")
    def demo_stop():
        from .. import demo
        demo.stop()
        return {"ok": True}

    # -- bets ---------------------------------------------------------------

    @app.get("/api/bets/catalog")
    def bets_catalog(sport: str):
        """Stat types available for a sport, for the picker's stat dropdown."""
        return {"stats": stat_catalog(sport)}

    @app.get("/api/bets/roster")
    def bets_roster(sport: str, team: str, full: bool = False):
        """Team roster, so a prop can be entered before the game starts.

        Defaults to the curated common-bets list (NFL only); pass full=true
        for the "someone made an unusual bet" escape hatch to the whole roster.
        """
        if not full:
            curated = curated_roster(sport, team)
            if curated:
                return {"players": curated, "curated": True}
        return {"players": team_roster(sport, team), "curated": False}

    @app.post("/api/bets")
    def add_bet(b: NewBet):
        if b.direction not in (OVER, UNDER):
            raise HTTPException(400, "direction must be 'over' or 'under'")
        if b.sport not in ("mlb", "nfl"):
            raise HTTPException(400, f"unknown sport: {b.sport}")

        catalog = {s["key"]: s for s in stat_catalog(b.sport)}
        if b.stat not in catalog:
            raise HTTPException(400, f"unknown stat for {b.sport}: {b.stat}")
        market = catalog[b.stat]

        direction = b.direction
        if market.get("fixed_goal") is not None:
            # Anytime TD asks exactly one question -- at least one? -- so the
            # goal and direction aren't the form's to set. Anything sent is
            # ignored rather than rejected, so a stale form can't fail.
            goal = float(market["fixed_goal"])
            direction = OVER
        else:
            if b.goal is None:
                raise HTTPException(400, "enter a goal, e.g. 80 for 80+ yards")
            goal = normalize_goal(direction, b.goal)
            if goal < 0:
                raise HTTPException(400, "a goal can't be negative")
            if direction == OVER and goal < 1:
                raise HTTPException(400, "an over needs a goal of at least 1")

        bet = state.bets.add(Bet(
            sport=b.sport,
            game_id=b.game_id,
            player_id=str(b.player_id),
            # 32, not 16: the old cut stored "CHRISTIAN MCCAFF", which every
            # renderer then abbreviated to "C.MCCAFF". Renderers fit names to
            # the space they have; storage shouldn't pre-empt that.
            player_name=b.player_name.upper()[:32],
            team=b.team.upper(),
            stat=b.stat,
            stat_label=market["label"],
            direction=direction,
            goal=goal,
            parlay_id=b.parlay_id,
        ))
        state.persist()
        return {"ok": True, "bet": bet.id}

    @app.delete("/api/bets/{bet_id}")
    def delete_bet(bet_id: str):
        removed = state.bets.remove(bet_id)
        state.persist()
        return {"ok": removed}

    @app.post("/api/bets/clear")
    def clear_bets():
        state.bets.clear()
        for sport in state.active_sports:
            state.set_bet_progress(sport, [])
        state.persist()
        return {"ok": True}

    @app.post("/api/parlays")
    def add_parlay(p: NewParlay):
        if not p.bet_ids:
            raise HTTPException(400, "a parlay needs at least one leg")
        parlay = state.bets.add_parlay(p.name, p.bet_ids)
        state.persist()
        return {"ok": True, "parlay": parlay.id}

    @app.delete("/api/parlays/{parlay_id}")
    def delete_parlay(parlay_id: str, drop_legs: bool = False):
        state.bets.remove_parlay(parlay_id, drop_legs=drop_legs)
        state.persist()
        return {"ok": True}

    # -- fantasy ------------------------------------------------------------

    @app.post("/api/fantasy/teams")
    def add_fantasy_team(t: NewFantasyTeam):
        team = state.fantasy.add_team(t.name)
        state.persist()
        return {"ok": True, "team": team.id}

    @app.delete("/api/fantasy/teams/{team_id}")
    def delete_fantasy_team(team_id: str):
        state.fantasy.remove_team(team_id)
        state.persist()
        return {"ok": True}

    @app.post("/api/fantasy/teams/{team_id}/rename")
    def rename_fantasy_team(team_id: str, t: NewFantasyTeam):
        state.fantasy.rename_team(team_id, t.name)
        state.persist()
        return {"ok": True}

    @app.post("/api/fantasy/players")
    def add_fantasy_player(p: NewFantasyPlayer):
        if p.position not in POSITIONS:
            raise HTTPException(400, f"position must be one of {POSITIONS}")
        # A team defense has no athlete to point at, so it's keyed by team.
        athlete_id = p.athlete_id
        if p.position == "DEF" and not athlete_id.startswith(DEF_PREFIX):
            athlete_id = DEF_PREFIX + (p.team or athlete_id).upper()

        added = state.fantasy.add_player(p.team_id, FantasyPlayer(
            athlete_id=athlete_id, name=p.name[:24], position=p.position,
            team=(p.team or "").upper(),
        ))
        if added is None:
            raise HTTPException(409, "unknown team, or that player is already on this roster")
        state.persist()
        return {"ok": True, "player": added.id}

    @app.delete("/api/fantasy/players/{player_id}")
    def delete_fantasy_player(player_id: str):
        removed = state.fantasy.remove_player(player_id)
        state.persist()
        return {"ok": removed}

    @app.post("/api/fantasy/players/{player_id}/starting")
    def toggle_starting(player_id: str):
        on = state.fantasy.toggle_starting(player_id)
        if on is None:
            raise HTTPException(404, "no such player")
        state.persist()
        return {"ok": True, "starting": on}

    def _apply_prefs(prefs, cats, styles, durations):
        for cat, style in (prefs.styles or {}).items():
            if cat in cats and style in ("full", "banner", "off"):
                styles[cat] = style
        for cat, secs in (prefs.durations or {}).items():
            if cat not in cats:
                continue
            try:
                durations[cat] = max(MIN_DURATION, min(MAX_DURATION, float(secs)))
            except (TypeError, ValueError):
                continue
        state.persist()

    @app.post("/api/fantasy/styles")
    def set_fantasy_styles(p: AlertPrefs):
        _apply_prefs(p, CATEGORIES, state.fantasy_styles, state.fantasy_durations)
        return {"ok": True, "styles": state.fantasy_styles,
                "durations": state.fantasy_durations}

    @app.post("/api/bets/styles")
    def set_bet_styles(p: AlertPrefs):
        _apply_prefs(p, BET_CATEGORIES, state.bet_styles, state.bet_durations)
        return {"ok": True, "styles": state.bet_styles,
                "durations": state.bet_durations}

    def _preview_context(state, sample):
        """The real parlay this preview should page through, or None.

        An earlier version invented a five-leg stand-in when there was no real
        parlay. That was a mistake: a panel showing bets you never placed reads
        as a bug, not a demo, and it hid the actual reason nothing was paging --
        that the ticket didn't exist. If there's no real multi-leg parlay, the
        preview shows the leg alert alone and the API says why.
        """
        for parlay in state.bets.parlays:
            legs = state.bets.legs_of(parlay.id)
            if len(legs) < 2:
                continue
            by_id = {p.bet.id: p for p in state.bet_progress}
            from ..bets import Progress
            out = [by_id.get(b.id) or Progress(b, None, "pending", 0.0, False)
                   for b in legs]
            # Lead with the alert's own leg when it belongs to this ticket, so
            # the highlighted row is the one the alert is about.
            out.sort(key=lambda p: p.bet.id != sample.id)
            return parlay, out
        return None, []

    @app.post("/api/bets/parlay-context")
    def set_parlay_context(p: ParlayContext):
        """Whether a full-screen parlay-leg alert is followed by the whole ticket."""
        if p.enabled is not None:
            state.parlay_context = bool(p.enabled)
        if p.page_seconds is not None:
            # Floored server-side for the same reason the other durations are:
            # a page that flicks past in a second can't be read.
            state.parlay_page_seconds = max(MIN_DURATION,
                                            min(MAX_DURATION, float(p.page_seconds)))
        state.persist()
        return {"ok": True, "parlay_context": state.parlay_context,
                "parlay_page_seconds": state.parlay_page_seconds}

    @app.post("/api/bets/alert-test")
    def test_bet_alert(category: str = "bet_hit"):
        """Preview a bet card without waiting for a real bet to move.

        Built from a REAL bet and its real parlay where there is one, so the
        preview shows the ticket name, "LEG n/N" and -- with the whole-ticket
        option on -- the actual ticket paging after it. Pushed as a moment, the
        same way a real play's cards reach the queue.
        """
        from ..bet_events import BetEvent, PRIORITY as BP, BET_HIT, PARLAY_HIT, add_tickets
        from ..bets import Bet, Parlay, Progress
        from ..moments import Moment
        if category not in BET_CATEGORIES:
            raise HTTPException(400, f"unknown category: {category}")
        style = state.bet_styles.get(category, "full")
        if style == "off":
            raise HTTPException(409, f"{category} alerts are switched off")

        sample = next((b for b in state.bets.bets), None) or Bet(
            sport="nfl", game_id="test", player_id="0", player_name="JERRY JEUDY",
            team="CLE", stat="rec_yds", stat_label="Receiving Yards",
            direction="over", goal=80.0,
        )
        hit = category in (BET_HIT, LEG_HIT, PARLAY_HIT)
        prog = Progress(sample, sample.goal if hit else round(sample.goal * .7),
                        "hit" if hit else "live", 1.0 if hit else .7, False)
        ev = BetEvent(
            kind=category, bet=sample, progress=prog,
            key=f"bet-style-test:{time.time()}",
            priority=BP[category], duration=state.bet_durations.get(category, 5.0),
            style=style, overlay=(style == "banner"),
            before=round(sample.goal * .45), delta=round(sample.goal * .25),
        )

        parlay, legs = _preview_context(state, sample)
        if category != BET_HIT:
            ev.parlay = parlay or Parlay(name="SAMPLE PARLAY")
            ev.legs_total = len(legs) if legs else 3
            ev.legs_hit = sum(1 for p in legs if p.status == "hit") if legs else 2
            ev.leg_no = 1

        cards, note = [ev], ""
        if state.parlay_context and style == "full" and category in (LEG_HIT, PROGRESS):
            if parlay is not None:
                cards = add_tickets(cards, state.bets, {p.bet.id: p for p in legs},
                                    state.parlay_page_seconds, f"test:{time.time()}")
            else:
                note = ("No parlay with more than one leg, so there is no ticket "
                        "to page through — group some bets into a parlay first.")

        queue.push_moment(Moment(game_id="test", key=f"bet-test:{time.time()}",
                                 cards=cards, detected_at=time.time()))
        return {"ok": True, "style": style,
                "seconds": round(sum(c.duration for c in cards), 1),
                "pages": sum(c.context_pages for c in cards), "note": note}

    @app.post("/api/fantasy/test")
    def test_fantasy(category: str = "touchdown"):
        """Fire a sample fantasy alert so a style choice can be previewed."""
        from ..fantasy_events import FantasyEvent, PRIORITY, DURATION
        if category not in CATEGORIES:
            raise HTTPException(400, f"unknown category: {category}")
        style = state.fantasy_styles.get(category, "full")
        if style == "off":
            raise HTTPException(409, f"{category} alerts are switched off")

        tracked = state.fantasy.tracked()
        sample = next(iter(tracked.values()), None)
        samples = {
            "touchdown": "24-yard receiving TD", "big_play": "38-yard pass",
            "touch": "12-yard reception", "kicking": "47-yard field goal",
            "defense": "interception",
        }
        queue.push(FantasyEvent(
            kind=category,
            athlete_id="test",
            player_name=(sample or {}).get("name") or "George Pickens",
            position=(sample or {}).get("position") or "WR",
            teams=(sample or {}).get("teams") or ["Yung Gunz"],
            description=samples[category],
            play_id="test", key=f"fantasy-test:{time.time()}",
            style=style, priority=PRIORITY[category],
            duration=state.fantasy_durations.get(category, DURATION[category]),
            overlay=(style == "banner"),
        ))
        return {"ok": True, "style": style}

    # -- college football ---------------------------------------------------

    @app.post("/api/cfb/filters")
    def set_cfb_filters(f: CFBFilters):
        """Which categories of game to rotate through (top25/top50/conf:<id>)."""
        valid = {"all", "top25", "top50"}
        valid |= {f"conf:{c['id']}" for c in cfb_conferences()}
        state.cfb_filters = [x for x in (f.filters or []) if x in valid]
        state.persist()
        return {"ok": True, "filters": state.cfb_filters}

    @app.get("/api/cfb/teams")
    def cfb_teams():
        """FBS teams grouped by conference, for the favourites picker.

        134-odd teams as one flat list is unusable, so the picker gets them
        pre-grouped the way someone actually thinks about college football.
        """
        return {"conferences": cfb_teams_by_conference()}

    @app.post("/api/mode/{mode}")
    def set_mode(mode: str):
        """Focus a sport's settings, and leave lifestyle mode if it's on.

        Which sports are actually in the rotation is still /api/sports' job --
        this doesn't touch that. But picking a sport up top IS how you get out
        of lifestyle mode, so that much belongs here rather than in a second
        request the client has to remember to send.
        """
        try:
            state.set_mode(mode)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if state.lifestyle_mode:
            state.lifestyle_mode = False
            # Anything queued belongs to the mode being left behind.
            queue.clear()
        state.persist()
        return state.snapshot(
            now_showing=display.get("current_id"), alert_info=display.get("alert"),
            now_screen=display.get("current_screen"),
        )

    @app.post("/api/sports")
    def set_sports(s: ActiveSports):
        """Which sports the panel cycles, in rotation order."""
        picked = state.set_active_sports(s.sports or [])
        state.persist()
        return {"ok": True, "active_sports": picked}

    @app.post("/api/sports/{sport}")
    def toggle_sport(sport: str):
        """Toggle one sport on or off without resending the whole list."""
        if sport not in SPORTS:
            raise HTTPException(400, f"unknown sport: {sport}")
        active = state.active_sports
        if sport in active:
            active = [s for s in active if s != sport]
        else:
            # Appended, so the newest sport rotates last and the block order
            # people already have stays put.
            active = active + [sport]
        picked = state.set_active_sports(active)
        state.persist()
        return {"ok": True, "active_sports": picked}

    @app.post("/api/schedule_mode/{on}")
    def set_schedule_mode(on: str):
        """When nothing is live, cycle upcoming games instead of sitting idle."""
        state.schedule_mode = on not in ("0", "false", "off")
        state.persist()
        return {"ok": True, "schedule_mode": state.schedule_mode}

    @app.post("/api/pin/{game_id}")
    def pin(game_id: str):
        # Same id twice is a toggle -- one control, two intents.
        state.pinned_id = None if state.pinned_id == game_id else game_id
        return {"ok": True, "pinned_id": state.pinned_id}

    @app.post("/api/pin")
    def unpin():
        state.pinned_id = None
        return {"ok": True, "pinned_id": None}

    @app.post("/api/replay/{recording_id}")
    def set_replay(recording_id: str):
        """`live` leaves replay mode; any other id starts that recording."""
        state.replay_id = None if recording_id == "live" else recording_id
        state.pinned_id = None
        # Replay is an NFL-only concept; don't blank the other sports' games.
        state.invalidate_source("nfl")
        return {"ok": True, "replay_id": state.replay_id}

    @app.post("/api/next")
    def next_game():
        """Advance the rotation immediately."""
        display["advance"] = True
        return {"ok": True}

    # -- wi-fi ----------------------------------------------------------
    # Reachable both ways round: over the setup hotspot when there's no
    # network yet, and over the real network afterwards to move it to a
    # different one.

    @app.get("/api/wifi")
    def wifi_status():
        from .. import wifi
        return {**wifi.status(), "known": wifi.known(),
                "hotspot_ssid": wifi.HOTSPOT_SSID}

    @app.get("/api/wifi/scan")
    def wifi_scan():
        from .. import wifi
        if not wifi.available():
            raise HTTPException(501, "Wi-Fi control needs NetworkManager (nmcli)")
        return {"networks": wifi.scan()}

    @app.post("/api/wifi/connect")
    def wifi_connect(c: WifiCredentials):
        from .. import wifi
        if not wifi.available():
            raise HTTPException(501, "Wi-Fi control needs NetworkManager (nmcli)")
        if not c.ssid.strip():
            raise HTTPException(400, "pick a network")
        ssid = c.ssid.strip()
        password = c.password or None

        # Answer FIRST, join afterwards.
        #
        # Joining drops the hotspot, and the browser asking for this is almost
        # always ON that hotspot -- so the moment the radio flips, the phone's
        # connection to this server dies. Doing the work inline meant the
        # response could never be delivered: the request hung until the phone
        # gave up, and from the user's side clicking Join did nothing at all,
        # with no message, no error and no clue whether it had worked.
        #
        # So the handler returns immediately and the join runs on a thread,
        # after a beat long enough for this response to actually reach the
        # phone. The outcome lands in state.network, which the panel shows and
        # the page picks up once it is back on a network that can see it.
        def _join():
            time.sleep(1.0)
            # The outcome is recorded inside wifi.connect() and carried on
            # every status() from then on, so it survives the watcher's
            # refresh and is still there when the phone gets back.
            wifi.connect(ssid, password)
            state.set_network({**wifi.status(), "known": wifi.known()})

        threading.Thread(target=_join, name="wifi-join", daemon=True).start()
        return {"ok": True, "pending": True,
                "detail": f"Joining {ssid} -- the hotspot is going down now."}

    @app.post("/api/wifi/forget/{ssid}")
    def wifi_forget(ssid: str):
        from .. import wifi
        return {"ok": wifi.forget(ssid)}

    @app.post("/api/wifi/hotspot/{on}")
    def wifi_hotspot(on: str):
        """Raise or drop the setup hotspot by hand, for testing it."""
        from .. import wifi
        if on in ("0", "false", "off"):
            result = wifi.stop_hotspot()
            state.set_network({**wifi.status(), "known": wifi.known()})
            return {"ok": result, **state.network}
        ok, detail = wifi.start_hotspot()
        state.set_network({**wifi.status(), "known": wifi.known()})
        return {"ok": ok, "detail": detail, **state.network}

    # -- lifestyle ------------------------------------------------------

    @app.post("/api/weather/location")
    def set_weather_location(loc: WeatherLocation):
        from ..sources import weather as weather_source
        result = weather_source.geocode(loc.location)
        if result is None:
            raise HTTPException(404, f"couldn't find a place called {loc.location!r}")
        state.weather_location = loc.location.strip()
        state.set_weather_location(result["lat"], result["lon"], result["name"])
        return {"ok": True, "resolved_name": result["name"]}

    @app.post("/api/countdowns")
    def add_countdown(c: NewCountdown):
        import datetime
        try:
            datetime.datetime.fromisoformat(c.target_iso)
        except ValueError:
            raise HTTPException(400, "target_iso must be an ISO datetime")
        cd = state.add_countdown(c.label, c.target_iso)
        return {"ok": True, "countdown": cd["id"]}

    @app.delete("/api/countdowns/{countdown_id}")
    def delete_countdown(countdown_id: str):
        removed = state.remove_countdown(countdown_id)
        return {"ok": removed}

    @app.post("/api/faces")
    def add_face(f: NewFace):
        from ..render import lifestyle_faces
        if f.layout not in lifestyle_faces.LAYOUTS:
            raise HTTPException(400, f"unknown layout: {f.layout}")
        n = lifestyle_faces.slot_count(f.layout)
        valid_cd_ids = {c["id"] for c in state.countdowns}
        slots = []
        for i in range(n):
            slot = f.slots[i] if i < len(f.slots) else None
            if slot is None or slot.kind not in lifestyle_faces.COMPLICATIONS:
                slots.append({})
                continue
            entry = {"kind": slot.kind}
            if slot.kind == "countdown":
                entry["countdown_id"] = slot.countdown_id if slot.countdown_id in valid_cd_ids else None
            slots.append(entry)
        face = state.add_face(f.name, f.layout, slots)
        return {"ok": True, "face": face["id"]}

    @app.delete("/api/faces/{face_id}")
    def delete_face(face_id: str):
        removed = state.remove_face(face_id)
        return {"ok": removed}

    @app.post("/api/faces/{face_id}/active")
    def toggle_face_active(face_id: str, active: bool = True):
        ok = state.set_face_active(face_id, active)
        if not ok:
            raise HTTPException(404, "no such face")
        return {"ok": True, "active": active}

    @app.post("/api/settings")
    def settings(s: Settings):
        refetch = None   # sport whose source needs rebuilding, if any

        if s.rotate_seconds is not None:
            state.rotate_seconds = max(2.0, min(120.0, s.rotate_seconds))
        if s.brightness is not None:
            state.brightness = max(1, min(100, s.brightness))
            display["brightness_dirty"] = True
        if s.include_finished is not None:
            state.include_finished = s.include_finished
        if s.bets_in_rotation is not None:
            state.bets_in_rotation = s.bets_in_rotation
        if s.schedule_with_live is not None:
            state.schedule_with_live = {sp: bool(s.schedule_with_live.get(sp,
                                       state.schedule_with_live.get(sp, False)))
                                       for sp in SPORTS}
        if s.schedule_look is not None:
            from ..render.schedule_bug import LOOKS
            if s.schedule_look not in LOOKS:
                raise HTTPException(400, f"unknown look: {s.schedule_look}")
            state.schedule_look = s.schedule_look
        if s.favorites_only is not None:
            state.favorites_only = {sp: bool(s.favorites_only.get(sp,
                                    state.favorites_only.get(sp, False)))
                                    for sp in SPORTS}
        if s.show_logos is not None:
            state.show_logos = {sp: bool(s.show_logos.get(sp, state.show_logos.get(sp)))
                                for sp in SPORTS}
            from ..render import logos
            logos.set_enabled(state.show_logos)
        if s.alerts_enabled is not None:
            state.alerts_enabled = s.alerts_enabled
            if not s.alerts_enabled:
                queue.clear()
        if s.alert_seconds is not None:
            state.alert_seconds = max(2.0, min(60.0, s.alert_seconds))
        if s.broadcast_delay is not None:
            state.broadcast_delay = max(0.0, min(300.0, s.broadcast_delay))
        if s.replay_speed is not None:
            state.replay_speed = max(0.5, min(60.0, s.replay_speed))
            # Speed is baked into the source at construction, so rebuild it.
            if state.replay_id:
                refetch = "nfl"

        # Switching modes, or muting sports in lifestyle mode, drops anything
        # already queued: an alert from the mode you just left arriving a
        # moment later is exactly what the switch was meant to stop.
        if s.lifestyle_mode is not None and s.lifestyle_mode != state.lifestyle_mode:
            state.lifestyle_mode = s.lifestyle_mode
            queue.clear()
        if s.lifestyle_alerts is not None and s.lifestyle_alerts != state.lifestyle_alerts:
            state.lifestyle_alerts = s.lifestyle_alerts
            if not s.lifestyle_alerts:
                queue.clear()
        if s.clock_enabled is not None:
            state.clock_enabled = s.clock_enabled
        if s.clock_style is not None:
            from ..render import lifestyle_clock
            if s.clock_style not in lifestyle_clock.STYLES:
                raise HTTPException(400, f"unknown clock style: {s.clock_style}")
            state.clock_style = s.clock_style
        if s.clock_seconds is not None:
            state.clock_seconds = s.clock_seconds
        if s.clock_show_date is not None:
            state.clock_show_date = s.clock_show_date
        if s.clock_analog_digital is not None:
            state.clock_analog_digital = s.clock_analog_digital

        # Colours and sizes are merged per screen rather than replaced, so the
        # UI can send just the swatch that changed. Validation lives in
        # lifestyle_theme so this and the config loader can't disagree.
        from ..render import lifestyle_theme as _theme
        if s.lifestyle_colors is not None:
            bad = _theme.apply_colors(state.lifestyle_colors, s.lifestyle_colors,
                                      strict=True)
            if bad:
                raise HTTPException(400, f"bad colour: {bad[0]!r}")
        if s.lifestyle_scale is not None:
            bad = _theme.apply_scale(state.lifestyle_scale, s.lifestyle_scale)
            if bad:
                raise HTTPException(400, f"bad scale: {bad[0]!r}")

        if s.weather_enabled is not None:
            state.weather_enabled = s.weather_enabled
        for field in ("weather_show_time", "weather_show_conditions",
                      "weather_show_hilo", "weather_show_sun"):
            value = getattr(s, field)
            if value is not None:
                setattr(state, field, value)
        if s.weather_units is not None:
            if s.weather_units not in ("f", "c"):
                raise HTTPException(400, "weather_units must be 'f' or 'c'")
            if s.weather_units != state.weather_units:
                state.weather_units = s.weather_units
                # The reading on file is in the OLD unit. Relabelling it would
                # put "94°C" on the panel, so refetch instead of waiting out
                # the quarter-hour timer.
                state.invalidate_weather()
        if s.message_enabled is not None:
            state.message_enabled = s.message_enabled
        if s.message_text is not None:
            state.message_text = s.message_text[:200]

        # Anything that changes what the source asks for needs a fresh fetch.
        # Each source parameter belongs to one sport, so only that sport's
        # games are dropped -- changing the NFL week shouldn't blank baseball.
        refetch_sports = set()
        for field, attr, sport in (
            ("nfl_seasontype", "nfl_seasontype", "nfl"),
            ("nfl_year", "nfl_year", "nfl"),
            ("nfl_week", "nfl_week", "nfl"),
            ("mlb_date", "mlb_date", "mlb"),
        ):
            val = getattr(s, field)
            if val is not None and getattr(state, attr) != val:
                setattr(state, attr, val or None)
                refetch_sports.add(sport)

        if refetch:
            refetch_sports.add(refetch)
        for sport in refetch_sports:
            state.invalidate_source(sport)

        state.persist()
        return state.snapshot(
            now_showing=display.get("current_id"), alert_info=display.get("alert"),
            now_screen=display.get("current_screen"),
        )

    return app
