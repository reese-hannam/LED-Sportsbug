"""Player stats derived play by play, rather than read from the box score.

Why this exists: bet alerts have to land WITH the play. The box score (see
nfl_stats.py) is authoritative but it is a snapshot endpoint -- it trails the
play feed, and it can't say which play moved a number. So a catch showed up on
the scoreboard, the down and distance moved on, and the bet alert for that
catch arrived 10-30 seconds later with nothing tying it to the play.

The core play feed names every participant and their role on every play, so
the stat line can be rebuilt from the plays themselves: a completed pass is +1
reception and +N receiving yards for the receiver, +1 completion and +N
passing yards for the passer. That makes each delta attributable to one play,
which is what lets a play's score, bet and fantasy alerts be sequenced as one
moment instead of three unrelated ones.

This is NOT the source of truth. It is a fast, attributable estimate that the
box score reconciles -- see reconcile(). Penalties, laterals and overturned
plays are exactly where play-level parsing and the official line disagree, and
when they do the box score wins. The derivation only has to be right about the
plays that matter for props, and fast; tools/selfcheck.py measures it against a
recorded game's final box score so drift is caught rather than assumed away.

Everything is recomputed from the FULL play list on each fetch rather than
accumulated incrementally. It costs microseconds, and it means an edited or
deleted play (ESPN does both) corrects itself on the next fetch instead of
leaving a permanent error in a running total.
"""

import re

from .nfl_stats import apply_combos

# Play types that carry no player stats even when participants are listed.
# A penalty names the penalized player; a timeout names nobody useful.
_DEAD = ("penalty", "timeout", "end period", "end of half", "end of game",
         "two-minute", "coin toss")

# "for 19 yards", "for 1 yard", "for -3 yards", "for no gain" -- the yardage
# the ball carrier is actually CREDITED with.
_GAIN_RE = re.compile(r"\bfor (?:(-?\d+) yards?|(no gain))", re.IGNORECASE)

# Offensive fouls committed during the play and enforced from the spot. See
# _credited_yards for why these credit nothing rather than a guess. Defensive
# and post-play fouls (taunting, roughness) are deliberately absent: those
# leave the carrier's full gain standing.
_SPOT_FOULS = ("offensive holding", "illegal block", "chop block", "clipping",
               "tripping", "offensive pass interference", "illegal crackback",
               "low block", "illegal use of hands")


def _credited_yards(play: dict) -> float:
    """What the player gained, which is not always what the play gained.

    `statYardage` is the play's NET result, and on a play with an accepted
    penalty that includes the enforcement. The recorded BUF @ CLE game has the
    canonical case: a 19-yard catch followed by a 15-yard taunting penalty
    comes through as -11. The receiver is credited 19 -- the official line
    agrees -- so on a penalty play the gain is read from the description.

    Only on penalty plays. Everywhere else `statYardage` is the better number:
    it is structured data, and the text wording varies in ways a regex would
    eventually get wrong.
    """
    yards = play.get("yards")
    net = float(yards) if isinstance(yards, (int, float)) else 0.0
    text = play.get("text") or ""
    lower = text.lower()

    # An aborted snap is logged as a carry with NO yardage credited, however
    # far the ball ended up from the line. The box score agrees in both
    # recorded cases (-1 and -3 in the feed, 0 on the official line).
    if "(aborted)" in lower:
        return 0.0

    # A fumble clause has the same problem as a penalty -- but only when the
    # OTHER team recovers. Then the carrier is credited up to the fumble, and
    # `statYardage` reflects the recovery instead: "D.Dallas up the middle for
    # 5 yards. FUMBLES, RECOVERED by CAR" is 0 in the feed and 5 officially.
    # When he recovers his OWN fumble the yards he advances afterwards count
    # too, and the net is right: "F.Gore ... for no gain. FUMBLES, and
    # recovers" is 2 in the feed and 2 officially. The play type says which.
    own_recovery = "recovery (own)" in (play.get("type") or "").lower()
    if "penalty" not in lower and ("fumble" not in lower or own_recovery):
        return net

    # A foul by the OFFENSE DURING the play is enforced from the spot of the
    # foul, and the carrier is credited only up to that spot -- which the
    # description never states. "K.Johnson ... for 9 yards. PENALTY on PIT,
    # Offensive Holding" was 4 yards on the official line, not 9. Guessing the
    # full gain there is the one mistake this module can't afford: it's an
    # OVERcount, and an overcount can fire a false LEG HIT. So credit nothing
    # and let the box score, which does know, fill it in -- an undercount heals
    # on its own a few seconds later.
    if any(foul in lower for foul in _SPOT_FOULS):
        return 0.0

    # Only the part BEFORE the penalty or fumble describes the carry itself;
    # a penalty clause carries its own "15 yards" that must not be read as it.
    before = re.split(r"penalty|fumbles", text, maxsplit=1, flags=re.IGNORECASE)[0]
    m = _GAIN_RE.search(before)
    if not m:
        return net
    return 0.0 if m.group(2) else float(m.group(1))


