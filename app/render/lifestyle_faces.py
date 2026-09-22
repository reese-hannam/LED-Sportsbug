"""Custom dashboards: a handful of preset slot layouts, each filled with
whichever pieces of data you pick -- the "Apple Watch face" idea, minus
freeform placement. A fixed geometry per layout means every face looks
aligned regardless of what's put in it, and there's no drag-and-drop
placement code to get wrong at 64 pixels tall.
"""

from . import lifestyle_clock as clock
from . import lifestyle_weather as weather
from . import lifestyle_message as message
from . import lifestyle_countdown as countdown
from .lifestyle_theme import Theme

# Each layout is a list of (x, y, w, h) slot boxes, tuned to the 128x64 panel.
LAYOUTS = {
    "solo": [(0, 0, 128, 64)],
    "big_strip": [(0, 0, 128, 42), (0, 42, 128, 22)],
    "halves": [(0, 0, 128, 32), (0, 32, 128, 32)],
    "quad": [(0, 0, 64, 32), (64, 0, 64, 32), (0, 32, 64, 32), (64, 32, 64, 32)],
}
LABEL = {
    "solo": "Solo -- one big piece",
    "big_strip": "Big + strip",
    "halves": "Two halves",
    "quad": "Quad grid",
}

# What a slot can be filled with. "countdown" additionally carries a
# `countdown_id` naming which saved countdown to show.
COMPLICATIONS = ("time", "date", "temp", "conditions", "hilo", "message", "countdown")
COMPLICATION_LABEL = {
    "time": "Time", "date": "Date", "temp": "Temperature",
    "conditions": "Conditions", "hilo": "High / Low", "message": "Message",
    "countdown": "Countdown",
}


def slot_count(layout: str) -> int:
    return len(LAYOUTS.get(layout, LAYOUTS["solo"]))


def _draw_slot(canvas, box, slot, state):
    """One slot, using the COLOURS of the screen its data comes from.

    A face showing the time and the temperature picks up the clock's colours
    for one and the weather's for the other, so setting a colour once applies
    everywhere that data appears rather than needing to be set again per face.
    """
    x, y, w_, h_ = box
    kind = (slot or {}).get("kind")
    if kind == "time":
        clock.draw_compact(canvas, x, y, w_, h_, state.clock_seconds,
                           Theme.of(state, "clock"))
    elif kind == "date":
        clock.draw_date_compact(canvas, x, y, w_, h_, Theme.of(state, "clock"))
    elif kind == "temp":
        weather.draw_temp_compact(canvas, x, y, w_, h_, state.weather,
                                  state.weather_units, Theme.of(state, "weather"))
    elif kind == "conditions":
        weather.draw_conditions_compact(canvas, x, y, w_, h_, state.weather,
                                        Theme.of(state, "weather"))
    elif kind == "hilo":
        weather.draw_hilo_compact(canvas, x, y, w_, h_, state.weather,
                                  Theme.of(state, "weather"))
    elif kind == "message":
        message.draw_compact(canvas, x, y, w_, h_, state.message_text,
                             Theme.of(state, "message"))
    elif kind == "countdown":
        cd = next((c for c in state.countdowns
                   if c["id"] == (slot or {}).get("countdown_id")), None)
        countdown.draw_compact(canvas, x, y, w_, h_, cd, Theme.of(state, "countdown"))


def draw(canvas, face: dict, state) -> None:
    boxes = LAYOUTS.get(face.get("layout"), LAYOUTS["solo"])
    slots = face.get("slots") or []
    for i, box in enumerate(boxes):
        _draw_slot(canvas, box, slots[i] if i < len(slots) else None, state)
