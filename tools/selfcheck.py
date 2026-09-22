"""Everything that should be true about a healthy build, checked in one go.

    .venv/bin/python tools/selfcheck.py
    .venv/bin/python tools/selfcheck.py --quick     # skip the render sweep

Run it after changing anything, and once on the Pi after deploying -- it is
the same checks in both places, which is the point. Exits non-zero on failure
so it can gate a deploy.

What it covers, and why each one exists:

  SETTINGS    Three lists name every setting -- config.DEFAULTS, persist() and
              apply(). They're maintained by hand, and a setting missing from
              persist() doesn't error, it just silently forgets itself on the
              next restart. This turns that into a loud failure.
  CONFIG      A full round-trip, plus hostile values, because config.json is a
              file a person can edit.
  RENDER      Every lifestyle screen in every combination, drawn against a
              canvas that refuses to be written outside 128x64. Catches the
              layout regressions that are invisible until something is clipped
              on real hardware.
  BUDGET      Frame timings. A Pi Zero is roughly 10-20x slower than a laptop
              at Python, so anything close to the 33ms budget here is over it
              there.
  ASSETS      The fonts and logos the panel expects to find on disk.
"""

import argparse
import itertools
import os
import re
import sys
import time
import datetime
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Deliberately NOT forcing MATRIX_EMULATE: on the Pi this should exercise the
# real bindings, which is half the point of being able to run it there.

from app import config                                              # noqa: E402
from app.matrix import WIDTH, HEIGHT, FONT_DIR, EMULATE, describe   # noqa: E402
from app.state import AppState                                      # noqa: E402
from app.render import (lifestyle_clock as clock, lifestyle_weather as wx,   # noqa: E402
                        lifestyle_message as msg, lifestyle_countdown as cd,
                        lifestyle_faces as faces, bigtext)
from app.render.lifestyle_theme import (Theme, SCREENS, MIN_SCALE,   # noqa: E402
                                        MAX_SCALE, to_rgb)

FAILURES: list[str] = []
CHECKS = 0


def check(name, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILURES.append(f"{name}: {detail}")
    return ok


def section(title):
    print(f"\n\033[1m{title}\033[0m")


# -- settings ---------------------------------------------------------------

def check_settings():
    section("SETTINGS")
    src = open(_repo("app/state.py")).read()
    persist_block = src.split("def persist(self)")[1].split("def apply(self")[0]
    persisted = set(re.findall(r'"([a-z_]+)":', persist_block))

    state = AppState()
    persisted |= set(state.bets.to_json()) | set(state.fantasy.to_json())
    defaults = set(config.DEFAULTS)

    lost = sorted(defaults - persisted)
    dropped = sorted(persisted - defaults)
    check("settings/persisted", not lost,
          f"in DEFAULTS but never written by persist(), so they reset on restart: {lost}")
    check("settings/known", not dropped,
          f"persist() writes keys config.save() will drop: {dropped}")
    print(f"  {len(defaults)} settings, all persisted and all known to config")


def _repo(rel):
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel)


# -- config -----------------------------------------------------------------

