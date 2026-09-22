"""Weather: current conditions plus today's sunrise/sunset, via Open-Meteo.

Chosen over the usual weather APIs because it needs no key and no account --
this is a project other people are meant to be able to clone and run, and "go
sign up for an API key" is real friction for that. Coverage is worldwide, and
a place name is enough; Open-Meteo's own geocoder resolves it to coordinates.

Two calls, not a class: unlike the sport sources there's no per-poll cursor or
connection to keep alive between calls, just "look up a place once" and
"fetch the forecast every so often" -- see the poller in app/main.py.
"""

import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo's WMO weather codes, collapsed to a short panel label and one of
# four icon shapes lifestyle_weather.py knows how to draw. Codes it doesn't
# recognise fall back to "cloud" in fetch() below, not to an exception.
WMO = {
    0: ("CLEAR", "clear"),
    1: ("MOSTLY CLEAR", "clear"),
    2: ("PARTLY CLOUDY", "cloud"),
    3: ("CLOUDY", "cloud"),
    45: ("FOG", "cloud"),
    48: ("FOG", "cloud"),
    51: ("DRIZZLE", "rain"),
    53: ("DRIZZLE", "rain"),
    55: ("DRIZZLE", "rain"),
    56: ("FREEZING DRIZZLE", "rain"),
    57: ("FREEZING DRIZZLE", "rain"),
    61: ("RAIN", "rain"),
    63: ("RAIN", "rain"),
    65: ("HEAVY RAIN", "rain"),
    66: ("FREEZING RAIN", "rain"),
    67: ("FREEZING RAIN", "rain"),
    71: ("SNOW", "snow"),
    73: ("SNOW", "snow"),
    75: ("HEAVY SNOW", "snow"),
    77: ("SNOW GRAINS", "snow"),
    80: ("SHOWERS", "rain"),
    81: ("SHOWERS", "rain"),
    82: ("HEAVY SHOWERS", "rain"),
    85: ("SNOW SHOWERS", "snow"),
    86: ("SNOW SHOWERS", "snow"),
    95: ("THUNDERSTORM", "rain"),
    96: ("THUNDERSTORM", "rain"),
    99: ("THUNDERSTORM", "rain"),
}


def geocode(place: str) -> dict | None:
    """{"lat", "lon", "name"} for a place name, or None if nothing matched."""
    place = (place or "").strip()
    if not place:
        return None
    try:
        r = requests.get(GEOCODE_URL, params={"name": place, "count": 1}, timeout=10)
        results = r.json().get("results")
        if not results:
            return None
        hit = results[0]
        bits = [hit.get("admin1"), hit.get("country")]
        label = ", ".join(b for b in bits if b)
        name = f"{hit['name']}, {label}" if label else hit["name"]
        return {"lat": hit["latitude"], "lon": hit["longitude"], "name": name}
    except Exception:
        return None


def fetch(lat: float, lon: float, units: str = "f") -> dict | None:
    """Current conditions plus today's high/low and sunrise/sunset.

    Returns None on any failure -- a bad or timed-out fetch should just mean
    "no weather screen this cycle," the same as every other source in this
    app, never a crash.
    """
    try:
        r = requests.get(FORECAST_URL, params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weathercode,is_day",
            "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset",
            "temperature_unit": "fahrenheit" if units == "f" else "celsius",
            "timezone": "auto",
        }, timeout=10)
        data = r.json()
        cur = data["current"]
        day = data["daily"]
        code = int(cur["weathercode"])
        label, icon = WMO.get(code, ("CLOUDY", "cloud"))
        return {
            "temp": round(cur["temperature_2m"]),
            "is_day": bool(cur["is_day"]),
            "condition": label,
            "icon": icon,
            "high": round(day["temperature_2m_max"][0]),
            "low": round(day["temperature_2m_min"][0]),
            "sunrise": day["sunrise"][0],
            "sunset": day["sunset"][0],
        }
    except Exception:
        return None
