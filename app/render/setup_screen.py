"""What the panel shows when it needs a person: no Wi-Fi, or where to find it.

These take over the whole display rather than joining the rotation. A panel
that can't reach the internet has nothing else worth saying, and the one thing
you need from across the room is what to do about it -- the network to join,
the password, and the address to open. All three are on screen at once so
nobody has to remember a step.
"""

from . import bigtext
from .lifestyle_theme import rows
from ..matrix import HEIGHT, WIDTH

AMBER = (255, 190, 0)
WHITE = (255, 255, 255)
DIM = (110, 110, 115)
RED = (235, 70, 70)

BOX = (2, 2, WIDTH - 4, HEIGHT - 4)


def draw_no_wifi(canvas, ssid: str, password: str, url: str) -> None:
    """The hotspot is up and waiting to be joined."""
    heading, join, creds, where = rows(BOX, [3, 2, 3, 2], gap=1)
    bigtext.fill(canvas, "NO WIFI", heading, RED)
    bigtext.fill(canvas, "JOIN", join, DIM)
    bigtext.fill(canvas, ssid, creds, AMBER)
    bigtext.fill(canvas, f"PW {password}", where, WHITE)


def draw_no_wifi_alt(canvas, url: str) -> None:
    """Second half of the same message: where to go once you've joined.

    Alternated with the first rather than crammed alongside it -- five things
    at once on 64 pixels leaves every one of them too small to read, and this
    screen is useless if it can't be read from where you're standing.
    """
    heading, addr, note = rows(BOX, [2, 3, 2], gap=1)
    bigtext.fill(canvas, "THEN OPEN", heading, DIM)
    bigtext.fill(canvas, url.replace("http://", ""), addr, AMBER)
    bigtext.fill(canvas, "TO SET UP WIFI", note, WHITE)


def _reason(detail: str) -> str:
    """The failure, in the few words a panel can show across a room."""
    d = (detail or "").lower()
    if "password" in d:
        return "WRONG PASSWORD"
    if "range" in d or "see " in d:
        return "NOT IN RANGE"
    if "address" in d:
        return "NO ADDRESS"
    return "TRY AGAIN"


def draw_join_failed(canvas, ssid: str, detail: str) -> None:
    """Why the last join didn't work.

    The phone that asked has been kicked off the hotspot by the time the join
    fails, so the panel is the one place guaranteed to be able to say what
    happened. Without this, a failed join looked exactly like Join doing
    nothing at all.
    """
    heading, net, why = rows(BOX, [3, 2, 2], gap=1)
    bigtext.fill(canvas, "NOT JOINED", heading, RED)
    bigtext.fill(canvas, (ssid or "").upper(), net, AMBER)
    bigtext.fill(canvas, _reason(detail), why, WHITE)


def draw_connecting(canvas) -> None:
    heading, note = rows(BOX, [3, 2], gap=1)
    bigtext.fill(canvas, "WIFI", heading, AMBER)
    bigtext.fill(canvas, "CONNECTING", note, DIM)


def draw(canvas, network: dict, phase: float, host: str) -> bool:
    """The right setup screen for the current network state, or False if the
    panel should get on with showing sport.

    `phase` is a free-running seconds counter; the two halves of the no-Wi-Fi
    message alternate on it.
    """
    from .. import wifi

    if not network or not network.get("supported"):
        return False

    if network.get("hotspot"):
        # After a failed join, why it failed rotates in ahead of the
        # instructions -- for as long as wifi.last_join() keeps reporting it.
        join = network.get("last_join") or {}
        failed = join.get("ok") is False
        screens = 3 if failed else 2
        step = int(phase / 4) % screens
        if failed and step == 0:
            draw_join_failed(canvas, join.get("ssid", ""), join.get("detail", ""))
        elif step == screens - 1:
            draw_no_wifi_alt(canvas, wifi.HOTSPOT_URL)
        else:
            draw_no_wifi(canvas, wifi.HOTSPOT_SSID, wifi.HOTSPOT_PASSWORD,
                         wifi.HOTSPOT_URL)
        return True

    if not network.get("online"):
        draw_connecting(canvas)
        return True

    return False