def check_config():
    section("CONFIG")
    # The REAL save/load path, pointed at a scratch file: persist() writes it,
    # config.load() reads it, apply() restores it. Reconstructing the payload
    # by hand instead would test a copy of the code rather than the code, and
    # would miss exactly the drift this is here to catch.
    import tempfile
    scratch = tempfile.mkdtemp(prefix="sportsbug-check-")
    real_path, real_root = config.CONFIG_PATH, config.PROJECT_ROOT
    config.CONFIG_PATH = os.path.join(scratch, "config.json")
    config.PROJECT_ROOT = scratch
    try:
        state = AppState()
        state.lifestyle_colors["clock"] = {"main": "#FF3232", "accent": "#123456"}
        state.lifestyle_scale["weather"] = 65
        state.clock_show_date = True
        state.clock_analog_digital = True
        state.weather_show_sun = False
        state.weather_show_hilo = True
        state.message_text = "ROUND TRIP"
        state.message_enabled = True
        state.lifestyle_mode = True
        state.clock_style = "analog"
        state.add_countdown("Kickoff", "2027-01-01T12:00:00")
        state.add_face("F", "quad", [{"kind": "time"}] * 4)
        state.toggle_favorite("mlb", "CLE")
        state.persist()

        restored = AppState()
        restored.apply(config.load())
        keys = [k for k in config.DEFAULTS
                if hasattr(state, k) and not isinstance(getattr(state, k),
                                                        (type(state.bets), type(state.fantasy)))]
        diff = {k: (getattr(state, k), getattr(restored, k)) for k in keys
                if getattr(state, k) != getattr(restored, k)}
        check("config/round-trip", not diff, f"changed on the way through: {diff}")
        check("config/favourites survive", restored.favorites["mlb"] == ["CLE"],
              restored.favorites)
    finally:
        config.CONFIG_PATH, config.PROJECT_ROOT = real_path, real_root
        for name in os.listdir(scratch):
            os.unlink(os.path.join(scratch, name))
        os.rmdir(scratch)

    hostile = AppState()
    hostile.apply({
        "lifestyle_colors": {"clock": {"main": "nonsense", "accent": "#0F0"},
                             "bogus": {"main": "#fff"}},
        "lifestyle_scale": {"clock": 5000, "weather": "abc"},
        "clock_style": "not-a-style",
        "countdowns": [{"id": "x", "label": "bad", "target_iso": "nope"}, "junk"],
        "faces": [{"layout": "nope"}, {"layout": "quad", "slots": "junk"}],
    })
    check("config/bad colour", hostile.lifestyle_colors["clock"]["main"] == "#FFFFFF",
          hostile.lifestyle_colors["clock"]["main"])
    check("config/short hex", hostile.lifestyle_colors["clock"]["accent"] == "#00FF00",
          hostile.lifestyle_colors["clock"]["accent"])
    check("config/scale clamp", hostile.lifestyle_scale["clock"] == MAX_SCALE,
          hostile.lifestyle_scale["clock"])
    check("config/bad style", hostile.clock_style in clock.STYLES, hostile.clock_style)
    check("config/bad countdown", hostile.countdowns == [], hostile.countdowns)
    check("config/bad face", len(hostile.faces) == 1 and
          len(hostile.faces[0]["slots"]) == 4, hostile.faces)
    print("  round-trips cleanly; hand-edited rubbish falls back instead of crashing")


# -- render -----------------------------------------------------------------

class StrictCanvas:
    """A canvas held to what the REAL bindings accept.

    Two things it is stricter about than the emulator, both of which are
    invisible on a laptop and broken on hardware:

      * anything drawn outside the panel;
      * float coordinates or colours. RGBMatrixEmulator quietly accepts a
        numpy-ish float, hzeller's C binding raises TypeError -- so an
        un-rounded bit of trigonometry runs fine for months in testing and
        then takes the display down the first time it runs on the Pi.
    """

    def __init__(self):
        self.width, self.height = WIDTH, HEIGHT
        self.escaped: list = []
        self.floats: list = []
        self.lit = 0

    def Clear(self):
        self.escaped, self.floats, self.lit = [], [], 0

    def SetPixel(self, x, y, r, g, b):
        for value in (x, y, r, g, b):
            if isinstance(value, float) or not isinstance(value, int):
                self.floats.append((x, y, r, g, b))
                break
        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
            self.escaped.append((int(x), int(y)))
        elif r or g or b:
            self.lit += 1


WEATHER_SAMPLES = [
    {"temp": t, "is_day": d, "condition": c, "icon": i, "high": hi, "low": lo,
     "sunrise": "2026-09-02T06:42", "sunset": "2026-09-02T19:38"}
    for t, hi, lo in ((72, 81, 64), (-14, 3, -22), (104, 108, 88), (0, 5, -3))
    for c, i in (("CLEAR", "clear"), ("PARTLY CLOUDY", "cloud"),
                 ("FREEZING DRIZZLE", "rain"), ("SNOW SHOWERS", "snow"))
    for d in (True, False)
]

MESSAGES = ["", "GO", "GO BUCKEYES",
            "Happy birthday to the best sister in the whole entire world",
            "SUPERCALIFRAGILISTICEXPIALIDOCIOUSANTIDISESTABLISHMENTARIANISM",
            "a b c d e f g h i j k l m n o p q r s t u v w x y z 1 2 3"]

THEMES = [Theme(), Theme(scale=MIN_SCALE), Theme(scale=70),
          Theme(main=to_rgb("#FF3232"), accent=to_rgb("#4090FF")),
          Theme(main=to_rgb("#00DC3C"), accent=to_rgb("#FFC400"), scale=55)]


def _countdowns():
    now = datetime.datetime.now()
    return [{"id": f"c{i}", "label": label,
             "target_iso": (now + datetime.timedelta(seconds=off)).isoformat()}
            for i, (label, off) in enumerate([
                ("NFL KICKOFF", 86400 * 23), ("TOMORROW", 86400), ("SOON", 3600),
                ("SECONDS", 30), ("NOW", 0), ("JUST PASSED", -60),
                ("YESTERDAY", -86400 * 2), ("", 86400 * 400),
                ("A VERY LONG COUNTDOWN LABEL INDEED", 86400 * 3)])]


