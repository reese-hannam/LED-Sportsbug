"""Decide which plays involving your starters are worth interrupting the panel.

Driven by ESPN's core-API play feed, which -- unlike the summary endpoint used
elsewhere -- carries `participants[]` with athlete ids AND the role each player
had in the play (passer, receiver, rusher, kicker, returner, forcedBy...). That
means matching is exact rather than parsing "S.Sanders" out of prose, and the
role is what makes position-specific rules possible at all: the same 24-yard
touchdown pass is a "24-yard TD pass" for the QB who threw it and a "24-yard
receiving TD" for the WR who caught it, and both alert if both are started.

The rules, per position:

  RB / WR / TE   every catch or run from scrimmage, and a gain of 25+ yards is
                 categorised as a BIG PLAY rather than a routine touch so it can
                 be styled louder. A return only counts if it was a touchdown --
                 otherwise a returner would spam the panel on every unremarkable
                 kickoff bring-out.
  QB             deliberately quieter: a pass only alerts at 25+ yards, and
                 otherwise only touchdowns (thrown, run, or caught). A 4-yard
                 checkdown is not news; alerting on every dropback would bury
                 everything else.
  K              field goals (with distance) and extra points.
  DEF            turnovers forced and defensive scores. A team defense has no
                 athlete id to match on, so it's matched by possession instead:
                 if the team WITH the ball isn't yours and the play is a
                 turnover or defensive score, your defense did it.

Every event carries a stable key so a replayed poll can't fire it twice, and a
player started on several fantasy teams produces ONE event naming all of them.
"""

from dataclasses import dataclass, field

from .fantasy import DEF_PREFIX

# Categories -- these are the units the user assigns an alert style to.
TOUCHDOWN = "touchdown"
BIG_PLAY = "big_play"
TOUCH = "touch"
KICKING = "kicking"
DEFENSE = "defense"

CATEGORIES = [TOUCHDOWN, BIG_PLAY, TOUCH, KICKING, DEFENSE]

CATEGORY_LABEL = {
    TOUCHDOWN: "Touchdowns",
    BIG_PLAY: "Big plays",
    TOUCH: "Routine touches",
    KICKING: "Kicking",
    DEFENSE: "Defense",
}

# Lower sorts first in the alert queue. A touchdown outranks a routine catch,
# and both sit below a parlay landing (priority 1) but above bet progress (80).
PRIORITY = {TOUCHDOWN: 6, BIG_PLAY: 15, DEFENSE: 16, KICKING: 35, TOUCH: 70}
# Default seconds on screen. Overridable per category from the UI; the floor is
# enforced server-side because anything under ~3s can't actually be read.
DURATION = {TOUCHDOWN: 7.0, BIG_PLAY: 5.5, DEFENSE: 5.5, KICKING: 4.5, TOUCH: 3.5}
MIN_DURATION = 3.0
MAX_DURATION = 20.0

# The line between "routine" and "big play", in yards. Applies to a QB's pass
# and to a skill player's catch or run alike.
BIG_PLAY_YARDS = 25
QB_BIG_PASS_YARDS = BIG_PLAY_YARDS

# Roles that mean the player physically handled the ball from scrimmage.
TOUCH_ROLES = {"receiver", "rusher"}
# Roles that mean the player's defense made a play.
DEFENSIVE_ROLES = {"forcedBy", "recoverer", "passDefender", "sackedBy", "fumbler"}

STYLE_FULL = "full"
STYLE_BANNER = "banner"
STYLE_OFF = "off"

DEFAULT_STYLES = {
    TOUCHDOWN: STYLE_FULL,
    BIG_PLAY: STYLE_FULL,
    TOUCH: STYLE_BANNER,
    KICKING: STYLE_BANNER,
    DEFENSE: STYLE_FULL,
}


