"""Orchestrator: web server + poller in background threads, render loop up front.

    MATRIX_EMULATE=1 python -m app.main

Prints the machine's LAN address on start, not "localhost", so the control
center can be opened from a phone without looking the IP up.
"""

import argparse
import functools
import socket
import threading
import time
import traceback

from . import config, wifi
from .alerts import AlertQueue
from .bet_events import BetEvent, BetEventDetector
from .bets import evaluate
from .events import EventDetector
from .fantasy_events import FantasyEvent
from .moments import Moment, MomentAssembler, PlayEngine
from .tracker import BetTracker
from .matrix import EMULATE, create_matrix, describe, font
from .state import AppState
from .sources.base import FINAL, PRE
from .sources.mlb import MLBSource, BatterLines, ScoringPlays
from .sources.nfl import NFLSource
from .sources.cfb import CFBSource
from .sources.replay import NFLReplaySource, Recording, load_payload
from .sources.nfl_plays import NFLPlayFeed, load_recorded, with_team_abbrev
from .sources import weather as weather_source
from .render import (mlb_bug, nfl_bug, cfb_bug, logos, schedule_bug, alert as alert_render,
                     bet_alert, bets_page, fantasy_alert,
                     lifestyle_clock, lifestyle_weather, lifestyle_message,
                     lifestyle_countdown, lifestyle_faces, lifestyle_theme,
                     setup_screen)
from .render import widgets as w

FRAME_SECONDS = 1 / 30
WEATHER_POLL_SECONDS = 900
# After a failed fetch, try again soon rather than sitting on a stale reading
# for the full quarter of an hour.
WEATHER_RETRY_SECONDS = 60

# How often a game with a bet or a fantasy starter in it has its play list
# checked. This is what decides how soon after a play its alerts can appear,
# so it is far shorter than the scoreboard poll -- and it only applies to games
# something is actually riding on.
PLAY_POLL_SECONDS = 4.0
REPLAY_PLAY_POLL_SECONDS = 1.0

RENDERERS = {"mlb": mlb_bug, "nfl": nfl_bug, "cfb": cfb_bug}


