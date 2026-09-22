"""Helpers for making sense of ESPN's NFL play-by-play TEXT field.

ESPN's play text uses a different (older) set of team abbreviations than its
own API fields for the same game. Confirmed by scanning every recording in
data/nfl/*.json: text says CLV where the API says CLE (Cleveland), HST where
the API says HOU (Houston), and WAS where the API says WSH (Washington).
TEXT_ALIASES normalizes those back to the canonical abbrevs used everywhere
else in this codebase (the keys in app/sources/teams.py).

This module also pulls a few other things out of raw play text/type that are
awkward to redo inline at every call site: detecting a charged team timeout
(as opposed to an "Official Timeout", which isn't chargeable to either team),
condensing a scoring play's text for a small display, and knowing which play
types stop the game clock.
"""

import re

# Derived empirically from data/nfl/*.json -- every non-canonical, all-caps
# token that appeared in play text next to "by/from/to/on X" and matched one
# of that game's two teams. LV, GB, DEN, NYJ, PIT, CAR, JAX, etc. already
# match their canonical abbrev in teams.py, so they need no entry here.
TEXT_ALIASES = {
    "CLV": "CLE",  # Cleveland
    "HST": "HOU",  # Houston
    "WAS": "WSH",  # Washington
}


def canonical(abbrev: str) -> str:
    return TEXT_ALIASES.get(abbrev, abbrev)


_TIMEOUT_RE = re.compile(r"^Timeout #\d+ by ([A-Z]{2,3}) at ")


def parse_timeout(play: dict) -> str | None:
    if (play.get("type") or {}).get("text") != "Timeout":
        return None
    match = _TIMEOUT_RE.match(play.get("text") or "")
    if not match:
        return None
    return canonical(match.group(1))


# Types after which the clock stops, per NFL rules plus the vocabulary
# actually seen across data/nfl/*.json (printed by the module's own scan --
# see the task report). Incompletions, timeouts, penalties, scores, kicking
# plays, period boundaries, and the two-minute warning all stop the clock;
# a plain in-bounds rush or reception does not.
CLOCK_STOPPING_TYPES = frozenset({
    "Pass Incompletion",
    "Timeout",
    "Official Timeout",
    "Penalty",
    "Two-minute warning",
    "End Period",
    "End of Half",
    "End of Game",
    "Kickoff",
    "Punt",
    "Field Goal Good",
    "Field Goal Missed",
    "Passing Touchdown",
    "Rushing Touchdown",
    "Punt Return Touchdown",
    "Interception Return Touchdown",
    "Pass Interception Return",
    "Fumble Recovery (Opponent)",
    "Fumble Recovery (Own)",
    "Sack Opp Fumble Recovery",
})

_FORMATION_RE = re.compile(r"^\([^)]*\)\s*")
_WHITESPACE_RE = re.compile(r"\s+")
_END_SENTENCE_RE = re.compile(r"(TOUCHDOWN|FIELD GOAL)\.")
# ", Center-R.Ferguson, Holder-M.Wishnowsky." -- snap/hold credits on kicks.
# These always trail, and the names contain periods, so take everything from the
# first credit to the end rather than trying to match one credit at a time.
_CREDITS_RE = re.compile(r",\s*(Center|Holder|Snap)-.*$", re.I)


def summarize_scoring(text: str, max_len: int = 90) -> str:
    text = _FORMATION_RE.sub("", text or "")
    text = _WHITESPACE_RE.sub(" ", text).strip()

    # Drop trailing clauses (extra point, two-point attempt, etc.) once the
    # sentence that actually reports the score has ended.
    match = _END_SENTENCE_RE.search(text)
    if match:
        text = text[: match.end()]

    # Field goals credit the snapper and holder; nobody reads that off a scoreboard.
    text = _CREDITS_RE.sub("", text).strip().rstrip(",").strip()
    if text and not text.endswith("."):
        text += "."

    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rsplit(" ", 1)[0]
    return truncated


# ---------------------------------------------------------------------------
# describe_score: condense a scoring play into two short static lines for a
# small (128x64) LED display: (scorer, detail). Two text shapes appear across
# data/nfl/*.json for scoringPlay==True entries:
#
#   A) Verbose play-by-play, e.g.
#      "(Shotgun) S.Sanders pass short middle to L.Floriea for 8 yards,
#       TOUCHDOWN. A.Szmyt extra point is GOOD"
#      "R.Davis up the middle for 1 yard, TOUCHDOWN."
#      "T.Bass 52 yard field goal is GOOD, Center-..."
#      Names already appear as "F.Last".
#
#   B) Summary style, e.g.
#      "Salvon Ahmed 49 Yd pass from Case Keenum (Cairo Santos Kick)"
#      "Woody Marks 20 Yd Run (Ka'imi Fairbairn Kick)"
#      Names appear as "First Last" and need condensing to "F.LAST".

_MAX_SCORER = 16
_MAX_DETAIL = 30

# A single already-condensed name token, e.g. "S.Sanders", "De'Von.Achane".
_DOTTED_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'-]*\.[A-Za-z][A-Za-z'-]*$")
# Team-prefixed name credit, e.g. "WAS-J.Josephs".
_TEAM_PREFIX_RE = re.compile(r"^[A-Z]{2,3}-")