@dataclass
class FantasyEvent:
    kind: str                 # one of CATEGORIES
    athlete_id: str
    player_name: str
    position: str
    teams: list[str]          # fantasy team names -- several if multi-rostered
    description: str          # "12-yard reception"
    play_id: str
    key: str
    # Which game this happened in. Carried so the alert queue can keep one
    # play's alerts together -- see alerts.py.
    game_id: str = ""
    style: str = STYLE_FULL
    priority: int = 50
    duration: float = 5.0
    overlay: bool = False     # banner styles draw over the current screen
    # Other rostered players involved in the SAME play, as (name, description).
    # A QB and his receiver both being started is one play, not two alerts.
    others: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return "FANTASY ALERT"

    @property
    def team_line(self) -> str:
        return " + ".join(self.teams)

    @property
    def players(self) -> list:
        """Everyone in this alert, lead first."""
        return [(self.player_name, self.description)] + list(self.others)


def _is_touchdown(play: dict) -> bool:
    return "touchdown" in (play.get("type") or "").lower()


# Play types where the ball was actually carried or caught. Anything else
# (incompletions, sacks, penalties) can still list a receiver as a participant.
_NON_TOUCH_HINTS = ("incompletion", "incomplete", "sack", "penalty", "timeout",
                    "kickoff", "punt", "field goal", "extra point")


def _is_touch_play(play: dict) -> bool:
    ptype = (play.get("type") or "").lower()
    if any(hint in ptype for hint in _NON_TOUCH_HINTS):
        return False
    return bool(ptype)


def _is_field_goal(play: dict) -> bool:
    return "field goal" in (play.get("type") or "").lower()


def _is_extra_point(play: dict) -> bool:
    ptype = (play.get("type") or "").lower()
    text = (play.get("text") or "").lower()
    if "two" in ptype and "point" in ptype:
        return False
    return "extra point" in ptype or "extra point" in text or ptype == "pat"


# Play types where the DEFENSE itself put points on the board. An ordinary
# offensive touchdown by the other team is emphatically NOT one of these --
# see match_defense().
DEFENSIVE_SCORE_HINTS = (
    "interception return touchdown",
    "fumble return touchdown",
    "fumble recovery touchdown",
    "safety",
    "blocked",
)


def _is_return_role(role: str) -> bool:
    return role in ("returner",)


def categorize(play: dict, role: str, position: str) -> str | None:
    """Which alert category this (play, role, position) belongs to -- or None
    if this player's involvement isn't worth an alert."""
    ptype = (play.get("type") or "").lower()
    yards = play.get("yards")
    touchdown = _is_touchdown(play)

    if position == "DEF":
        # Handled by possession, not by role -- see match_defense().
        return None

    if position == "K":
        if role not in ("kicker", "patScorer"):
            return None
        # A kicker is also the `kicker` on every kickoff, which is not a
        # scoring event and would fire on every change of possession. Only
        # field goals and extra points count.
        if not (_is_field_goal(play) or _is_extra_point(play)):
            return None
        # Missed kicks aren't worth an alert; only made ones.
        if "missed" in ptype or "blocked" in ptype or "no good" in ptype:
            return None
        return KICKING

    if position == "QB":
        # Touchdowns always, however they came.
        if touchdown and role in ("passer", "rusher", "receiver", "scorer"):
            return TOUCHDOWN
        # Otherwise only a genuinely long throw.
        if role == "passer" and yards is not None and yards >= QB_BIG_PASS_YARDS:
            return BIG_PLAY
        return None

    # RB / WR / TE (and anything else that carries the ball).
    if touchdown and role in TOUCH_ROLES | {"returner", "scorer"}:
        return TOUCHDOWN
    if _is_return_role(role):
        # Returns only matter when they score -- caught by the branch above.
        return None
    if role in TOUCH_ROLES:
        # A long gain is a big play for a skill player too, not just for a QB,
        # so it can be styled separately from a routine 4-yard carry.
        if (yards is not None and yards >= BIG_PLAY_YARDS
                and _is_touch_play(play)):
            return BIG_PLAY
        # A receiver is listed on incompletions too (he was the target), but
        # being thrown at is not touching the ball. Same for a sack, where the
        # intended receiver can still appear.
        if not _is_touch_play(play):
            return None
        return TOUCH
    return None