class FakeState:
    """Just enough of AppState for the face renderer."""

    def __init__(self, weather, seconds=False):
        self.clock_seconds = seconds
        self.weather = weather
        self.weather_units = "f"
        self.message_text = "HAPPY BIRTHDAY MOM"
        self.countdowns = _countdowns()
        self.lifestyle_colors = {s: {"main": "#FFFFFF", "accent": "#808080"} for s in SCREENS}
        self.lifestyle_scale = {s: 100 for s in SCREENS}


def _bet_fixtures():
    """A six-leg parlay with long names, an under, and progress for all of it."""
    from app.bets import Bet, BetBook, evaluate, OVER, UNDER
    book = BetBook()
    specs = [("CHRISTIAN MCCAFFREY", "rush_rec_yds", "Rush + Rec Yards", 100),
             ("BROCK PURDY", "pass_yds", "Passing Yards", 250),
             ("GEORGE KITTLE", "rec", "Receptions", 5),
             ("DEEBO SAMUEL", "anytime_td", "Anytime TD", 1),
             ("JAKE MOODY", "fg_made", "Field Goals Made", 2),
             ("BRANDON AIYUK", "rec_yds", "Receiving Yards", 60)]
    legs = [book.add(Bet("nfl", "g", str(i), n, "SF", s, lab, OVER, float(g)))
            for i, (n, s, lab, g) in enumerate(specs)]
    parlay = book.add_parlay("A PARLAY NAME FAR TOO LONG TO FIT", [b.id for b in legs])
    under = book.add(Bet("nfl", "g", "9", "BROCK PURDY", "SF", "pass_int",
                         "Interceptions Thrown", UNDER, 1.0))
    by = {b.id: evaluate(b, v, False) for b, v in zip(legs, (57, 180, 5, 0, 1, 12))}
    by[under.id] = evaluate(under, 2, False)
    return book, legs, parlay, under, by


def _at(event, elapsed):
    """An alert `elapsed` seconds into its animation."""
    from app.alerts import Alert
    a = Alert(event, event.duration)
    a.start()
    a.started -= elapsed
    return a


