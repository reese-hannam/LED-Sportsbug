"""A live countdown to a date and time you set.

Computed fresh every frame straight from the wall clock, the same way an
alert's remaining time is `duration - elapsed` -- no polling involved, so it
never goes stale between screens.
"""

import datetime

from . import bigtext
from .lifestyle_theme import Theme, rows


def _remaining_seconds(target: datetime.datetime) -> float:
    now = datetime.datetime.now()
    if target.tzinfo is not None:
        now = now.astimezone()
    return (target - now).total_seconds()


def remaining_seconds(countdown: dict) -> float | None:
    try:
        return _remaining_seconds(datetime.datetime.fromisoformat(countdown["target_iso"]))
    except (KeyError, ValueError):
        return None


def is_stale(countdown: dict) -> bool:
    """True once the target is more than a day past.

    AppState.lifestyle_screens() uses this to stop giving a finished countdown
    panel time -- the same "stale, not gone" treatment a bet gets when its
    game falls off the slate. It is never deleted automatically, only left off
    the panel until you remove it.
    """
    remaining = remaining_seconds(countdown)
    return remaining is None or remaining < -86400


def draw(canvas, countdown: dict, theme=None) -> None:
    theme = theme or Theme()
    box = theme.box
    remaining = remaining_seconds(countdown)
    if remaining is None:
        bigtext.fill(canvas, "BAD DATE", box, theme.accent)
        return

    label = (countdown.get("label") or "COUNTDOWN").strip().upper()

    if remaining <= 0:
        top, bottom = rows(box, [3, 7])
        bigtext.fill(canvas, label, top, theme.accent)
        bigtext.fill(canvas, "TODAY", bottom, theme.main)
        return

    days = int(remaining // 86400)
    hours = int(remaining % 86400 // 3600)
    minutes = int(remaining % 3600 // 60)
    seconds = int(remaining % 60)

    label_row, big_row, clock_row = rows(box, [2, 7, 3])
    bigtext.fill(canvas, label, label_row, theme.accent)
    bigtext.fill(canvas, f"{days} DAY{'S' if days != 1 else ''}" if days else "< 1 DAY",
                 big_row, theme.main)
    bigtext.fill(canvas, f"{hours:02d}:{minutes:02d}:{seconds:02d}", clock_row, theme.accent)


# -- compact: sized to a face slot rather than the whole panel -------------

def draw_compact(canvas, x, y, w_, h_, countdown: dict | None, theme=None) -> None:
    if not countdown:
        return
    theme = theme or Theme()
    remaining = remaining_seconds(countdown)
    if remaining is None:
        return
    days = max(0, int(remaining // 86400))
    text = "TODAY" if remaining <= 0 else f"{days}D"
    label = (countdown.get("label") or "").strip().upper()[:14]

    box = (x + 1, y + 1, w_ - 2, h_ - 2)
    if label and h_ >= 16:
        top, bottom = rows(box, [3, 7], gap=1)
        bigtext.fill(canvas, label, top, theme.accent)
        bigtext.fill(canvas, text, bottom, theme.main)
    else:
        bigtext.fill(canvas, text, box, theme.main)