def match_defense(play: dict, def_team: str) -> bool:
    """Did `def_team`'s defense make a play here?

    A team defense has no athlete id, so this works off possession: the team
    with the ball is the offense, so if that isn't us and the play was a
    turnover or a defensive score, it was our defense.
    """
    if not def_team:
        return False
    offense = (play.get("team") or "").upper()
    if not offense or offense == def_team.upper():
        return False

    if play.get("turnover"):
        return True

    # A defensive SCORE is not merely "the other team scored". An ordinary
    # offensive touchdown by the opponent means our defense got beaten, which
    # is the opposite of an alert -- only a pick six, fumble return, safety or
    # blocked kick counts.
    ptype = (play.get("type") or "").lower()
    return any(hint in ptype for hint in DEFENSIVE_SCORE_HINTS)


def _merge_by_play(events: list[FantasyEvent]) -> list[FantasyEvent]:
    """One play is one alert, however many of your players were in it.

    A touchdown pass involves both the QB who threw it and the receiver who
    caught it. If both are started -- a completely ordinary thing to happen --
    the naive result is two alerts back to back describing the same moment. They
    are merged into one, keeping the weightiest category and listing every
    player underneath.

    Teams are unioned, so a play involving players from two different fantasy
    rosters names both.
    """
    grouped: dict = {}
    order: list = []
    for ev in events:
        if ev.play_id not in grouped:
            grouped[ev.play_id] = []
            order.append(ev.play_id)
        grouped[ev.play_id].append(ev)

    merged: list[FantasyEvent] = []
    for play_id in order:
        group = grouped[play_id]
        if len(group) == 1:
            merged.append(group[0])
            continue

        # Weightiest first: a touchdown leads, a routine touch follows.
        group.sort(key=lambda e: e.priority)
        lead = group[0]
        lead.others = [(e.player_name, e.description) for e in group[1:]]

        seen = list(lead.teams)
        for e in group[1:]:
            for name in e.teams:
                if name not in seen:
                    seen.append(name)
        lead.teams = seen
        merged.append(lead)

    return merged