def check_render(quick=False):
    section("RENDER")
    if not EMULATE:
        # The sweep draws into a stand-in canvas to inspect every pixel.
        # hzeller's DrawText is C and takes a real Canvas, so it can't be
        # pointed at one -- this sweep belongs on the laptop, where the whole
        # graphics layer is Python. Everything else here runs fine on the Pi.
        print("  skipped: needs the emulator (real DrawText won't take a "
              "stand-in canvas).\n  Run it on the laptop, or with MATRIX_EMULATE=1.")
        return
    drawn = [0]
    blank: list = []

    def draw(name, fn):
        canvas = StrictCanvas()
        try:
            fn(canvas)
        except Exception as e:
            FAILURES.append(f"render/{name}: {type(e).__name__}: {e}\n"
                            f"    {traceback.format_exc().strip().splitlines()[-1]}")
            return
        drawn[0] += 1
        if canvas.escaped:
            FAILURES.append(f"render/{name}: {len(canvas.escaped)} pixels drawn off-panel, "
                            f"e.g. {canvas.escaped[:4]}")
        if canvas.floats:
            FAILURES.append(f"render/{name}: non-int arguments to SetPixel, which the real "
                            f"bindings reject: e.g. {canvas.floats[0]}")
        if canvas.lit == 0:
            blank.append(name)

    times = [(h, m) for h, m in ((0, 0), (1, 11), (9, 59), (12, 0), (23, 59))]
    if quick:
        times = times[:1]

    import unittest.mock as mock
    for style, secs, date, dial, theme in itertools.product(
            clock.STYLES, (False, True), (False, True), (False, True), THEMES):
        draw(f"clock/{style} sec={secs} date={date} dial={dial} scale={theme.scale}",
             lambda c, s=style, x=secs, d=date, g=dial, t=theme:
                 clock.draw(c, s, x, t, show_date=d, analog_digital=g))
    for (hh, mm), style in itertools.product(times, clock.STYLES):
        stamp = datetime.datetime(2026, 9, 2, hh, mm, 37).astimezone()
        with mock.patch.object(clock, "_now", return_value=stamp):
            draw(f"clock/{style} at {hh:02d}:{mm:02d}",
                 lambda c, s=style: clock.draw(c, s, True, Theme(), show_date=True))

    for sample in WEATHER_SAMPLES:
        draw(f"weather/{sample['icon']} {sample['temp']}",
             lambda c, w=sample: wx.draw(c, w, "f"))
    for t_, cond, hilo, sun, theme in itertools.product(
            (False, True), (False, True), (False, True), (False, True), THEMES):
        draw(f"weather/rows t={t_} c={cond} h={hilo} s={sun} scale={theme.scale}",
             lambda c, a=t_, b=cond, d=hilo, e=sun, th=theme:
                 wx.draw(c, WEATHER_SAMPLES[0], "f", th, show_time=a, show_conditions=b,
                         show_hilo=d, show_sun=e))
    draw("weather/no reading", lambda c: wx.draw(c, None, "f"))
    draw("weather/bad sun times",
         lambda c: wx.draw(c, dict(WEATHER_SAMPLES[0], sunrise="x", sunset="y"), "f"))

    for text, theme in itertools.product(MESSAGES, THEMES):
        draw(f"message/{text[:16]!r} scale={theme.scale}",
             lambda c, m=text, t=theme: msg.draw(c, m, t))
    draw("message/None", lambda c: msg.draw(c, None))

    for entry, theme in itertools.product(_countdowns(), THEMES):
        draw(f"countdown/{entry['label'][:16]!r} scale={theme.scale}",
             lambda c, e=entry, t=theme: cd.draw(c, e, t))
    draw("countdown/bad date", lambda c: cd.draw(c, {"label": "X", "target_iso": "nope"}))
    draw("countdown/no target", lambda c: cd.draw(c, {"label": "X"}))

    for layout, comp, weather, secs in itertools.product(
            faces.LAYOUTS, faces.COMPLICATIONS, (WEATHER_SAMPLES[0], None), (False, True)):
        slots = [{"kind": comp, "countdown_id": "c0"}
                 for _ in range(faces.slot_count(layout))]
        draw(f"face/{layout} {comp} wx={weather is not None} sec={secs}",
             lambda c, f={"layout": layout, "slots": slots}, s=FakeState(weather, secs):
                 faces.draw(c, f, s))
    draw("face/dangling countdown",
         lambda c: faces.draw(c, {"layout": "quad", "slots": [
             {"kind": "time"}, {"kind": "temp"},
             {"kind": "countdown", "countdown_id": "gone"}, {}]},
             FakeState(WEATHER_SAMPLES[0])))
    draw("face/unknown layout",
         lambda c: faces.draw(c, {"layout": "nope", "slots": [{"kind": "time"}]},
                              FakeState(None)))
    draw("face/unknown complication",
         lambda c: faces.draw(c, {"layout": "solo", "slots": [{"kind": "zzz"}]},
                              FakeState(None)))

    # The sports screens too -- they're the older code and get the same
    # off-panel and int-argument treatment. Fixtures come from tools/preview.py
    # rather than a second set written here.
    from tools.preview import demo_games
    from app.render import mlb_bug, nfl_bug, cfb_bug, schedule_bug
    from app.sources.base import PRE
    bugs = {"mlb": mlb_bug, "nfl": nfl_bug, "cfb": cfb_bug}
    for game in demo_games():
        if game.state == PRE:
            for look in schedule_bug.LOOKS:
                draw(f"schedule/{look} {game.id}",
                     lambda c, g=game, k=look: schedule_bug.draw(c, g, k))
        else:
            draw(f"bug/{game.sport} {game.id}",
                 lambda c, g=game: bugs[g.sport].draw(c, g))
    # College carries ranks the other two don't, and they shift the layout.
    ranked = demo_games()[4]
    ranked.sport = "cfb"
    ranked.detail = dict(ranked.detail, rank_away=1, rank_home=25)
    draw("bug/cfb ranked", lambda c: cfb_bug.draw(c, ranked))
    for look in schedule_bug.LOOKS:
        draw(f"schedule/{look} ranked",
             lambda c, k=look: schedule_bug.draw(c, ranked, k))

    # The setup screens -- the ones a person sees when something is wrong, so
    # the ones that most need to be legible and in-bounds.
    from app.render import setup_screen
    for net, label in ((({"supported": True, "hotspot": True, "online": False}), "hotspot"),
                       (({"supported": True, "hotspot": False, "online": False}), "connecting")):
        for phase in (0, 5):
            draw(f"setup/{label} phase={phase}",
                 lambda c, n=net, p=phase: setup_screen.draw(c, n, p, "sportsbug.local"))
    # A failed join: the reason rotates in ahead of the instructions, and has
    # to fit whatever SSID and reason it's given.
    for ssid, detail in (("Friend WiFi", "The password for Friend WiFi was rejected."),
                         ("A Friend's Very Long Network Name", "Couldn't see it — in range?"),
                         ("X", "Joined X but it never gave the panel an address.")):
        failed = {"supported": True, "hotspot": True, "online": False,
                  "last_join": {"ssid": ssid, "ok": False, "detail": detail, "at": 0}}
        for phase in (0, 4, 8):
            draw(f"setup/join failed {ssid[:12]!r} phase={phase}",
                 lambda c, n=failed, p=phase: setup_screen.draw(c, n, p, "sportsbug.local"))
    check("setup/online shows nothing",
          setup_screen.draw(StrictCanvas(), {"supported": True, "online": True,
                                             "hotspot": False}, 0, "x") is False,
          "an online panel should fall through to the rotation")

    # Every font must render W and M with a visible vertex, or NO WIFI reads
    # as NO HIFI -- see bigtext.VERTEX_BLIND.
    for name in bigtext.SAFE_LADDER:
        for ch in "WM":
            px = set(bigtext._pixels(name, ch))
            xs = [p[0] for p in px]
            ys = [p[1] for p in px]
            solid = sum(1 for y in range(min(ys), max(ys) + 1)
                        if all((x, y) in px for x in range(min(xs), max(xs) + 1)))
            check(f"font/{name} {ch} vertex", solid <= 1,
                  f"{solid} fully-solid rows -- reads as {'H' if ch == 'W' else 'N'}")

    # Bet and fantasy cards. One card per leg puts far more of these on the
    # panel than the old merged alerts did, so every kind gets the in-bounds
    # treatment -- in both styles, parlay leg and straight bet, and at several
    # points through its animation, since the bar and the band both move.
    from app.bet_events import (PROGRESS, LEG_HIT, BET_HIT, PARLAY_HIT, PARLAY_TICKET,
                                bet_card, add_tickets)
    from app.fantasy_events import FantasyEvent
    from app.render import bet_alert, fantasy_alert
    book, legs, parlay, under, by = _bet_fixtures()
    for kind, style, bet, t in itertools.product(
            (PROGRESS, LEG_HIT, BET_HIT), ("full", "banner"), (legs[0], legs[3], under),
            (0.2, 0.6, 3.0)):
        ev = bet_card(kind, bet, by[bet.id], "k", book, by, styles={kind: style},
                      before=0.4 * bet.goal, delta=0.3 * bet.goal)
        draw(f"bet/{kind} {style} {bet.stat} t={t}",
             lambda c, e=ev, tt=t: bet_alert.draw(c, _at(e, tt)))
    hit = bet_card(PARLAY_HIT, legs[0], by[legs[0].id], "p", book, by)
    hit.legs_hit = hit.legs_total = len(legs)
    for t in (0.3, 1.0, 2.5):
        draw(f"bet/parlay hit t={t}", lambda c, tt=t: bet_alert.draw(c, _at(hit, tt)))
    lead = bet_card(PROGRESS, legs[0], by[legs[0].id], "k", book, by)
    ticket = [e for e in add_tickets([lead], book, by, 4.0, "t") if e.kind == PARLAY_TICKET]
    check("render/a parlay run closes with its ticket", len(ticket) == 1,
          "no ticket card after a full-screen leg card")
    for t in (0.1, 4.5):
        draw(f"bet/ticket t={t}", lambda c, tt=t: bet_alert.draw(c, _at(ticket[0], tt)))
    for style, teams in itertools.product(("full", "banner"),
                                          (["Yung Gunz"], ["Yung Gunz", "The Other League"])):
        fe = FantasyEvent(kind="touch", athlete_id="1", player_name="Christian McCaffrey",
                          position="RB", teams=teams, description="12-yard reception",
                          play_id="p", key="f", style=style, overlay=style == "banner")
        draw(f"fantasy/{style} teams={len(teams)}",
             lambda c, e=fe: fantasy_alert.draw(c, _at(e, 1.0)))

    # Blank is correct for "nothing configured" screens; anything else is a bug.
    expected_blank = ("message/''", "message/None", "countdown/bad date",
                      "countdown/no target", "face/no-slots", "face/unknown complication")
    odd = [b for b in blank
           if not b.startswith(expected_blank) and "wx=False" not in b]
    check("render/nothing blank unexpectedly", not odd, f"{odd[:6]}")
    print(f"  {drawn[0]} screens drawn, none outside {WIDTH}x{HEIGHT}, "
          f"{len(blank)} intentionally blank")


