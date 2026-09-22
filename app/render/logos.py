"""Team logos for the score bugs.

The artwork is downloaded and pre-shrunk to panel size once, by
tools/fetch_logos.py (the installer runs it), so this does no network I/O, no resampling, and no PIL work per frame -- it
loads a small PNG once, turns it into a flat list of (x, y, rgb), and every
later frame is that list replayed into the canvas.

Why a pixel list rather than an image: the matrix API is SetPixel, so an image
would have to be walked per frame anyway. Walking it once at load and skipping
the black pixels (most of a 16x16 logo is background) makes the per-frame cost
proportional to the ink, not the area -- typically 120-180 pixels instead of
256, on a Pi Zero rendering at 30fps.

Colours are used exactly as the artwork has them. Dark logos on a black panel
ARE hard to see -- Yankee navy especially -- and that is accepted deliberately:
lifting them to be visible changes what colour they are, and a logo that is dim
but right beats one that is bright but wrong. The abbreviation beside it is what
guarantees you can always tell who is playing.
"""

import os

ASSETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "assets", "logos",
)

# The size each sport's SCORE BUG uses. The pre-game layouts ask for bigger
# artwork explicitly. College is 12 because its rank column takes the room.
BUG_SIZE = {"nfl": 16, "mlb": 16, "cfb": 12}

# (sport, key) -> [(x, y, (r, g, b))] or None when there's no artwork.
_cache: dict = {}


def _load(sport: str, key: str, size: int):
    path = os.path.join(ASSETS, sport, str(size), f"{key.upper()}.png")
    if not os.path.exists(path):
        return None
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
    except Exception:
        # Missing Pillow or a corrupt file must never stop the panel drawing --
        # the bug just renders without a logo, exactly as it did before.
        return None

    px = img.load()
    out = []
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = px[x, y]
            if r or g or b:
                out.append((x, y, (r, g, b)))
    return out or None


# Which sports draw logos. Initialised here rather than only by set_enabled():
# as a bare `global` it existed solely if someone had remembered to call that
# first, so every entry point other than app.main -- tools/preview.py included
# -- crashed with NameError the moment it drew a score bug. A module owns its
# own default; callers override it.
_enabled: dict = {"mlb": True, "nfl": True, "cfb": False}


def set_enabled(mapping: dict) -> None:
    global _enabled
    _enabled = {str(k): bool(v) for k, v in (mapping or {}).items()}


def enabled(sport: str | None = None):
    return dict(_enabled) if sport is None else bool(_enabled.get(sport))


def logo(sport: str, abbrev: str, size: int | None = None):
    """Pixels for a team's logo at `size`, or None if there isn't one."""
    if not abbrev:
        return None
    size = size or BUG_SIZE.get(sport, 16)
    ident = (sport, abbrev.upper(), size)
    if ident not in _cache:
        _cache[ident] = _load(sport, abbrev, size)
    return _cache[ident]


def draw(canvas, sport: str, abbrev: str, x: int, y: int, size: int | None = None) -> bool:
    """Blit a logo with its top-left at (x, y).

    Returns False when logos are off or there's no artwork for this team, which
    is the renderers' signal to fall back to the colour spine and the original
    layout -- so a missing logo degrades to exactly what the panel did before.
    """
    if not _enabled.get(sport):
        return False
    pixels = logo(sport, abbrev, size)
    if not pixels:
        return False
    for dx, dy, (r, g, b) in pixels:
        canvas.SetPixel(x + dx, y + dy, r, g, b)
    return True


def key_for(game, side: str) -> str:
    """The artwork key for one side of a game.

    College is keyed by ESPN's team **id**, not an abbreviation: there are ~270
    Division I teams once FCS opponents are counted, abbreviations collide
    across the divisions, and the scoreboard hands the id over anyway. The
    other sports have thirty-odd stable abbreviations and no such problem.
    """
    team = game.away if side == "away" else game.home
    if game.sport == "cfb":
        tid = (game.detail or {}).get(f"id_{side}")
        if tid:
            return str(tid)
    return team.abbrev


def draw_team(canvas, game, side: str, x: int, y: int, size: int | None = None) -> bool:
    """Blit one side's logo. False if there's nothing to draw."""
    return draw(canvas, game.sport, key_for(game, side), x, y, size)


def available(sport: str, size: int | None = None) -> int:
    """How many logos are on disk for a sport -- for the UI to report."""
    size = size or BUG_SIZE.get(sport, 16)
    try:
        return len([f for f in os.listdir(os.path.join(ASSETS, sport, str(size)))
                    if f.endswith(".png")])
    except OSError:
        return 0
