"""Condense a single ESPN play into a SHORT per-athlete description.

playtext.py already condenses a whole scoring play into a (scorer, detail)
pair for the LED display. This module answers a narrower, per-participant
question: for a given athlete_id on a given play, what one short phrase
describes what *that* athlete did? The same play produces different text
for different participants -- the passer on a touchdown pass gets "TD pass",
the receiver gets "receiving TD" -- because the fantasy alert line

    FANTASY ALERT -- Yung Gunz -- George Pickens -- 12-yard reception

is built per roster player, not per play.

Everything here is derived from the normalized play dict the fetcher
produces (see the module's callers), not from raw ESPN text -- participant
roles and play `type` text are already parsed out by the time this module
sees them. Like playtext.py, parsing is defensive: malformed or
unrecognized input returns "" / False rather than raising, since a caller
scanning hundreds of plays per game should never crash on one weird play.
"""

# Shared vocabulary so callers that categorize plays (e.g. deciding which
# alerts are worth sending) and this module agree on spelling.
CATEGORY_TOUCHDOWN = "touchdown"
CATEGORY_BIG_PLAY = "big_play"
CATEGORY_TOUCH = "touch"
CATEGORY_KICKING = "kicking"
CATEGORY_DEFENSE = "defense"

MAX_LEN = 34

_TOUCHDOWN_TYPES = frozenset({
    "Passing Touchdown",
    "Rushing Touchdown",
    "Punt Return Touchdown",
    "Interception Return Touchdown",
    "Kickoff Return Touchdown",
    "Fumble Return Touchdown",
})


def is_touchdown(play: dict) -> bool:
    """True if the play's type indicates a touchdown of any kind."""
    type_text = _type_text(play)
    if type_text in _TOUCHDOWN_TYPES:
        return True
    return "touchdown" in type_text.lower()


def is_extra_point(play: dict) -> bool:
    """True if this play is a PAT kick attempt (not a two-point try)."""
    text = _type_text(play).lower()
    if "two point" in text or "two-point" in text:
        return False
    return "extra point" in text or "point after" in text


def _yards_int(play: dict) -> int | None:
    yards = play.get("yards")
    if yards is None:
        return None
    try:
        return int(round(float(yards)))
    except (TypeError, ValueError):
        return None


def _yard_phrase(yards: int, suffix: str, zero_text: str | None = None) -> str:
    """"{N}-yard {suffix}", with natural handling of zero/negative yardage."""
    if yards == 0 and zero_text is not None:
        return zero_text
    if yards < 0:
        return f"{abs(yards)}-yard loss"
    return f"{yards}-yard {suffix}"


def _type_text(play: dict) -> str:
    """The play's type as a string.

    Accepts BOTH shapes deliberately: the raw core-API play nests it as
    {"type": {"text": "Rush"}}, while app/sources/nfl_plays.parse_play flattens
    it to {"type": "Rush"}. This module is called with the normalized form in
    production and the raw form in ad-hoc probes, so it has to handle either.
    """
    value = (play or {}).get("type")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text") or ""
    return ""


def describe_play(play: dict, athlete_id: str) -> str:
    """Short phrase (<=34 chars) describing what `athlete_id` did on `play`.

    Returns "" when the play/role combination isn't recognized, or on any
    malformed input, rather than guessing at text.
    """
    if not isinstance(play, dict) or not athlete_id:
        return ""

    participants = play.get("participants") or []
    if not isinstance(participants, list):
        return ""

    roles = []
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        if str(participant.get("athlete_id")) == str(athlete_id):
            role = participant.get("role")
            if role:
                roles.append(role)
    if not roles:
        return ""

    # A single athlete can appear under several roles on one play (e.g. the
    # rusher who also fumbled). Try each role and take the first phrase we
    # can build -- later roles are usually the more notable event.
    for role in roles:
        described = _describe_role(play, role)
        if described:
            return described
    return ""