# -- alerts -----------------------------------------------------------------

def check_alerts():
    """The bet/fantasy alert pipeline, end to end, against a recorded game."""
    section("ALERTS")
    import json
    import math
    import types
    from app.alerts import AlertQueue, MAX_MOMENTS
    from app.bets import (Bet, BetBook, evaluate, normalize_goal, legacy_goal,
                          OVER, UNDER, HIT, LIVE, BUSTED)
    from app.bet_events import LEG_HIT, BET_HIT
    from app.events import Event
    from app.moments import ASSEMBLY_WINDOW, Moment, MomentAssembler, PlayEngine
    from app.sources.base import Team

    # Goals are inclusive, and old saved bets must settle exactly as before.
    over = Bet("nfl", "g", "p", "X", "T", "rec", "Receptions", OVER, 4.0)
    check("goal/over hits ON reaching it",
          evaluate(over, 3, False).status == LIVE and evaluate(over, 4, False).status == HIT,
          "OVER 4 must hit on the fourth, not the fifth")
    under = Bet("nfl", "g", "p", "X", "T", "pass_int", "INT", UNDER, 1.0)
    check("goal/under survives on it, busts past it",
          evaluate(under, 1, False).status == LIVE and evaluate(under, 2, False).status == BUSTED,
          "UNDER 1 means one or fewer")
    check("goal/typed book lines round to the goal",
          normalize_goal(OVER, 79.5) == 80 and normalize_goal(UNDER, 1.5) == 1, "")
    check("goal/old saved bets settle the same",
          legacy_goal(OVER, 79.5) == 80 and legacy_goal(OVER, 4) == 5
          and legacy_goal(UNDER, 1.5) == 1, "a config from before goals would change outcome")

    # Play-derived stats against the official line. An OVERcount is the one
    # failure this can't have: it could fire a false LEG HIT.
    from app.sources.nfl_plays import load_recorded
    from app.sources.nfl_playstats import accumulate
    from app.sources.nfl_stats import player_stats, game_players
    gid = "401873294"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    site_path = os.path.join(root, "data", "nfl", f"{gid}.json")
    plays = load_recorded(gid)
    if not plays or not os.path.exists(site_path):
        print("  play-stat checks skipped: recording not present")
    else:
        site = json.load(open(site_path))
        derived = accumulate(plays)
        keys = ["pass_yds", "pass_td", "pass_cmp", "pass_att", "pass_int", "rush_yds",
                "rush_att", "rush_td", "rec", "rec_yds", "rec_td", "rush_rec_yds",
                "anytime_td", "fg_made", "xp_made", "kick_pts"]
        cells = exact = 0
        overcounts = []
        for pl in game_players(site):
            official, mine = player_stats(site, pl["id"]), derived.get(pl["id"], {})
            for k in keys:
                o, m = official.get(k), mine.get(k)
                if o is None and not m:
                    continue
                cells += 1
                if abs((o or 0) - (m or 0)) < 1e-6:
                    exact += 1
                elif (m or 0) > (o or 0):
                    overcounts.append(f"{pl['name']} {k} {m:g}>{o or 0:g}")
        check("plays/never overcount", not overcounts, f"{overcounts[:4]}")
        check("plays/match the official line", exact == cells, f"{exact}/{cells} exact")
        print(f"  play-derived stats: {exact}/{cells} cells exact against the box score")

        # One player in two parlays: a card for EACH, first ticket first.
        top = max(derived.items(), key=lambda kv: kv[1].get("rush_rec_yds", 0))[0]
        total = derived[top]["rush_rec_yds"]
        book = BetBook()
        a = book.add(Bet("nfl", gid, top, "X", "T", "rush_rec_yds", "Rush + Rec Yards",
                         OVER, float(math.ceil(total * .5))))
        b = book.add(Bet("nfl", gid, top, "X", "T", "rush_rec_yds", "Rush + Rec Yards",
                         OVER, float(math.ceil(total) + 15)))
        book.add_parlay("FIRST", [a.id])
        book.add_parlay("SECOND", [b.id])
        engine, game, moments = PlayEngine(), types.SimpleNamespace(id=gid), []
        for i in range(1, len(plays) + 1):
            moments += engine.step(game, plays[:i], book, now=float(i))[1]
        names = [[c.parlay.name for c in m.cards if getattr(c, "parlay", None)]
                 for m in moments]
        both = [n for n in names if "FIRST" in n and "SECOND" in n]
        check("moments/one player in two parlays gets a card in EACH", bool(both),
              "the second ticket never got its own card")
        check("moments/each moment runs one ticket before the next",
              all(n.index("FIRST") < n.index("SECOND") for n in both), f"{both[:2]}")
        hits = [c for m in moments for c in m.cards if getattr(c, "kind", "") in (LEG_HIT, BET_HIT)]
        check("moments/a hit fires once, on the play that crosses the goal",
              len(hits) == 1 and (hits[0].before or 0) < hits[0].bet.goal <= hits[0].progress.value,
              f"{len(hits)} hit cards")
        check("moments/joining mid-game is silent",
              PlayEngine().step(game, plays[:90], book, now=1.0)[1] == [],
              "a restart mid-game replayed the first half")

    # A touchdown's score card leads its own play, the PAT isn't a second
    # alert, a score with no play still goes out, and the delay applies once.
    def score(points, away, home, key):
        return Event(kind="score", game_id="G", sport="nfl", team=Team("SF", home),
                     opponent=Team("LAR", away), points=points, label="TOUCHDOWN",
                     away=Team("LAR", away), home=Team("SF", home), period="2ND", key=key)
    asm = MomentAssembler()
    asm.add_moment(Moment(game_id="G", key="play:1", cards=["bet card"], detected_at=100.0,
                          scoring=True, away_score=0, home_score=7, expect_score=True))
    check("assembly/a scoring play waits for its score", asm.release(0.0, now=101.0) == [], "")
    asm.add_score(score(6, 0, 6, "td"), now=102.0)
    out = asm.release(0.0, now=102.5)
    check("assembly/the score card leads its play's cards",
          len(out) == 1 and isinstance(out[0].cards[0], Event) and out[0].cards[1] == "bet card",
          f"{out and out[0].cards}")
    asm.add_score(score(1, 0, 7, "pat"), now=104.0)
    check("assembly/the PAT after a shown touchdown is absorbed",
          asm.release(0.0, now=130.0) == [], "the extra point fired as a second alert")
    lone = MomentAssembler()
    lone.add_score(score(3, 3, 0, "fg"), now=0.0)
    check("assembly/an unpaired score still goes out",
          len(lone.release(0.0, now=ASSEMBLY_WINDOW + 0.1)) == 1, "")
    once = MomentAssembler()
    once.add_moment(Moment(game_id="G", key="x", cards=["c"], detected_at=0.0))
    check("assembly/broadcast delay applies exactly once",
          once.release(10.0, now=9.9) == [] and len(once.release(10.0, now=10.0)) == 1, "")

    # The queue never splits a moment, and sheds routine progress first.
    class Card:
        def __init__(self, name, kind="bet_progress"):
            self.name, self.kind, self.duration = name, kind, 1.0
    q = AlertQueue(duration=1.0)
    q.push_moment(Moment(game_id="A", key="a", cards=[Card("a1"), Card("a2")], detected_at=0))
    q.push_moment(Moment(game_id="B", key="b", cards=[Card("b1")], detected_at=1))
    shown = []
    for _ in range(3):
        alert = q.current()
        shown.append(alert.event.name)
        alert.started -= 10
    check("queue/a moment plays through before the next", shown == ["a1", "a2", "b1"], f"{shown}")
    q = AlertQueue(duration=1.0)
    q.push_moment(Moment(game_id="A", key="hit", cards=[Card("hit", LEG_HIT)], detected_at=0))
    for i in range(MAX_MOMENTS + 3):
        q.push_moment(Moment(game_id="A", key=f"p{i}", cards=[Card(f"p{i}")], detected_at=i + 1))
    check("queue/backed up, a leg landing survives and progress goes",
          q.current().event.name == "hit", "")


