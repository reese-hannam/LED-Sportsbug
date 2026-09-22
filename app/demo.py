"""A scripted run through everything the panel does, on one button.

For filming, and for a first-plug-in sanity check: three live games rotating,
then the alerts in order -- a home run, a touchdown, fantasy, and a parlay
coming in leg by leg -- and then everything back exactly as it was.

Nothing here touches the network or config.json. The games are invented, the
alerts are pushed straight into the queue rather than detected, and the real
bets, rosters and settings are restored when the script ends or Stop is
pressed. While it runs the poller stands aside (see main.poller), because a
real poll landing mid-script would overwrite the made-up games with whatever
is actually on today.
"""

import threading
import time

from .bets import Bet, BetBook, evaluate, OVER
from .bet_events import LEG_HIT, PROGRESS, bet_card, parlay_hit_card
from .events import Event, SCORE
from .fantasy_events import BIG_PLAY, TOUCHDOWN, FantasyEvent
from .moments import Moment
from .sources.base import Game, LIVE, Team
from .sources.colors import team_color

# While the demo runs. Short enough that all three games show in the opening
# stretch; the real setting is put back afterwards.
ROTATE_SECONDS = 6.0
# Quiet game time after each alert, so the rotation is visible between them --
# without this the panel is wall-to-wall alerts, which films badly.
GAP_SECONDS = 4.0
# How long the three games run before the first alert, and after the last.
OPENING_SECONDS = 20.0
CLOSING_SECONDS = 12.0

_thread: threading.Thread | None = None
_stop = threading.Event()


def _team(sport, abbrev, score, record):
    return Team(abbrev, score, team_color(sport, abbrev), record=record)


def _games():
    """One live game per sport, each with the detail its renderer draws."""
    return [
        Game(id="demo-nfl", sport="nfl",
             away=_team("nfl", "BAL", 17, "2-1"), home=_team("nfl", "CLE", 24, "3-0"),
             state=LIVE, period="3RD",
             detail={"clock": "7:41", "down_distance": "2nd & 6", "yardline": "BAL 32",
                     "possession": "CLE", "red_zone": False,
                     "timeouts_away": 2, "timeouts_home": 3}),
        Game(id="demo-mlb", sport="mlb",
             away=_team("mlb", "MIN", 3, "80-70"), home=_team("mlb", "CLE", 5, "88-62"),
             state=LIVE, period="B7",
             detail={"balls": 2, "strikes": 1, "outs": 1, "bases": [True, False, True],
                     "is_top": False, "inning_state": "Bottom",
                     "batter_line": "J.RAMIREZ 2-3, HR"}),
        Game(id="demo-cfb", sport="cfb",
             away=_team("cfb", "MICH", 10, "3-1"), home=_team("cfb", "OSU", 28, "4-0"),
             state=LIVE, period="3RD",
             detail={"clock": "11:08", "down_distance": "1st & 10", "yardline": "MICH 24",
                     "possession": "OSU", "red_zone": False,
                     "rank_away": 5, "rank_home": 1}),
    ]


def _score_card(game, points, label, scorer, how, scored="") -> Event:
    """A scoring alert for the HOME side of `game`, with the score bumped.

    The game's own score is raised to match, so when the rotation comes back
    around the bug shows the new number -- the alert and the scoreboard agree,
    which is the whole point of the thing.
    """
    game.home.score += points
    return Event(kind=SCORE, game_id=game.id, sport=game.sport,
                 team=game.home, opponent=game.away, points=points, label=label,
                 away=game.away, home=game.home, period=game.period,
                 key=f"demo:{game.id}:{time.time()}",
                 clock=game.detail.get("clock", ""), scorer=scorer, how=how,
                 scored=scored, priority=2)


def _fantasy_card(kind, name, description, teams, style, duration) -> FantasyEvent:
    return FantasyEvent(kind=kind, athlete_id=f"demo-{name}", player_name=name,
                        position="WR", teams=teams, description=description,
                        play_id="demo", key=f"demo:{name}:{time.time()}",
                        game_id="demo-nfl", style=style, priority=6,
                        duration=duration, overlay=(style == "banner"))


def _book():
    """A three-leg parlay on the demo Browns game."""
    book = BetBook()
    legs = [
        book.add(Bet("nfl", "demo-nfl", "d1", "DAVID NJOKU", "CLE", "rec",
                     "Receptions", OVER, 5.0)),
        book.add(Bet("nfl", "demo-nfl", "d2", "JERRY JEUDY", "CLE", "rec_yds",
                     "Receiving Yards", OVER, 60.0)),
        book.add(Bet("nfl", "demo-nfl", "d3", "NICK CHUBB", "CLE", "anytime_td",
                     "Anytime TD", OVER, 1.0)),
    ]
    parlay = book.add_parlay("SUNDAY PARLAY", [b.id for b in legs])
    return book, parlay, legs


def running() -> bool:
    return _thread is not None and _thread.is_alive()


def start(state, queue) -> bool:
    """Begin the script in its own thread. False if one is already running."""
    global _thread
    if running():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(state, queue), name="demo",
                               daemon=True)
    _thread.start()
    return True


def stop() -> None:
    """Ask the script to finish. It restores everything on its way out."""
    _stop.set()