def _t(play: dict) -> str:
    return (play.get("type") or "").lower()


def _roles(play: dict) -> dict[str, list[str]]:
    """role -> athlete ids, in the feed's own participant order."""
    out: dict[str, list[str]] = {}
    for p in sorted(play.get("participants") or [], key=lambda x: x.get("order", 0)):
        aid = p.get("athlete_id")
        if aid:
            out.setdefault(p.get("role") or "other", []).append(str(aid))
    return out


def _one(roles: dict, role: str) -> str | None:
    ids = roles.get(role) or []
    return ids[0] if ids else None


def _add(out: dict, aid: str | None, stat: str, amount: float = 1.0) -> None:
    if not aid:
        return
    line = out.setdefault(aid, {})
    line[stat] = line.get(stat, 0.0) + amount


def _longest(out: dict, aid: str | None, stat: str, yards: float) -> None:
    if not aid:
        return
    line = out.setdefault(aid, {})
    line[stat] = max(line.get(stat, float("-inf")), yards)


def play_deltas(play: dict) -> dict[str, dict[str, float]]:
    """athlete id -> {stat: amount} contributed by ONE play.

    Only the stats props are written on. Anything this can't phrase reliably is
    left to the box score rather than guessed at.
    """
    ptype = _t(play)
    if not ptype or any(d in ptype for d in _DEAD):
        return {}

    roles = _roles(play)
    yards = _credited_yards(play)
    touchdown = "touchdown" in ptype
    out: dict[str, dict[str, float]] = {}

    passer = _one(roles, "passer")
    receiver = _one(roles, "receiver")
    rusher = _one(roles, "rusher")
    returner = _one(roles, "returner")

    # --- passing / receiving ---------------------------------------------
    completed = ("pass reception" in ptype or "passing touchdown" in ptype
                 or ("pass" in ptype and "complet" in ptype and "incomplet" not in ptype))
    if completed and passer:
        _add(out, passer, "pass_att")
        _add(out, passer, "pass_cmp")
        _add(out, passer, "pass_yds", yards)
        if touchdown:
            _add(out, passer, "pass_td")
        if receiver:
            _add(out, receiver, "rec")
            _add(out, receiver, "rec_yds", yards)
            _add(out, receiver, "rec_tgts")
            _longest(out, receiver, "rec_long", yards)
            if touchdown:
                _add(out, receiver, "rec_td")
        return out

    if "incomplet" in ptype and passer:
        _add(out, passer, "pass_att")
        _add(out, receiver, "rec_tgts")
        return out

    if "interception" in ptype and passer:
        _add(out, passer, "pass_att")
        _add(out, passer, "pass_int")
        # The intended receiver is named in text only, never as a participant,
        # so a target on an interception is left to the box score. The player
        # who picked it off is the returner.
        _add(out, returner, "def_int")
        if touchdown:
            _add(out, returner, "int_td")
        return out

    if "sack" in ptype and passer:
        _add(out, passer, "pass_sacked")
        _add(out, _one(roles, "sackedBy"), "def_sacks")
        return out

    # --- rushing ---------------------------------------------------------
    if rusher and ("rush" in ptype or "fumble" in ptype):
        # "Fumble Recovery (Own)" is still a carry: the rusher is listed and
        # the yardage stands. A fumble LOST is a turnover the box score scores.
        _add(out, rusher, "rush_att")
        _add(out, rusher, "rush_yds", yards)
        _longest(out, rusher, "rush_long", yards)
        if touchdown:
            _add(out, rusher, "rush_td")
        return out

    # --- returns ---------------------------------------------------------
    # Return yardage is not a prop anyone writes, but a return TOUCHDOWN
    # settles an anytime-TD bet, so it has to be counted.
    if returner and touchdown and ("kickoff" in ptype or "kick" in ptype):
        _add(out, returner, "kr_td")
        return out
    if returner and touchdown and "punt" in ptype:
        _add(out, returner, "pr_td")
        return out

    # --- kicking ---------------------------------------------------------
    kicker = _one(roles, "kicker")
    if "field goal" in ptype and "good" in ptype and kicker:
        _add(out, kicker, "fg_made")
        _add(out, kicker, "kick_pts", 3)
        _longest(out, kicker, "fg_long", yards)
        return out

    return out


