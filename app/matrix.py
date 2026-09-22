"""Hardware/emulator shim.

Everything above this module imports RGBMatrix, graphics, etc. from here and
never touches rgbmatrix or RGBMatrixEmulator directly, so the same code runs on
a laptop and on the Pi.

Which one is used is DETECTED, not configured: if the real `rgbmatrix` bindings
import, they're the real thing and they win. They only exist where someone has
built hzeller's library, which is the Pi. Defaulting to the emulator instead
meant a Pi that booted with MATRIX_EMULATE unset came up showing nothing at all
while looking perfectly healthy in the logs -- the worst kind of failure to
diagnose from across a room. MATRIX_EMULATE is still honoured as an override in
both directions, for testing the emulator on a Pi or vice versa.

Panel geometry and wiring are environment variables because they're properties
of one physical build, not of this program: a different HAT or a longer chain
shouldn't need a code change. The defaults describe the rig this was written
for -- two 64x64 P3 panels on an Adafruit HAT.
"""

import os
import sys

# Checked here because this is the first thing app.main imports. The code uses
# `str | None` annotations, which are evaluated at runtime and are a SyntaxError
# before 3.10 -- on Raspberry Pi OS Bullseye (Python 3.9) that surfaces as a
# parse error in an arbitrary module, which says nothing about the real cause.
if sys.version_info < (3, 10):
    raise SystemExit(
        f"This needs Python 3.10 or newer; found {sys.version.split()[0]}.\n"
        "Raspberry Pi OS Bookworm ships 3.11 and is what to use -- Bullseye's "
        "3.9 is too old."
    )


def _env_flag(name):
    """None when unset, else True/False. Distinguishing 'unset' from 'off'
    is the whole point -- unset means 'work it out yourself'."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return None
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


_forced = _env_flag("MATRIX_EMULATE")
if _forced is None:
    try:
        import rgbmatrix  # noqa: F401
        EMULATE = False
    except ImportError:
        EMULATE = True
else:
    EMULATE = _forced

if EMULATE:
    from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions, graphics
else:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics  # type: ignore

# Two 64x64 panels daisy-chained -> one 128x64 canvas.
PANEL_ROWS = _env_int("MATRIX_ROWS", 64)
PANEL_COLS = _env_int("MATRIX_COLS", 64)
CHAIN = _env_int("MATRIX_CHAIN", 2)
PARALLEL = _env_int("MATRIX_PARALLEL", 1)

# Wiring.
#
# "regular" is the plain Pi GPIO pinout, and it's the default because it is what
# this build actually uses: a SEENGREAT RGB Matrix Adapter Rev 3.0, which is not
# an Adafruit HAT and does not share its pin assignment. "adafruit-hat" and
# "adafruit-hat-pwm" are there for anyone on genuine Adafruit hardware -- the
# -pwm variant also needs a solder jumper between GPIO4 and GPIO18.
#
# gpio_slowdown is the other one that usually needs changing: too low and the
# panel sparkles or ghosts, too high and the refresh rate drops. 1-2 suits a
# Pi Zero 2, 4 a Pi 4.
HARDWARE_MAPPING = os.getenv("MATRIX_MAPPING", "regular")
GPIO_SLOWDOWN = _env_int("MATRIX_SLOWDOWN", 2)

# Panel driver chip.
#
# Empty means "standard shift-register panel, no init needed", which covers most
# of them. Panels built on FM6126A (and FM6127) chips instead need a specific
# register-init sequence sent before they will respond to anything at all --
# without it the panel stays completely black while the library reports success
# and every log line looks healthy. That failure is indistinguishable from a
# dead ribbon cable, bad power, or a wrong pinout, which is exactly why it cost
# this project several hours. The panels on this build are FM6126A, so that is
# the default; set MATRIX_PANEL_TYPE="" for ordinary panels.
PANEL_TYPE = os.getenv("MATRIX_PANEL_TYPE", "FM6126A")

# Hardware PWM (the good path) conflicts with the Pi's onboard sound module.
# The install disables snd_bcm2835; where that hasn't happened the library
# refuses to start at all, and this is the escape hatch -- at the cost of
# visible flicker, so it is off by default rather than silently degrading.
NO_HARDWARE_PULSE = _env_flag("MATRIX_NO_HW_PULSE") or False

WIDTH = PANEL_COLS * CHAIN
HEIGHT = PANEL_ROWS

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets", "fonts")

_font_cache: dict = {}


def font(name: str):
    """Load a BDF font by bare name ('6x10'), cached."""
    if name not in _font_cache:
        f = graphics.Font()
        f.LoadFont(os.path.join(FONT_DIR, f"{name}.bdf"))
        _font_cache[name] = f
    return _font_cache[name]


def create_matrix(brightness: int = 60) -> RGBMatrix:
    opts = RGBMatrixOptions()
    opts.rows = PANEL_ROWS
    opts.cols = PANEL_COLS
    opts.chain_length = CHAIN
    opts.parallel = PARALLEL
    opts.brightness = brightness
    opts.gpio_slowdown = GPIO_SLOWDOWN
    opts.hardware_mapping = HARDWARE_MAPPING
    # These two exist on the real bindings but not on every emulator release,
    # so a missing attribute has to be survivable -- but NEVER probe for them
    # with hasattr().
    #
    # hasattr() calls the GETTER, and rgbmatrix's panel_type getter returns
    # `self.__options.panel_type`, a `const char *` that is NULL until someone
    # assigns it. Cython converting a NULL char* to a Python object
    # dereferences null: an immediate SEGFAULT, no traceback, no error, before
    # the panel is ever touched. It reads exactly like a hardware fault, and it
    # fires on the healthy path -- the check meant to make this safe is the
    # thing that crashes. try/except assigns first and asks questions later,
    # which is both correct and cheaper.
    if PANEL_TYPE:
        try:
            opts.panel_type = PANEL_TYPE
        except AttributeError:
            pass  # emulator, or a binding too old to know about panel types
    if NO_HARDWARE_PULSE:
        try:
            opts.disable_hardware_pulsing = True
        except AttributeError:
            pass
    # The C library drops to a normal user after grabbing GPIO, which severs
    # the process from the venv's privileges it still needs; the service file
    # runs as root deliberately instead.
    opts.drop_privileges = False
    return RGBMatrix(options=opts)


def describe() -> str:
    """One line for the startup banner: what we're driving and how."""
    if EMULATE:
        return f"emulator {WIDTH}x{HEIGHT}"
    extra = f", {PANEL_TYPE}" if PANEL_TYPE else ""
    if NO_HARDWARE_PULSE:
        extra += ", no-hw-pulse"
    return (f"panel {WIDTH}x{HEIGHT} via {HARDWARE_MAPPING} "
            f"(chain {CHAIN}, slowdown {GPIO_SLOWDOWN}{extra})")


def text_width(f, s: str) -> int:
    """Advance width of a string, matching how DrawText will lay it out."""
    return sum(f.CharacterWidth(ord(ch)) for ch in s)