def _run(state, queue) -> None:
    games = _games()
    nfl, mlb = games[0], games[1]
    book, parlay, legs = _book()
    njoku, jeudy, chubb = legs
    values = {njoku.id: 3.0, jeudy.id: 47.0, chubb.id: 0.0}

    def progress():
        return {b.id: evaluate(b, values[b.id], False) for b in legs}

    def publish():
        """Push the legs' current standing so the parlay SCREEN updates too."""
        state.set_bet_progress("nfl", list(progress().values()))

    def leg(bet, kind, new_value, delta):
        before = values[bet.id]
        values[bet.id] = new_value
        by_bet = progress()
        publish()
        return bet_card(kind, bet, by_bet[bet.id], f"demo:{bet.id}:{time.time()}",
                        book, by_bet, before=before, delta=delta)

    def home_run():
        return [_score_card(mlb, 2, "HOME RUN", "J.RAMIREZ", "2-RUN HOMER",
                            scored="SCORED S.KWAN")]

    def touchdown():
        return [_score_card(nfl, 7, "TOUCHDOWN", "D.NJOKU", "8YD TD CATCH")]

    def fantasy_touchdown():
        return [_fantasy_card(TOUCHDOWN, "Jerry Jeudy", "12-yard receiving TD",
                              ["Yung Gunz"], "full", 6.0)]

    def fantasy_big_play():
        # A bottom bar on purpose: it shows the lighter style, and the game
        # stays visible underneath it.
        return [_fantasy_card(BIG_PLAY, "Cedric Tillman", "42-yard reception",
                              ["Yung Gunz", "Dawg Pound"], "banner", 5.0)]

    def parlay_progress():
        return [leg(njoku, PROGRESS, 4.0, 1.0)]

    def parlay_leg_hit():
        return [leg(jeudy, LEG_HIT, 62.0, 15.0)]

    def parlay_complete():
        # Njoku is still sitting at 4 of 5 from the progress card, so he has to
        # get there before the ticket can claim all three legs -- a PARLAY HIT
        # over a card reading "2 OF 3 IN" is exactly the sort of thing a viewer
        # catches. Three cards back to back is what a real finish looks like:
        # one play completes a leg, the next completes the ticket.
        cards = [leg(njoku, LEG_HIT, 5.0, 1.0), leg(chubb, LEG_HIT, 1.0, 1.0)]
        done = parlay_hit_card(parlay, book, progress(), f"demo:parlay:{time.time()}")
        return cards + [done] if done else cards

    script = [
        ("Three live games", OPENING_SECONDS, None),
        ("Guardians home run", None, home_run),
        ("Browns touchdown", None, touchdown),
        ("Fantasy: touchdown", None, fantasy_touchdown),
        ("Fantasy: big play", None, fantasy_big_play),
        ("Parlay: a leg moves", None, parlay_progress),
        ("Parlay: a leg hits", None, parlay_leg_hit),
        ("Parlay: all three in", None, parlay_complete),
        ("Back to the games", CLOSING_SECONDS, None),
    ]

    saved = _take_over(state, queue, games, book)
    try:
        publish()
        for label, hold, action in script:
            if _stop.is_set():
                break
            state.demo_step = label
            wait = hold or 0.0
            if action is not None:
                cards = action()
                queue.push_moment(Moment(game_id="demo",
                                         key=f"demo:{label}:{time.time()}",
                                         cards=cards, detected_at=time.time()))
                # Long enough for every card to play, plus quiet game time.
                wait = sum(getattr(c, "duration", None) or state.alert_seconds
                           for c in cards) + GAP_SECONDS
            if _stop.wait(wait):
                break
    finally:
        _restore(state, queue, saved)


def seconds() -> float:
    """Roughly how long a full run takes, for the button to say so."""
    # Five one-card alerts at the queue's own durations, plus the parlay's
    # extra card, plus a gap each, plus the opening and closing stretches.
    return OPENING_SECONDS + CLOSING_SECONDS + 8 * GAP_SECONDS + 45


def _take_over(state, queue, games, book) -> dict:
    """Swap the panel onto the demo, remembering everything being replaced."""
    saved = {
        "active_sports": state.active_sports,
        "cfb_filters": list(state.cfb_filters),
        "favorites_only": dict(state.favorites_only),
        "rotate_seconds": state.rotate_seconds,
        "alerts_enabled": state.alerts_enabled,
        "lifestyle_mode": state.lifestyle_mode,
        "bets_in_rotation": state.bets_in_rotation,
        "pinned_id": state.pinned_id,
        "bets": state.bets,
    }
    state.demo_running = True          # stops the poller writing over the games
    state.demo_step = "Starting"
    state.set_demo_games(games)
    # All three sports, or the rotation shows only the focused one's game and
    # the NFL parlay's progress is dropped on the floor (set_bet_progress
    # merges active sports only). Same reason the filters come off: a college
    # category filter or "favourites only" would hide the demo's own games.
    state.set_active_sports(["nfl", "mlb", "cfb"])
    state.cfb_filters = []
    state.favorites_only = {s: False for s in state.favorites_only}
    state.bets = book
    state.rotate_seconds = ROTATE_SECONDS
    state.alerts_enabled = True
    state.lifestyle_mode = False
    state.bets_in_rotation = True
    state.pinned_id = None
    queue.clear()                      # nothing real waiting mid-script
    return saved


def _restore(state, queue, saved: dict) -> None:
    queue.clear()
    state.set_demo_games(None)
    state.bets = saved["bets"]
    # The poller refills this on its next pass; leaving the demo's legs here
    # would keep a parlay nobody placed on the panel.
    state.set_bet_progress("nfl", [])
    state.set_active_sports(saved["active_sports"])
    state.cfb_filters = saved["cfb_filters"]
    state.favorites_only = saved["favorites_only"]
    state.rotate_seconds = saved["rotate_seconds"]
    state.alerts_enabled = saved["alerts_enabled"]
    state.lifestyle_mode = saved["lifestyle_mode"]
    state.bets_in_rotation = saved["bets_in_rotation"]
    state.pinned_id = saved["pinned_id"]
    state.demo_step = ""
    state.demo_running = False