def _describe_role(play: dict, role: str) -> str:
    play_type = _type_text(play)
    yards = _yards_int(play)
    result = ""

    if is_extra_point(play):
        if role in ("patScorer", "kicker"):
            text = play.get("text") or ""
            result = "missed extra point" if "no good" in text.lower() or "missed" in text.lower() else "extra point"

    elif play_type == "Rush":
        if role == "rusher" and yards is not None:
            result = _yard_phrase(yards, "run", zero_text="no gain")
        elif role == "tackler":
            result = "tackle"

    elif play_type == "Rushing Touchdown":
        if role == "rusher" and yards is not None:
            result = _yard_phrase(yards, "rushing TD")

    elif play_type == "Pass Reception":
        if role == "receiver":
            result = _yard_phrase(yards, "reception", zero_text="reception") if yards is not None else "reception"
        elif role == "passer":
            result = _yard_phrase(yards, "pass") if yards is not None else "completed pass"
        elif role == "tackler":
            result = "tackle"

    elif play_type == "Passing Touchdown":
        if role == "receiver":
            result = _yard_phrase(yards, "receiving TD") if yards is not None else "receiving TD"
        elif role == "passer":
            result = _yard_phrase(yards, "TD pass") if yards is not None else "TD pass"

    elif play_type == "Pass Incompletion":
        if role in ("passer", "receiver"):
            result = "incomplete pass"
        elif role == "passDefender":
            result = "pass defended"

    elif play_type == "Sack":
        if role == "sackedBy":
            result = "sack"
        elif role == "passer" and yards is not None:
            result = f"sacked for {abs(yards)}-yard loss" if yards <= 0 else "sacked"
        elif role == "assistedBy":
            result = "assisted sack"

    elif play_type in ("Pass Interception Return", "Interception Return Touchdown"):
        is_pick_six = play_type == "Interception Return Touchdown"
        if role == "returner":
            result = "pick six" if is_pick_six else (
                _yard_phrase(yards, "INT return") if yards is not None else "interception return"
            )
        elif role == "passDefender":
            result = "interception"
        elif role == "passer":
            result = "interception thrown"

    elif play_type == "Field Goal Good":
        if role == "kicker" and yards is not None:
            result = f"{yards}-yard field goal"

    elif play_type == "Field Goal Missed":
        if role == "kicker" and yards is not None:
            result = f"missed {yards}-yard FG"

    elif play_type == "Punt":
        if role == "punter" and yards is not None:
            result = _yard_phrase(yards, "punt") if yards else "punt"
        elif role == "returner":
            result = _yard_phrase(yards, "punt return") if yards else "punt return"

    elif play_type == "Punt Return Touchdown":
        if role == "returner":
            result = _yard_phrase(yards, "punt return TD") if yards is not None else "punt return TD"

    elif play_type == "Kickoff":
        if role == "kicker":
            result = "kickoff"
        elif role == "returner":
            result = _yard_phrase(yards, "kickoff return") if yards else "kickoff return"

    elif play_type in ("Fumble Recovery (Own)", "Fumble Recovery (Opponent)"):
        if role == "recoverer":
            result = "fumble recovery"
        elif role == "fumbler":
            result = "fumble"
        elif role == "forcedBy":
            result = "forced fumble"

    elif play_type == "Safety":
        if role in ("sackedBy", "forcedBy", "tackler"):
            result = "safety"

    elif play_type == "Penalty":
        if role == "penalized":
            result = "penalty"

    # Roles that carry the same meaning regardless of the specific play
    # type above (e.g. a fumble noted as a side-effect of a run or pass).
    if not result:
        if role == "fumbler":
            result = "fumble"
        elif role == "forcedBy":
            result = "forced fumble"
        elif role == "recoverer":
            result = "fumble recovery"

    if not result:
        return ""

    return result if len(result) <= MAX_LEN else ""