def lan_ip() -> str:
    """This machine's address on the local network.

    Both servers bind to every interface, so the printed URL should be the one
    that actually works from a phone -- "localhost" is useless on another
    device, and the IP changes whenever the network does.

    Opening a UDP socket to an outside address doesn't send anything; it just
    makes the OS pick the interface it would route through, which is the one
    other devices on the LAN can reach.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "localhost"   # offline: nothing useful to advertise
    finally:
        s.close()


def mdns_host() -> str:
    """The name a phone can use, on whatever network the panel joins.

    An IP address changes with the network; this doesn't, which is the whole
    reason the Pi is named `sportsbug` at install time. Some systems already
    report the .local form from gethostname() -- macOS does -- so appending it
    blindly gave "name.local.local".
    """
    host = socket.gethostname().rstrip(".")
    return host if host.endswith(".local") else f"{host}.local"


def build_source(state: AppState, sport: str):
    """One source per active sport. Each polls on its own interval."""
    if sport == "mlb":
        return MLBSource(date=state.mlb_date)
    if sport == "cfb":
        return CFBSource()
    if state.replay_id:
        return NFLReplaySource(state.replay_id, speed=state.replay_speed)
    return NFLSource(
        seasontype=state.nfl_seasontype,
        year=state.nfl_year,
        week=state.nfl_week,
    )


def _enrich_targets(games, current_id, pinned_id, ahead=2) -> set[str]:
    """The displayed game, anything pinned, and the next few in rotation order."""
    live = [g.id for g in games if g.is_live]
    targets = {i for i in (current_id, pinned_id) if i}
    if current_id in live:
        start = live.index(current_id)
        targets.update(live[start + 1: start + 1 + ahead])
        # Wrap, so the last game in the list still warms the first.
        if start + 1 + ahead > len(live):
            targets.update(live[: (start + 1 + ahead) % len(live)])
    elif live:
        targets.update(live[:ahead + 1])
    return targets


def build_screens(state: AppState) -> list[tuple]:
    """Everything the rotation cycles through, as (key, kind, payload).

    Games first, then bet screens. Each carries a stable key so the render loop
    can hold its place across polls even as games reorder or a bet page comes
    and goes.
    """
    screens = [("game:" + g.id, "game", g) for g in state.rotation()]

    # Bet screens between games are optional: alerts happen either way, and
    # some people want the rotation to stay on the games themselves.
    if not state.bets_in_rotation:
        return screens

    progress = state.bet_progress
    if not progress:
        return screens

    # Only bets whose game is still on today's slate get panel time. A bet on a
    # game that has since dropped off can never settle -- nothing updates its
    # stats, so it stays "live" forever, is never swept by the retention clock,
    # and holds a rotation slot showing a finished game's final box score. That
    # reads as a phantom screen appearing out of nowhere.
    #
    # Deliberately checked against the WHOLE slate rather than the narrowed
    # rotation: a bet you placed on a game you've since filtered out is still a
    # bet you placed, and belongs on the panel. It's staleness that disqualifies
    # a bet here, not the display filters.
    on_slate = {g.id for g in state.games}
    progress = [p for p in progress if p.bet.game_id in on_slate]
    if not progress:
        return screens

    by_id = {p.bet.id: p for p in progress}

    # Parlays get a screen each; their legs are not repeated as straight bets.
    grouped: set[str] = set()
    for parlay in state.bets.parlays:
        legs = [by_id[b.id] for b in state.bets.legs_of(parlay.id) if b.id in by_id]
        if not legs:
            continue
        grouped.update(p.bet.id for p in legs)
        # One screen per page of four legs. A six-leg ticket used to show legs
        # one to four and "+2 more" forever -- the two it promised never came.
        pages = bets_page.page_count(legs)
        for page in range(pages):
            screens.append((f"parlay:{parlay.id}:{page}", "parlay",
                            (parlay, legs, page if pages > 1 else None)))

    straight = [p for p in progress if p.bet.id not in grouped]
    for i, page in enumerate(bets_page.paginate(straight)):
        screens.append((f"props:{i}", "props", page))

    return screens


def _alerts_for_mode(state: AppState, events: list) -> list:
    """Which alerts are allowed to interrupt, given the panel's mode.

    Lifestyle mode is a deliberate step away from sports, so a parlay leg
    filling in or a fantasy running back's 12-yard catch has no business
    taking over a wall clock. A favourite team SCORING is the one thing worth
    breaking that for, and even that is opt-out via `lifestyle_alerts` -- the
    score events reaching here are already favourites-only, filtered where
    they're detected in poller().

    Filtered at release rather than at detection so the mode that counts is
    the one in force when the alert would actually appear: flipping to
    lifestyle mode during a broadcast delay stops an in-flight bet alert from
    landing on the clock a minute later.
    """
    if not state.lifestyle_mode:
        return events
    if not state.lifestyle_alerts:
        return []
    return [e for e in events if not isinstance(e, (BetEvent, FantasyEvent))]


def _play_tracked(state: AppState, game) -> bool:
    """Is this NFL game worth following play by play?

    Only if something rides on it: an NFL bet, or a fantasy starter's team on
    the field. A sixteen-game Sunday is not sixteen extra feeds.
    """
    gid = str(game.id)
    if any(b.sport == "nfl" and str(b.game_id) == gid for b in state.bets.bets):
        return True
    teams = {str(i.get("nfl_team", "")).upper() for i in state.fantasy.tracked().values()}
    teams.discard("")
    return bool(teams) and (game.away.abbrev.upper() in teams
                            or game.home.abbrev.upper() in teams)


def _publish(state: AppState, progress_by_bet: dict, sport: str) -> None:
    """Hand one sport's bet progress to the display, and sweep settled bets.

    The sweep runs on real progress rather than a timer, and survives restarts
    through the persisted resolved_at stamp.
    """
    mine = [progress_by_bet[b.id] for b in state.bets.bets
            if b.sport == sport and b.id in progress_by_bet]
    state.set_bet_progress(sport, mine)
    if mine and state.bets.cleanup({p.bet.id: p for p in mine}):
        state.persist()


def _box_bets(state, tracker, bet_detector, assembler, games, progress_by_bet, now):
    """Baseball props: box-score snapshots, diffed cycle to cycle."""
    prog = tracker.progress(state.bets, games, sport="mlb")
    for p in prog:
        progress_by_bet[p.bet.id] = p
    _publish(state, progress_by_bet, "mlb")
    cards = bet_detector.detect(prog, state.bets, styles=state.bet_styles,
                                durations=state.bet_durations,
                                parlay_context=state.parlay_context,
                                page_seconds=state.parlay_page_seconds,
                                by_bet=progress_by_bet)
    if cards:
        assembler.add_moment(Moment(game_id="", key=f"box:mlb:{now:.3f}",
                                    cards=cards, detected_at=now))


def _nfl_baseline(state, tracker, engine, games, progress_by_bet) -> None:
    """Box-score progress for NFL bets in games NOT followed play by play.

    Pre-game bets read PENDING from here, and a game nobody is following still
    settles. A followed game's bets belong to the PlayEngine and are never
    overwritten here -- the box score reaches those through reconcile().

    Replay has no live box score (a recording only has the FINAL one, which
    would settle every bet the moment replay began), so an unfollowed replay
    bet just reads as not yet recorded.
    """
    by_id = {g.id: g for g in games}
    if state.replay_id:
        prog = [evaluate(b, None, bool(by_id.get(b.game_id)
                                       and by_id[b.game_id].state == FINAL))
                for b in state.bets.bets if b.sport == "nfl"]
    else:
        prog = tracker.progress(state.bets, games, sport="nfl")
    for p in prog:
        if not engine.following(p.bet.game_id):
            progress_by_bet[p.bet.id] = p
    _publish(state, progress_by_bet, "nfl")


def _follow_plays(state, source, games, engine, assembler, tracker, play_feed,
                  next_play_poll, finished, progress_by_bet, now) -> None:
    """The fast clock: new plays in games something rides on, as moments."""
    if source is None or not games:
        return
    replay = isinstance(source, NFLReplaySource)
    stepped = False
    for g in games:
        gid = str(g.id)
        if gid in finished or not _play_tracked(state, g):
            continue
        # Live games, plus one last pass over a game that has just gone final
        # so its closing plays -- and any settlement -- aren't missed.
        if not (g.is_live or replay or (g.state == FINAL and engine.following(gid))):
            continue
        if now < next_play_poll.get(gid, 0.0):
            continue
        next_play_poll[gid] = now + (REPLAY_PLAY_POLL_SECONDS if replay
                                     else PLAY_POLL_SECONDS)
        try:
            plays = _plays_so_far(g, play_feed, source)
            if not plays and not engine.following(gid):
                continue
            official = None if replay else (
                lambda pid, stat, _gid=gid: tracker.box_value("nfl", _gid, pid, stat))
            prog, moments = engine.step(
                g, plays, state.bets, official=official, others=dict(progress_by_bet),
                game_final=(g.state == FINAL), now=now,
                bet_styles=state.bet_styles, bet_durations=state.bet_durations,
                fantasy_book=state.fantasy, fantasy_styles=state.fantasy_styles,
                fantasy_durations=state.fantasy_durations,
                parlay_context=state.parlay_context,
                page_seconds=state.parlay_page_seconds)
        except Exception as e:
            # A bad play fetch must never take the scoreboard down.
            state.set_error(f"plays: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue
        stepped = True
        for p in prog:
            progress_by_bet[p.bet.id] = p
        # Only wait for a score alert to pair with if one can actually come.
        expect = state.alerts_enabled and state.is_favorite_game(g)
        for m in moments:
            m.expect_score = expect
            assembler.add_moment(m)
        if g.state == FINAL:
            finished.add(gid)
    if stepped:
        _publish(state, progress_by_bet, "nfl")


def _norm_play_text(play) -> str:
    """Play text reduced to something comparable across the two feeds."""
    return " ".join(str((play or {}).get("text") or "").split()).upper()


@functools.lru_cache(maxsize=8)
def _core_index_map(event_id: str) -> dict:
    """site-API play index -> core-API play index, for one recording.

    The replay's clock drives the SITE play list (that's what carries the score
    and the down/distance the bug draws). Fantasy reads the CORE list, which is
    the only one with per-play participants. The two are the same game in the
    same order, but they are NOT the same length -- the core feed carries extra
    marker rows like "GAME" -- so using one list's index to slice the other
    silently misaligns them.

    In BUF @ CLE that offset is 2 by the first touchdown: the scoring play is
    site index 23 and core index 25, so the fantasy alert for a touchdown
    arrived one to two polls after the scoring alert for the same play. On the
    panel that reads as a touchdown alert, a return to the game, then a second
    alert for the touchdown you were just told about.

    Aligning on play TEXT rather than position fixes it. Ids would be better
    but the recorder doesn't keep them on the site list, and text is identical
    between the feeds because both quote the league's own play description.
    """
    site = load_payload(event_id)
    recording = Recording(site)
    core = load_recorded(event_id)
    if not core:
        return {}

    out, ci = {}, 0
    for si, sp in enumerate(recording.plays):
        want = _norm_play_text(sp)
        j = ci
        while j < len(core) and _norm_play_text(core[j]) != want:
            j += 1
        if j < len(core):
            out[si] = j
            ci = j + 1
        else:
            # No match (a play the core feed words differently). Hold the
            # previous position rather than skipping ahead -- being briefly
            # behind is recoverable, running ahead would fire a fantasy alert
            # for a play that hasn't been shown yet.
            out[si] = max(ci - 1, 0)
    return out


def _plays_so_far(game, play_feed, source):
    """The plays that have happened, as far as this source is concerned.

    Live: every play so far. The engine decides which are new -- it needs the
    whole list regardless, since stats are recomputed from it on every step.

    Replay: the core-API play list has no timing of its own, so it's windowed
    by the replay's position -- translated through _core_index_map, because the
    two lists do not share indices. Re-passing already-seen plays is harmless;
    the detector dedupes on a stable key.
    """
    if isinstance(source, NFLReplaySource):
        recorded = load_recorded(game.id)
        if not recorded:
            return []
        idx = source.recording.index_at(min(source.elapsed, source.recording.duration))
        core_idx = _core_index_map(str(game.id)).get(idx, idx)
        return [with_team_abbrev(p) for p in recorded[: core_idx + 1]]
    return [with_team_abbrev(p) for p in play_feed.all_plays(game.id)]


def poller(state: AppState, queue: AlertQueue, display: dict, stop: threading.Event):
    """Polls every ACTIVE sport into shared state, and turns plays into moments.

    Two clocks run here. Each sport's scoreboard is polled on that source's own
    interval; that drives the games on screen and the score alerts. Separately,
    any NFL game with a bet or a fantasy starter in it has its PLAY LIST read
    every PLAY_POLL_SECONDS, and each new play becomes a moment -- its score,
    bet and fantasy cards together, in reading order (see moments.py).

    Detection always runs on real-time data. Everything detected goes to the
    MomentAssembler, the one place the broadcast delay is applied: a moment is
    released `broadcast_delay` seconds after it happened, whole. (Bet alerts
    used to be delayed TWICE -- once by reading already-delayed progress, again
    on release -- which is part of why they trailed the play they were about.)
    The game STATE on the panel is delayed separately, by AppState.games.
    """
    generation = -1
    sources: dict[str, object] = {}
    next_poll: dict[str, float] = {}
    detector = EventDetector()
    bet_detector = BetEventDetector()
    batters = BatterLines()
    scoring = ScoringPlays()
    tracker = BetTracker()
    engine = PlayEngine()
    assembler = MomentAssembler()
    play_feed = NFLPlayFeed()
    nfl_games: list = []
    next_play_poll: dict[str, float] = {}
    finished: set[str] = set()
    # Freshest REAL-TIME progress for every bet, across sports. A parlay spans
    # games and even leagues, so every leg has to be visible to whichever game
    # completes the ticket.
    progress_by_bet: dict = {}
    next_weather_poll = 0.0
    weather_generation = -1

    while not stop.is_set():
        # Independent of the per-sport loop below and of lifestyle_mode: kept
        # warm whenever a location is set, so flipping Lifestyle Mode on
        # doesn't leave the weather screen blank waiting on the first fetch.
        #
        # A generation bump (new place, switched to Celsius) refetches
        # immediately -- otherwise the panel would relabel a Fahrenheit
        # number as Celsius and show 94°C until the timer came round.
        if state.weather_lat is not None and (
                weather_generation != state.weather_generation
                or time.time() >= next_weather_poll):
            weather_generation = state.weather_generation
            reading = weather_source.fetch(state.weather_lat, state.weather_lon,
                                           state.weather_units)
            state.set_weather(reading)
            next_weather_poll = time.time() + (
                WEATHER_POLL_SECONDS if reading else WEATHER_RETRY_SECONDS)

        if generation != state.source_generation:
            generation = state.source_generation
            active = state.active_sports
            sources = {sp: build_source(state, sp) for sp in active}
            # Poll every sport immediately on a rebuild.
            next_poll = {sp: 0.0 for sp in active}
            # Scores from the previous selection would diff into nonsense
            # events -- a sport switching off and back on is not a score change.
            detector.reset()
            bet_detector.reset()
            engine.reset()
            play_feed.reset()
            queue.clear()
            assembler.clear()
            nfl_games = []
            next_play_poll.clear()
            finished.clear()
            progress_by_bet.clear()

        now = time.time()
        # A demo run (app/demo.py) owns the panel: it installs its own games
        # and pushes its own alerts, and a poll landing mid-script would
        # overwrite them with whatever is actually on today.
        polling = not state.demo_running
        for sport, source in (list(sources.items()) if polling else []):
            if now < next_poll.get(sport, 0.0):
                continue
            try:
                games = source.fetch()

                # The batter line needs a per-game call, so only enrich what's
                # actually on the panel rather than the whole slate. Look a
                # couple of games AHEAD in the rotation too: the poll is slower
                # than the rotation interval, so without lookahead a game would
                # sit on screen with a blank strip until the next poll caught up.
                if sport == "mlb":
                    wanted = _enrich_targets(games, display.get("current_id"),
                                             state.pinned_id, ahead=2)
                    for g in games:
                        if g.id in wanted and g.is_live:
                            g.detail["batter_line"] = batters.get(
                                g.id, g.detail.get("batter_id"), g.detail.get("batter", "")
                            )

                state.set_games(sport, games, generation)

                if sport == "mlb":
                    _box_bets(state, tracker, bet_detector, assembler, games,
                              progress_by_bet, now)
                elif sport == "nfl":
                    nfl_games = games
                    _nfl_baseline(state, tracker, engine, games, progress_by_bet)
                # College football has no bet or fantasy plumbing.

                events = detector.detect(games)
                if state.alerts_enabled:
                    by_id = {g.id: g for g in games}
                    alerting = [e for e in events if e.game_id in by_id
                                and state.is_favorite_game(by_id[e.game_id])]
                    # MLB carries no play description on the schedule feed, so
                    # look up who did it -- only for events that will actually
                    # be shown, and using the CURRENT score so the match is
                    # exact even though the alert itself won't appear yet.
                    for e in alerting:
                        if e.sport == "mlb" and not e.scorer:
                            e.scorer, e.how, e.scored = scoring.describe(
                                e.game_id, e.away.score, e.home.score
                            )
                    for e in alerting:
                        # A game followed play by play pairs its score with
                        # the scoring play, so the score LEADS that play's bet
                        # and fantasy cards. Anything else goes out on its own.
                        assembler.add_score(e, now, hold=(
                            sport == "nfl" and engine.following(e.game_id)))
            except Exception as e:
                # One sport's API failing must not stop the others polling.
                state.set_error(f"{sport}: {type(e).__name__}: {e}")
                traceback.print_exc()
            next_poll[sport] = time.time() + source.poll_interval

        if polling:
            _follow_plays(state, sources.get("nfl"), nfl_games, engine, assembler,
                          tracker, play_feed, next_play_poll, finished,
                          progress_by_bet, time.time())

        # Release whatever is due, every tick regardless of poll intervals, so
        # a moment lands close to exactly `broadcast_delay` after its play.
        # Still drained while alerts are off -- just not queued -- so nothing
        # floods the panel the moment they're switched back on.
        if len(assembler):
            for m in assembler.release(state.broadcast_delay, time.time()):
                if not state.alerts_enabled:
                    continue
                m.cards = _alerts_for_mode(state, m.cards)
                queue.push_moment(m)

        stop.wait(0.25)


def serve(state: AppState, display: dict, queue: AlertQueue, port: int, stop: threading.Event):
    import uvicorn
    from .web.server import create_app

    cfg = uvicorn.Config(
        create_app(state, display, queue), host="0.0.0.0", port=port, log_level="warning"
    )
    uvicorn.Server(cfg).run()


def draw_idle(canvas, state: AppState):
    if state.lifestyle_mode:
        # Lifestyle Mode is on but nothing under it is switched on yet --
        # distinct from "no games right now" below, which is a sports message.
        w.text(canvas, font("7x13B"), 4, 22, w.YELLOW, "LIFESTYLE")
        w.text(canvas, font("5x7"), 4, 38, w.DIM, "nothing turned on")
        return

    # Name every sport that's on, not just the focused one -- with multiple
    # active, "MLB" alone would look like the others had been turned off.
    # 17 chars is what 7x13B fits in the 124px available; three sports is 11.
    w.text(canvas, font("7x13B"), 4, 22, w.YELLOW,
           " ".join(s.upper() for s in state.active_sports)[:17])
    msg = "NO GAMES" if state.last_poll else "LOADING"
    w.text(canvas, font("5x7"), 4, 38, w.DIM, msg)
    if state.last_error:
        w.text(canvas, font("4x6"), 4, 54, w.RED, state.last_error[:31])


def main():
    p = argparse.ArgumentParser()
    # These default to None, not to a value: a flag that happens to equal the
    # stored setting must still count as "explicitly passed" and win over it.
    p.add_argument("--mode", choices=("mlb", "nfl", "cfb"))
    p.add_argument("--rotate", type=float, help="seconds per game")
    p.add_argument("--brightness", type=int)
    p.add_argument("--port", type=int, default=8080, help="control center port")
    p.add_argument("--date", help="MLB: YYYY-MM-DD (defaults to today)")
    p.add_argument("--preseason", action="store_true", help="NFL: seasontype=1")
    p.add_argument("--year", type=int, help="NFL season year")
    p.add_argument("--week", type=int, help="NFL week")
    p.add_argument("--replay", help="NFL: replay a recording id from data/nfl instead of live")
    p.add_argument("--speed", type=float, help="replay speed multiplier")
    args = p.parse_args()

    state = AppState()
    # Stored settings first, then let any explicitly passed flag win.
    state.apply(config.load())
    # The renderers ask the logo module directly rather than being handed the
    # flag, so the restored setting has to be pushed into it once at boot.
    logos.set_enabled(state.show_logos)
    if args.mode is not None:
        state._mode = args.mode
    if args.rotate is not None:
        state.rotate_seconds = args.rotate
    if args.brightness is not None:
        state.brightness = args.brightness
    if args.speed is not None:
        state.replay_speed = args.speed
    state.mlb_date = args.date
    state.nfl_seasontype = 1 if args.preseason else 2
    state.nfl_year = args.year
    state.nfl_week = args.week
    state.replay_id = args.replay

    # Live render info the web layer reads, and one-shot commands it writes back.
    display = {"current_id": None, "advance": False, "brightness_dirty": False}

    queue = AlertQueue(duration=state.alert_seconds)

    stop = threading.Event()
    threading.Thread(target=poller, args=(state, queue, display, stop), daemon=True).start()
    # Watches the Wi-Fi and raises the setup hotspot when there's nowhere to
    # connect -- see app/wifi.py. Its own thread because nmcli calls block.
    threading.Thread(target=wifi.watch, args=(state, stop), daemon=True).start()
    threading.Thread(target=serve, args=(state, display, queue, args.port, stop), daemon=True).start()

    matrix = create_matrix(brightness=state.brightness)
    canvas = matrix.CreateFrameCanvas()

    # flush=True so the banner still appears promptly when stdout is a pipe or
    # a log file rather than a terminal (Python buffers hard when not a TTY).
    host = lan_ip()
    lines = ["", f"  driving  {describe()}"]
    if EMULATE:
        # Only the emulator serves a panel stream; on hardware the panel is
        # the display and both of these would be dead links.
        lines += [f"  panel   -> http://{host}:8888",
                  f"  TV      -> http://{host}:{args.port}/tv   <- fullscreen, for a laptop or iPad"]
    lines.append(f"  control -> http://{mdns_host()}:{args.port}"
                 "   <- open this on your phone, on any network")
    lines.append(f"              http://{host}:{args.port}   (same thing, by address)")
    if host == "localhost":
        lines.append("  (no network yet -- the control center is still up on this machine)")
    else:
        lines.append(f"  (also at http://localhost:{args.port} on this machine)")
    print("\n".join(lines) + "\n", flush=True)

    index = 0
    last_switch = time.time()
    last_frame = time.time()
    last_rotation_ids: list[str] = []
    started_at = time.time()
    # What to tell someone to type into a phone. The .local name works on any
    # network the panel joins, which an IP address does not -- see README.
    control_host = mdns_host()

    try:
        while True:
            now = time.time()
            frame_delta = now - last_frame
            last_frame = now

            if display.pop("brightness_dirty", False):
                matrix.brightness = state.brightness

            # Resolve the alert first: while one is up the rotation clock is
            # frozen, so the interrupted game keeps the rest of its slot instead
            # of being silently skipped behind the alert. A bet PROGRESS toast
            # is the exception -- it draws on top of the game rather than
            # replacing it, so the rotation underneath keeps running normally.
            queue.duration = state.alert_seconds
            active = queue.current() if state.alerts_enabled else None
            active_overlay = bool(active and getattr(active.event, "overlay", False))
            if active and not active_overlay:
                last_switch += frame_delta

            # A pin holds a game on screen -- meaningless once the panel has
            # switched to lifestyle content, so it's ignored rather than
            # popping a game back up over a clock nobody asked to leave.
            pinned = state.pinned() if not state.lifestyle_mode else None
            if pinned is not None:
                # A pin holds the screen; restart the clock so unpinning gives a
                # full interval rather than an instant flick to the next screen.
                screen = ("game:" + pinned.id, "game", pinned)
                last_switch = now
            else:
                # AppState owns the lifestyle rotation the same way it owns
                # rotation() for games; build_screens adds the bet screens.
                screens = (state.lifestyle_screens() if state.lifestyle_mode
                           else build_screens(state))
                keys = [s[0] for s in screens]
                # Keep showing the same screen across polls even as games reorder
                # or a bet page appears/disappears.
                if keys != last_rotation_ids:
                    current = last_rotation_ids[index % len(last_rotation_ids)] if last_rotation_ids else None
                    index = keys.index(current) if current in keys else 0
                    last_rotation_ids = keys

                screen = None
                if screens:
                    if display.pop("advance", False) or now - last_switch >= state.rotate_seconds:
                        index = (index + 1) % len(screens)
                        last_switch = now
                    screen = screens[index % len(screens)]

            game = screen[2] if screen and screen[1] == "game" else None
            display["current_id"] = game.id if game else None
            display["current_screen"] = screen[0] if screen else None

            if active is None:
                display["alert"] = None
            elif isinstance(active.event, BetEvent):
                ev = active.event
                display["alert"] = {
                    "kind": ev.kind,
                    "label": ev.label,
                    "player": ev.bet.player_name,
                    "stat": ev.bet.stat_label,
                    "display": ev.progress.display,
                    "legs_hit": ev.legs_hit,
                    "legs_total": ev.legs_total,
                    "parlay": ev.parlay.name if ev.parlay else None,
                    "leg_no": ev.leg_no,
                    "overlay": ev.overlay,
                    "remaining": round(max(0.0, active.duration - active.elapsed), 1),
                    "pending": queue.pending_count,
                }
            elif isinstance(active.event, FantasyEvent):
                ev = active.event
                display["alert"] = {
                    "kind": ev.kind,
                    "label": ev.label,
                    "player": ev.player_name,
                    "teams": ev.team_line,
                    "position": ev.position,
                    "description": ev.description,
                    "style": ev.style,
                    "overlay": ev.overlay,
                    "remaining": round(max(0.0, active.duration - active.elapsed), 1),
                    "pending": queue.pending_count,
                }
            else:
                display["alert"] = {
                    "kind": "score",
                    "label": active.event.label,
                    "team": active.event.team.abbrev,
                    "scorer": active.event.scorer,
                    "how": active.event.how,
                    "scored": active.event.scored,
                    "description": active.event.description,
                    "points": active.event.points,
                    "remaining": round(max(0.0, active.duration - active.elapsed), 1),
                    "pending": queue.pending_count,
                }

            # Draw the normal screen first when an overlay strip needs it
            # underneath. A full-screen card skips it -- see just below.
            canvas.Clear()
            # Wi-Fi trouble outranks the rotation and even an alert: with no
            # network there are no scores to show, and the only useful thing
            # the panel can do is say how to fix it.
            if setup_screen.draw(canvas, state.network, now - started_at, control_host):
                canvas = matrix.SwapOnVSync(canvas)
                spare = FRAME_SECONDS - (time.time() - now)
                if spare > 0:
                    time.sleep(spare)
                continue
            if active is not None and not active_overlay:
                # A full-screen card replaces the screen outright, so drawing
                # the screen first only to clear it was pure waste -- on a Pi
                # Zero, several milliseconds a frame for the life of every
                # alert, and alerts now run back to back.
                pass
            elif screen is None:
                draw_idle(canvas, state)
            elif screen[1] == "game":
                game = screen[2]
                # A game that hasn't started gets its own layout. The live bug
                # is built around a score, a clock and a down-and-distance
                # strip it has none of, which is why a pre-game left the bottom
                # fifteen pixels black.
                if game.state == PRE:
                    schedule_bug.draw(canvas, game, state.schedule_look)
                else:
                    RENDERERS[game.sport].draw(canvas, game)
            elif screen[1] == "props":
                bets_page.draw_props(canvas, screen[2])
            elif screen[1] == "parlay":
                bets_page.draw_parlay(canvas, screen[2][0], screen[2][1],
                                      page=screen[2][2])
            elif screen[1] == "clock":
                lifestyle_clock.draw(canvas, state.clock_style, state.clock_seconds,
                                     lifestyle_theme.Theme.of(state, "clock"),
                                     show_date=state.clock_show_date,
                                     analog_digital=state.clock_analog_digital)
            elif screen[1] == "weather":
                lifestyle_weather.draw(canvas, state.weather, state.weather_units,
                                       lifestyle_theme.Theme.of(state, "weather"),
                                       show_time=state.weather_show_time,
                                       show_conditions=state.weather_show_conditions,
                                       show_hilo=state.weather_show_hilo,
                                       show_sun=state.weather_show_sun)
            elif screen[1] == "message":
                lifestyle_message.draw(canvas, state.message_text,
                                       lifestyle_theme.Theme.of(state, "message"))
            elif screen[1] == "countdown":
                lifestyle_countdown.draw(canvas, screen[2],
                                         lifestyle_theme.Theme.of(state, "countdown"))
            elif screen[1] == "face":
                lifestyle_faces.draw(canvas, screen[2], state)

            if active is not None:
                if not active_overlay:
                    canvas.Clear()
                if isinstance(active.event, FantasyEvent):
                    fantasy_alert.draw(canvas, active)
                elif isinstance(active.event, BetEvent):
                    bet_alert.draw(canvas, active)
                else:
                    alert_render.draw(canvas, active)

            canvas = matrix.SwapOnVSync(canvas)

            # Sleep the REMAINDER of the frame, not a flat interval. Sleeping
            # a full 1/30 after the work meant the real rate was
            # 1/(render + 1/30) -- fine on a laptop where render is under a
            # millisecond, but on a Pi doing 10ms of work that's 23fps, and
            # the clock's seconds visibly stutter.
            spare = FRAME_SECONDS - (time.time() - now)
            if spare > 0:
                time.sleep(spare)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stop.set()


if __name__ == "__main__":
    main()