# Wording the feed uses when a conversion attempt did NOT produce a kicked point.
_PAT_MISSED = ("pat failed", "no good", "blocked", "missed", "failed")


def _extra_point(play: dict, out: dict) -> None:
    """A touchdown play also carries its PAT: a `patScorer` on the same play.

    The feed folds the extra point into the touchdown's own row instead of
    giving it a play of its own -- and in TWO different wordings depending on
    the game: "T.Bass extra point is GOOD" and "Sincere McCormick 7 Yd Run
    (Eddy Pineiro Kick)". Matching only the first missed every extra point in
    half the recorded games. The `patScorer` role is the reliable signal that a
    kick was attempted; the text is only consulted to rule out a miss ("PAT
    failed") and to keep a two-point try out of a kicker's line.
    """
    kicker = _one(_roles(play), "patScorer")
    if not kicker:
        return
    text = (play.get("text") or "").lower()
    if "two-point" in text or "two point" in text:
        return
    if any(word in text for word in _PAT_MISSED):
        return
    _add(out, kicker, "xp_made")
    _add(out, kicker, "kick_pts", 1)


def deltas_with_pat(play: dict) -> dict[str, dict[str, float]]:
    """play_deltas() plus the extra point folded into a touchdown's own row."""
    out = play_deltas(play)
    _extra_point(play, out)
    return out


def add_into(totals: dict, deltas: dict) -> None:
    """Fold one play's deltas into running totals. Longest-plays take the max."""
    for aid, line in deltas.items():
        dest = totals.setdefault(aid, {})
        for stat, amount in line.items():
            if stat.endswith("_long"):
                dest[stat] = max(dest.get(stat, float("-inf")), amount)
            else:
                dest[stat] = dest.get(stat, 0.0) + amount


def accumulate(plays: list[dict]) -> dict[str, dict[str, float]]:
    """This game so far: athlete id -> {stat: total}, combos included."""
    totals: dict[str, dict[str, float]] = {}
    for play in plays:
        add_into(totals, deltas_with_pat(play))
    for line in totals.values():
        apply_combos(line)
    return totals


# Every stat the plays can produce, directly or as a combination. A bet on one
# of these is driven by plays and announced WITH its play; anything else (a
# defender's tackles, a punter's average) only exists in the box score, so its
# changes are announced from there instead -- see moments.PlayEngine.
_BASE_DERIVED = {
    "pass_att", "pass_cmp", "pass_yds", "pass_td", "pass_int", "pass_sacked",
    "rec", "rec_yds", "rec_tgts", "rec_long", "rec_td",
    "rush_att", "rush_yds", "rush_long", "rush_td",
    "kr_td", "pr_td", "def_int", "int_td", "def_sacks",
    "fg_made", "fg_long", "xp_made", "kick_pts",
}


def _derived_stats() -> set:
    from .nfl_stats import STAT_CATALOG
    out = set(_BASE_DERIVED)
    for s in STAT_CATALOG:
        parts = s.get("components") or ()
        # A combo counts as play-driven if the plays can move it at all. The
        # parts they can't see (a defensive TD inside anytime-TD) still arrive,
        # via the box score, through reconcile().
        if parts and any(c in _BASE_DERIVED for c in parts):
            out.add(s["key"])
    return out


DERIVED_STATS = _derived_stats()


def reconcile(derived: float | None, official: float | None) -> float | None:
    """One value from the two sources, biased so a bet can't appear to lag.

    The play-derived number moves first; the box score catches up. So:

      * box score HIGHER  -- the plays missed something (a lateral, a stat
        correction, a play typed in a way this doesn't parse). Trust it.
      * box score LOWER   -- almost always just behind. Keep the play value,
        which is newer.

    An overturned play is the one case "keep the higher number" gets wrong, and
    it resolves itself: ESPN removes or rewrites the play, the next full-list
    recompute drops it, and both sources agree again.
    """
    if derived is None:
        return official
    if official is None:
        return derived
    return max(derived, official)