# -- frame budget -----------------------------------------------------------

class NullCanvas:
    width, height = WIDTH, HEIGHT

    def SetPixel(self, x, y, r, g, b):
        pass

    def Clear(self):
        pass


def check_budget():
    section("FRAME BUDGET")
    canvas, theme = NullCanvas(), Theme()
    weather = WEATHER_SAMPLES[0]
    entry = _countdowns()[0]
    cases = [
        ("clock segment", lambda: clock.draw(canvas, "segment", True, theme, show_date=True)),
        ("clock digital", lambda: clock.draw(canvas, "digital", True, theme, show_date=True)),
        ("clock analog", lambda: clock.draw(canvas, "analog", True, theme, analog_digital=True)),
        ("weather", lambda: wx.draw(canvas, weather, "f", theme, show_hilo=True)),
        ("countdown", lambda: cd.draw(canvas, entry, theme)),
        ("message", lambda: msg.draw(canvas, "Happy birthday to the best sister", theme)),
    ]
    from app.bet_events import PROGRESS, bet_card
    from app.render import bet_alert
    book, legs, _parlay, _under, by = _bet_fixtures()
    card = bet_card(PROGRESS, legs[0], by[legs[0].id], "b", book, by, before=45, delta=12)
    cases.append(("bet card", lambda: bet_alert.draw(canvas, _at(card, 0.5))))
    # A Pi Zero 2 is roughly this much slower than a development laptop at
    # pure Python; the budget is judged against the scaled figure, not the
    # local one, because the local one always looks fine.
    pi_factor = 15
    budget_ms = 1000 / 30
    worst = 0.0
    for name, fn in cases:
        fn()
        start = time.perf_counter()
        for _ in range(200):
            fn()
        ms = (time.perf_counter() - start) / 200 * 1000
        worst = max(worst, ms * pi_factor)
        print(f"  {name:16} {ms:6.3f} ms   ~{ms * pi_factor:5.1f} ms on a Pi")
    check("budget/fits 30fps", worst < budget_ms * 0.6,
          f"worst case ~{worst:.1f} ms of a {budget_ms:.1f} ms frame")


