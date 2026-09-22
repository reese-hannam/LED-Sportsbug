"""Team colors, tuned for LED rather than for print.

Several teams' primary colors are near-black navies (NYY, MIL, TB...). On a P3
matrix those read as "off pixel", so every color gets floored to a minimum
luminance. Keep the raw table honest and let led() do the adjusting.
"""

MLB = {
    "ARI": (167, 25, 48), "ATH": (0, 56, 49), "ATL": (206, 17, 65),
    "BAL": (223, 70, 1), "BOS": (189, 48, 57), "CHC": (14, 51, 134),
    "CIN": (198, 1, 31), "CLE": (227, 25, 55), "COL": (51, 0, 111),
    "CWS": (39, 37, 31), "DET": (12, 35, 64), "HOU": (235, 110, 31),
    "KC": (0, 70, 135), "LAA": (186, 0, 33), "LAD": (0, 90, 156),
    "MIA": (0, 163, 224), "MIL": (18, 40, 75), "MIN": (0, 43, 92),
    "NYM": (0, 45, 114), "NYY": (12, 35, 64), "PHI": (232, 24, 40),
    "PIT": (253, 184, 39), "SD": (47, 36, 29), "SEA": (0, 92, 92),
    "SF": (253, 90, 30), "STL": (196, 30, 58), "TB": (9, 44, 92),
    "TEX": (0, 50, 120), "TOR": (19, 74, 142), "WSH": (171, 0, 3),
}
# statsapi uses AZ/ATH where other feeds use ARI/OAK.
MLB["AZ"] = MLB["ARI"]
MLB["OAK"] = MLB["ATH"]

NFL = {
    "ARI": (151, 35, 63), "ATL": (167, 25, 48), "BAL": (26, 25, 95),
    "BUF": (0, 51, 141), "CAR": (0, 133, 202), "CHI": (11, 22, 42),
    "CIN": (251, 79, 20), "CLE": (255, 60, 0), "DAL": (0, 53, 148),
    "DEN": (251, 79, 20), "DET": (0, 118, 182), "GB": (24, 48, 40),
    "HOU": (3, 32, 47), "IND": (0, 44, 95), "JAX": (0, 103, 120),
    "KC": (227, 24, 55), "LAC": (0, 128, 198), "LAR": (0, 53, 148),
    "LV": (165, 172, 175), "MIA": (0, 142, 151), "MIN": (79, 38, 131),
    "NE": (0, 34, 68), "NO": (211, 188, 141), "NYG": (1, 35, 82),
    "NYJ": (18, 87, 64), "PHI": (0, 76, 84), "PIT": (255, 182, 18),
    "SEA": (0, 34, 68), "SF": (170, 0, 0), "TB": (213, 10, 10),
    "TEN": (12, 35, 64), "WSH": (90, 20, 20),
}

# Below this perceived luminance a color is indistinguishable from black on the panel.
MIN_LUMA = 90


def led(rgb: tuple, min_luma: int = MIN_LUMA) -> tuple:
    """Lift a color until it's actually visible as a color on the matrix."""
    r, g, b = rgb
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    if luma >= min_luma:
        return (int(r), int(g), int(b))
    if luma < 1:
        return (min_luma, min_luma, min_luma)
    scale = min_luma / luma
    return tuple(min(255, int(c * scale)) for c in (r, g, b))


def team_color(sport: str, abbrev: str) -> tuple:
    table = MLB if sport == "mlb" else NFL
    return led(table.get(abbrev.upper(), (200, 200, 200)))