# Format A: verbose play-by-play.
_A_LEAD_NAME_RE = re.compile(r"^([A-Za-z][A-Za-z'.-]*)\s+(.*)$")
_A_FOR_YARDS_RE = re.compile(
    r"^(?P<mid>.*?)\s+for\s+(?P<yards>-?\d+)\s+yards?,\s*TOUCHDOWN\b"
)
_A_PASS_TO_RE = re.compile(r"\bpass\b.*?\bto\s+([A-Za-z][A-Za-z'.-]*)\s*$")
_A_FIELD_GOAL_RE = re.compile(
    r"^([A-Za-z][A-Za-z'.-]*)\s+(\d+)\s+yard field goal is GOOD"
)
_A_FUMBLE_TD_RE = re.compile(
    r"RECOVERED by\s+(?:[A-Z]{2,3}-)?([A-Za-z][A-Za-z'.-]*).*?TOUCHDOWN", re.I
)

# Format B: summary style ("First Last 49 Yd pass from First2 Last2 (...)").
_B_PASS_RE = re.compile(
    r"^([A-Za-z][A-Za-z'. -]*?)\s+(\d+)\s+Yd\s+pass from\s+([A-Za-z][A-Za-z'. -]*?)\s*\(",
    re.I,
)
_B_TYPED_RE = re.compile(
    r"^([A-Za-z][A-Za-z'. -]*?)\s+(\d+)\s+Yd\s+"
    r"(Run|Interception Return|Punt Return|Fumble Return|Kickoff Return)\b",
    re.I,
)
_B_FIELD_GOAL_RE = re.compile(
    r"^([A-Za-z][A-Za-z'. -]*?)\s+(\d+)\s+Yd\s+Field Goal\b", re.I
)


def _normalize_name(name: str) -> str:
    """Condense a name to "F.LAST" form; passes already-dotted names through."""
    name = (name or "").strip().strip(".")
    if not name:
        return ""
    name = _TEAM_PREFIX_RE.sub("", name)
    if _DOTTED_NAME_RE.match(name):
        return name.upper()
    parts = name.split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].upper()
    first, last = parts[0], parts[-1]
    if not first or not last:
        return ""
    return f"{first[0].upper()}.{last.upper()}"


def _fit_detail(base: str, with_from: str | None) -> str:
    """Prefer the "FROM <passer>" version if it fits; otherwise fall back."""
    if with_from is not None and len(with_from) <= _MAX_DETAIL:
        return with_from
    return base


def describe_score(text: str) -> tuple[str, str]:
    """Returns (scorer, detail) -- two short uppercase lines for a small display."""
    text = _FORMATION_RE.sub("", text or "")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return ("", "")

    scorer = ""
    detail = ""

    # Format A: field goal, e.g. "T.Bass 52 yard field goal is GOOD, ..."
    match = _A_FIELD_GOAL_RE.match(text)
    if match:
        scorer = _normalize_name(match.group(1))
        detail = f"{match.group(2)}YD FIELD GOAL"

    # Format A: "<name> <verb phrase> for <yards> yards, TOUCHDOWN"
    if not scorer:
        lead = _A_LEAD_NAME_RE.match(text)
        if lead:
            name, rest = lead.group(1), lead.group(2)
            fy = _A_FOR_YARDS_RE.match(rest)
            if fy:
                mid, yards = fy.group("mid"), fy.group("yards")
                pass_to = _A_PASS_TO_RE.search(mid) if " pass" in f" {mid}" else None
                if pass_to:
                    receiver = _normalize_name(pass_to.group(1))
                    passer = _normalize_name(name)
                    if receiver:
                        scorer = receiver
                        base = f"{yards}YD PASS"
                        with_from = f"{base} FROM {passer}" if passer else None
                        detail = _fit_detail(base, with_from)
                elif "punt return" in mid.lower():
                    scorer = _normalize_name(name)
                    detail = f"{yards}YD PUNT RETURN"
                elif "kickoff return" in mid.lower() or "kick return" in mid.lower():
                    scorer = _normalize_name(name)
                    detail = f"{yards}YD KICK RETURN"
                elif "interception" in mid.lower():
                    scorer = _normalize_name(name)
                    detail = f"{yards}YD INT RETURN"
                elif "fumble" in mid.lower():
                    scorer = _normalize_name(name)
                    detail = f"{yards}YD FUMBLE RETURN"
                elif "sacked" not in mid.lower():
                    scorer = _normalize_name(name)
                    detail = f"{yards}YD RUN"

    # Format A: fumble scooped and scored, no clean "for X yards" clause.
    if not scorer:
        fumble = _A_FUMBLE_TD_RE.search(text)
        if fumble:
            scorer = _normalize_name(fumble.group(1))
            detail = "FUMBLE RETURN"

    # Format B: "<name> <yards> Yd pass from <passer> (...)"
    if not scorer:
        match = _B_PASS_RE.match(text)
        if match:
            receiver = _normalize_name(match.group(1))
            passer = _normalize_name(match.group(3))
            if receiver:
                scorer = receiver
                base = f"{match.group(2)}YD PASS"
                with_from = f"{base} FROM {passer}" if passer else None
                detail = _fit_detail(base, with_from)

    # Format B: "<name> <yards> Yd Field Goal"
    if not scorer:
        match = _B_FIELD_GOAL_RE.match(text)
        if match:
            scorer = _normalize_name(match.group(1))
            detail = f"{match.group(2)}YD FIELD GOAL"

    # Format B: "<name> <yards> Yd Run/Interception Return/Punt Return/..."
    if not scorer:
        match = _B_TYPED_RE.match(text)
        if match:
            scorer = _normalize_name(match.group(1))
            kind = match.group(3).upper()
            if kind == "INTERCEPTION RETURN":
                kind = "INT RETURN"
            detail = f"{match.group(2)}YD {kind}"

    if not scorer or not detail:
        return ("", "")

    if len(scorer) > _MAX_SCORER or len(detail) > _MAX_DETAIL:
        return ("", "")

    return (scorer, detail)