class FantasyEventDetector:
    def __init__(self):
        self._seen: set[str] = set()

    def reset(self) -> None:
        self._seen.clear()

    def detect(self, plays: list[dict], book, styles: dict | None = None,
               durations: dict | None = None) -> list[FantasyEvent]:
        """`plays` must be only NEW plays -- the feed handles seeding so a
        game joined mid-way doesn't replay its whole history as alerts."""
        from .sources.playdesc import describe_play

        styles = {**DEFAULT_STYLES, **(styles or {})}
        self._durations = {**DURATION, **(durations or {})}
        tracked = book.tracked()
        if not tracked:
            return []

        # Team defenses, keyed by the NFL team they represent.
        defenses = {
            str(aid)[len(DEF_PREFIX):].upper(): info
            for aid, info in tracked.items() if str(aid).startswith(DEF_PREFIX)
        }

        events: list[FantasyEvent] = []
        for play in plays:
            events.extend(self._from_play(play, tracked, defenses, styles, describe_play))

        events = _merge_by_play(events)
        events.sort(key=lambda e: e.priority)
        return events

    def detect_play(self, play: dict, book, styles: dict | None = None,
                    durations: dict | None = None) -> list[FantasyEvent]:
        """ONE play's alerts, one card per player, weightiest first.

        The play-driven path (moments.PlayEngine) calls this for each new play
        so a player's fantasy card lands in the same moment as that play's
        score and bet cards. Deliberately NOT merged per play the way detect()
        is: a QB and his receiver both started are two people, and their cards
        run back to back -- each naming its fantasy team(s) -- rather than one
        card with the second player squeezed into a caption.

        A player started on several rosters is still ONE card naming all of
        them; that merge happens in FantasyBook.tracked().
        """
        from .sources.playdesc import describe_play

        styles = {**DEFAULT_STYLES, **(styles or {})}
        self._durations = {**DURATION, **(durations or {})}
        tracked = book.tracked()
        if not tracked:
            return []
        defenses = {
            str(aid)[len(DEF_PREFIX):].upper(): info
            for aid, info in tracked.items() if str(aid).startswith(DEF_PREFIX)
        }
        events = self._from_play(play, tracked, defenses, styles, describe_play)
        events.sort(key=lambda e: e.priority)
        return events

    def _from_play(self, play, tracked, defenses, styles, describe_play) -> list[FantasyEvent]:
        out = []
        play_id = str(play.get("id", ""))

        # --- players ------------------------------------------------------
        # A player can appear twice in one play (e.g. rusher and scorer); keep
        # the highest-value category rather than firing twice.
        best: dict[str, tuple[str, str]] = {}
        for part in play.get("participants", []) or []:
            aid = str(part.get("athlete_id", ""))
            info = tracked.get(aid)
            if not info:
                continue
            role = part.get("role", "")
            kind = categorize(play, role, info.get("position", ""))
            if not kind:
                continue
            prev = best.get(aid)
            if prev is None or PRIORITY[kind] < PRIORITY[prev[0]]:
                best[aid] = (kind, role)

        for aid, (kind, role) in best.items():
            info = tracked[aid]
            style = styles.get(kind, STYLE_FULL)
            if style == STYLE_OFF:
                continue
            desc = describe_play(play, aid) or _fallback_description(kind, play)
            ev = self._make(kind, aid, info, desc, play_id, style, play)
            if ev:
                out.append(ev)

        # --- team defenses -------------------------------------------------
        for team_abbrev, info in defenses.items():
            if not match_defense(play, team_abbrev):
                continue
            style = styles.get(DEFENSE, STYLE_FULL)
            if style == STYLE_OFF:
                continue
            desc = _defense_description(play)
            ev = self._make(DEFENSE, DEF_PREFIX + team_abbrev, info, desc,
                            play_id, style, play)
            if ev:
                out.append(ev)

        return out

    def _make(self, kind, aid, info, desc, play_id, style, play) -> FantasyEvent | None:
        key = f"fantasy:{play_id}:{aid}:{kind}"
        if key in self._seen:
            return None
        self._seen.add(key)
        return FantasyEvent(
            kind=kind,
            athlete_id=str(aid),
            player_name=info.get("name", ""),
            position=info.get("position", ""),
            teams=list(info.get("teams", [])),
            description=desc,
            play_id=play_id,
            key=key,
            style=style,
            priority=PRIORITY.get(kind, 50),
            duration=getattr(self, "_durations", DURATION).get(kind, 5.0),
            overlay=(style == STYLE_BANNER),
            detail={
                "period": play.get("period"),
                "clock": play.get("clock"),
                "text": play.get("text", ""),
            },
        )


def _fallback_description(kind: str, play: dict) -> str:
    """Used when playdesc can't phrase this role/play pair.

    Never leak a raw ESPN play type ("Passing Touchdown") into an alert -- on a
    128px panel that reads like a bug. The commonest case is a kicker listed as
    a participant on the touchdown play his PAT belonged to, which is genuinely
    an extra point even though the play is typed as the touchdown.
    """
    if kind == KICKING:
        return "extra point" if _is_extra_point(play) else "field goal"
    if kind == TOUCHDOWN:
        return "touchdown"
    if kind == BIG_PLAY:
        yards = play.get("yards")
        return f"{int(yards)}-yard pass" if yards else "big play"
    if kind == DEFENSE:
        return _defense_description(play)
    return "big play" if kind == BIG_PLAY else ""


def _defense_description(play: dict) -> str:
    ptype = (play.get("type") or "").lower()
    if "interception" in ptype:
        return "pick six" if "touchdown" in ptype else "interception"
    if "fumble" in ptype:
        return "fumble recovery TD" if "touchdown" in ptype else "fumble recovery"
    if "safety" in ptype:
        return "safety"
    if "touchdown" in ptype:
        return "defensive TD"
    return "turnover"