# -- assets -----------------------------------------------------------------

def check_assets():
    section("ASSETS")
    missing = [f"{n}.bdf" for n in bigtext.LADDER
               if not os.path.exists(os.path.join(FONT_DIR, f"{n}.bdf"))]
    check("assets/fonts", not missing, f"missing: {missing}")
    for name in bigtext.LADDER:
        glyphs = bigtext._font(name)["glyphs"]
        check(f"assets/{name} digits", all(ord(c) in glyphs for c in "0123456789:APM"),
              "missing characters the clock needs")

    from app.render import logos
    counts = {s: logos.available(s) for s in ("mlb", "nfl", "cfb")}
    check("assets/logos", all(v > 0 for v in counts.values()), f"{counts}")
    print(f"  {len(bigtext.LADDER)} fonts, logos: " +
          ", ".join(f"{k} {v}" for k, v in counts.items()))


# -- demo -------------------------------------------------------------------

def check_demo():
    """The scripted demo (app/demo.py): right order, and nothing left behind."""
    section("DEMO")
    from app import demo
    from app.alerts import AlertQueue
    from app.bets import Bet, OVER
    from app.main import build_screens
    from app.state import AppState

    state, queue = AppState(), AlertQueue()
    writes = []
    state.persist = lambda: writes.append(1)
    state.bets.add(Bet("nfl", "real", "p", "REAL", "PIT", "rec", "Receptions", OVER, 4.0))
    before = {"bets": state.bets, "sports": state.active_sports,
              "rotate": state.rotate_seconds, "life": state.lifestyle_mode,
              "rot": state.bets_in_rotation, "pin": state.pinned_id,
              "alerts": state.alerts_enabled, "cfb": list(state.cfb_filters),
              "favonly": dict(state.favorites_only)}

    # Run the script with every wait skipped, recording what each step pushed
    # and what the panel would have been showing at the time.
    holds = (demo.ROTATE_SECONDS, demo.GAP_SECONDS, demo.OPENING_SECONDS,
             demo.CLOSING_SECONDS)
    wait = demo._stop.wait
    push = queue.push_moment
    kinds, screens = [], []
    demo.GAP_SECONDS = demo.OPENING_SECONDS = demo.CLOSING_SECONDS = 0.0
    demo._stop.wait = lambda t=None: False
    queue.push_moment = lambda m: (kinds.append([type(c).__name__ for c in m.cards]),
                                   screens.append([s[1] for s in build_screens(state)]),
                                   push(m))[2]
    try:
        demo.start(state, queue)
        demo._thread.join(10)
    finally:
        queue.push_moment = push
        demo._stop.wait = wait
        (demo.ROTATE_SECONDS, demo.GAP_SECONDS, demo.OPENING_SECONDS,
         demo.CLOSING_SECONDS) = holds

    flat = [k for cards in kinds for k in cards]
    check("demo/order", flat == ["Event", "Event", "FantasyEvent", "FantasyEvent",
                                "BetEvent", "BetEvent", "BetEvent", "BetEvent", "BetEvent"],
          f"scores, then fantasy, then the parlay -- got {flat}")
    check("demo/parlay finishes together", len(kinds[-1]) == 3,
          "the last two legs and the ticket belong in one moment, back to back")
    check("demo/three games and a parlay on screen",
          screens and screens[0].count("game") == 3 and "parlay" in screens[0],
          f"rotation during the run: {screens[0] if screens else None}")

    check("demo/games handed back", state._demo_games is None and state.games == [], "")
    check("demo/bets handed back", state.bets is before["bets"], "")
    check("demo/settings handed back",
          (state.active_sports == before["sports"]
           and state.rotate_seconds == before["rotate"]
           and state.lifestyle_mode == before["life"]
           and state.bets_in_rotation == before["rot"]
           and state.pinned_id == before["pin"]
           and state.alerts_enabled == before["alerts"]
           and state.cfb_filters == before["cfb"]
           and state.favorites_only == before["favonly"]), "")
    check("demo/nothing written to config", not writes,
          "a two-minute demo must not make its bets and settings permanent")
    check("demo/flags cleared",
          not state.demo_running and not state.demo_step and not demo.running(), "")
    print(f"  {len(flat)} cards over {len(kinds)} moments, "
          f"~{round(demo.seconds())}s end to end")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="skip the slower render and timing sweeps")
    args = parser.parse_args()

    print(f"driving: {describe()}")
    check_settings()
    check_config()
    check_render(quick=args.quick)
    check_alerts()
    check_demo()
    if not args.quick:
        check_budget()
    check_assets()

    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} of {CHECKS} checks FAILED\033[0m")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"\033[32mall {CHECKS} checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
