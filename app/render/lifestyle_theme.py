"""Colour and size, shared by every lifestyle screen.

Two ideas, both deliberately small:

  MAIN and ACCENT. One colour for the thing you're reading -- the time, the
  temperature -- and one for everything supporting it. Two is enough to make a
  screen feel chosen rather than default, and few enough that the settings
  page stays a couple of swatches per screen instead of a paint program.

  SCALE shrinks the LAYOUT BOX, not the individual pieces. Every screen lays
  itself out inside `box`, so one slider moves everything together and nothing
  can drift out of proportion with anything else. 100% is the whole panel with
  a two-pixel margin, which is the point -- these screens are meant to fill it.
"""

from ..matrix import HEIGHT, WIDTH
from .widgets import WHITE

GREY = (128, 128, 128)

# Offered as swatches in the control center. Chosen to stay legible on a black
# panel at low brightness -- nothing navy, nothing that muddies to grey.
PRESETS = [
    ("White", "#FFFFFF"),
    ("Amber", "#FFC400"),
    ("Red", "#FF3232"),
    ("Green", "#00DC3C"),
    ("Cyan", "#00D0E0"),
    ("Blue", "#4090FF"),
    ("Purple", "#B060FF"),
    ("Pink", "#FF5FA0"),
    ("Orange", "#FF7A18"),
    ("Grey", "#808080"),
]

MIN_SCALE, MAX_SCALE = 40, 100

# The screens that carry their own colour and size settings.
SCREENS = ("clock", "weather", "message", "countdown")

DEFAULTS = {s: {"main": "#FFFFFF", "accent": "#808080"} for s in SCREENS}


def to_rgb(value, fallback=WHITE):
    """"#RRGGBB" -> (r, g, b). Anything unparseable falls back rather than
    raising: a hand-edited config shouldn't be able to stop the panel drawing."""
    try:
        text = str(value).strip().lstrip("#")
        if len(text) == 3:
            text = "".join(c * 2 for c in text)
        if len(text) != 6:
            return fallback
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except (TypeError, ValueError):
        return fallback


def dim(rgb, amount=0.45):
    """A quieter version of a colour, for detail that shouldn't compete."""
    return tuple(max(0, min(255, round(c * amount))) for c in rgb)


# -- validation ------------------------------------------------------------
# The ONE place a colour or a size is checked. Both the settings API and the
# config loader go through these, so a value the web UI would reject can't get
# in through a hand-edited config.json either, and neither can drift from the
# other as the rules change.

def clean_color(value):
    """"#RRGGBB" for anything parseable, else None for the caller to reject
    or fall back on."""
    rgb = to_rgb(value, None)
    return None if rgb is None else "#%02X%02X%02X" % rgb


def clean_scale(value):
    """A size percent clamped into range, or None if it isn't a number."""
    try:
        return max(MIN_SCALE, min(MAX_SCALE, int(value)))
    except (TypeError, ValueError):
        return None


def apply_colors(current: dict, incoming, strict=False) -> list:
    """Merge `incoming` per-screen colours into `current`, in place.

    Merged rather than replaced so a caller can send just the swatch that
    changed. Returns the values it refused, so a strict caller (the API) can
    turn them into a 400 while a lenient one (loading config) just keeps the
    default and carries on.
    """
    rejected = []
    if not isinstance(incoming, dict):
        return rejected
    for screen, entry in incoming.items():
        if screen not in SCREENS or not isinstance(entry, dict):
            if strict and screen not in SCREENS:
                rejected.append(screen)
            continue
        for role in ("main", "accent"):
            if entry.get(role) is None:
                continue
            cleaned = clean_color(entry[role])
            if cleaned is None:
                rejected.append(entry[role])
                continue
            current.setdefault(screen, dict(DEFAULTS[screen]))[role] = cleaned
    return rejected


def apply_scale(current: dict, incoming) -> list:
    """Merge `incoming` per-screen sizes into `current`, in place."""
    rejected = []
    if not isinstance(incoming, dict):
        return rejected
    for screen, value in incoming.items():
        if screen not in SCREENS:
            continue
        cleaned = clean_scale(value)
        if cleaned is None:
            rejected.append(value)
            continue
        current[screen] = cleaned
    return rejected


class Theme:
    """The colours and the box one screen draws inside."""

    def __init__(self, main=WHITE, accent=GREY, scale=100):
        self.main = main
        self.accent = accent
        self.scale = max(MIN_SCALE, min(MAX_SCALE, int(scale)))

    @classmethod
    def of(cls, state, screen: str):
        colours = (getattr(state, "lifestyle_colors", None) or {}).get(screen) or {}
        scales = getattr(state, "lifestyle_scale", None) or {}
        return cls(
            main=to_rgb(colours.get("main"), WHITE),
            accent=to_rgb(colours.get("accent"), GREY),
            scale=scales.get(screen, 100),
        )

    @property
    def box(self):
        """(x, y, w, h) to lay out in. 2px of margin at full size so nothing
        renders hard against the bezel."""
        w = round((WIDTH - 4) * self.scale / 100)
        h = round((HEIGHT - 4) * self.scale / 100)
        return (WIDTH - w) // 2, (HEIGHT - h) // 2, w, h


def rows(box, weights, gap=2):
    """Split `box` into stacked rows sized by `weights`.

    The layout primitive the screens share: say what's on the screen and how
    much of it each piece deserves, and the arithmetic of filling the panel --
    including the scale slider and the gaps -- happens once, here.
    """
    x, y, w, h = box
    live = [wt for wt in weights if wt > 0]
    if not live:
        return []
    total = sum(live)
    spare = h - gap * (len(live) - 1)
    out, top = [], y
    for i, weight in enumerate(weights):
        if weight <= 0:
            out.append(None)
            continue
        # Last row takes the remainder, so rounding never leaves a spare pixel.
        used = sum(1 for wt in weights[:i] if wt > 0)
        is_last = used == len(live) - 1
        rh = (y + h - top) if is_last else round(spare * weight / total)
        out.append((x, top, w, rh))
        top += rh + gap
    return out
