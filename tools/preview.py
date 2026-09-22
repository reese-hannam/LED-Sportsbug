"""Render score bugs to a PNG instead of a live panel.

Duck-types the matrix canvas so the real renderers draw into it unmodified, then
scales up with a dark grid so the result reads like an actual P3 matrix.

    python tools/preview.py                 # live MLB games, first 4
    python tools/preview.py --mode nfl --preseason --year 2026 --week 3
    python tools/preview.py --demo          # synthetic states, no network
"""

import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MATRIX_EMULATE", "1")

from app.matrix import WIDTH, HEIGHT  # noqa: E402
from app.render import mlb_bug, nfl_bug  # noqa: E402
from app.sources.base import Game, Team, LIVE, FINAL, PRE  # noqa: E402
from app.sources.colors import team_color  # noqa: E402


class PreviewCanvas:
    """Minimal stand-in for RGBMatrixEmulator's Canvas."""

    def __init__(self, width=WIDTH, height=HEIGHT):
        self.width = width
        self.height = height
        self.Clear()

    def Clear(self):
        self.pixels = np.zeros((self.height, self.width, 3), dtype=np.uint8)

    def SetPixel(self, x, y, r, g, b):
        if 0 <= x < self.width and 0 <= y < self.height:
            self.pixels[int(y), int(x)] = (r, g, b)

    def to_image(self, scale=6, gap=1, brightness=1.0):
        """Upscale with an inter-pixel gutter so it looks like discrete LEDs."""
        cell = scale + gap
        img = Image.new("RGB", (self.width * cell, self.height * cell), (12, 12, 14))
        px = img.load()
        arr = (self.pixels.astype(np.float32) * brightness).clip(0, 255).astype(np.uint8)
        for y in range(self.height):
            for x in range(self.width):
                r, g, b = (int(v) for v in arr[y, x])
                if r or g or b:
                    for dy in range(scale):
                        for dx in range(scale):
                            px[x * cell + dx, y * cell + dy] = (r, g, b)
        return img


def demo_games():
    def mk(sport, a, h, **kw):
        return Game(
            id=f"{a}{h}", sport=sport,
            away=Team(a, kw.pop("as_", 0), team_color(sport, a), record="70-60"),
            home=Team(h, kw.pop("hs", 0), team_color(sport, h), record="65-65"),
            **kw,
        )

    return [
        mk("mlb", "BOS", "MIA", as_=0, hs=2, state=LIVE, period="B7",
           detail={"balls": 2, "strikes": 2, "outs": 1, "bases": [True, True, False],
                   "is_top": False, "inning_state": "Bottom"}),
        mk("mlb", "NYY", "HOU", as_=11, hs=3, state=LIVE, period="T9",
           detail={"balls": 3, "strikes": 1, "outs": 2, "bases": [False, False, True],
                   "is_top": True, "inning_state": "Top"}),
        mk("mlb", "CHC", "AZ", as_=0, hs=2, state=FINAL, period="F"),
        mk("mlb", "LAD", "SF", state=PRE, start_utc="2026-08-27T02:15:00Z"),
        mk("nfl", "CLE", "PIT", as_=17, hs=24, state=LIVE, period="3RD",
           detail={"clock": "5:32", "down_distance": "1st & 10", "yardline": "PIT 35",
                   "possession": "PIT", "red_zone": False}),
        mk("nfl", "KC", "BUF", as_=28, hs=31, state=LIVE, period="4TH",
           detail={"clock": "0:47", "down_distance": "3rd & 2", "yardline": "KC 8",
                   "possession": "BUF", "red_zone": True}),
    ]


def fetch_games(args):
    if args.mode == "mlb":
        from app.sources.mlb import MLBSource
        return MLBSource(date=args.date).fetch()
    from app.sources.nfl import NFLSource, PRESEASON, REGULAR
    return NFLSource(
        seasontype=PRESEASON if args.preseason else REGULAR,
        year=args.year, week=args.week,
    ).fetch()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=("mlb", "nfl"), default="mlb")
    p.add_argument("--demo", action="store_true", help="synthetic games, no network")
    p.add_argument("--date")
    p.add_argument("--preseason", action="store_true")
    p.add_argument("--year", type=int)
    p.add_argument("--week", type=int)
    p.add_argument("--count", type=int, default=4)
    p.add_argument("--scale", type=int, default=6)
    p.add_argument("--out", default="preview.png")
    args = p.parse_args()

    games = demo_games() if args.demo else fetch_games(args)
    if not args.demo:
        live = [g for g in games if g.is_live]
        games = (live or games)[: args.count]

    if not games:
        print("no games to render")
        return

    tiles = []
    for g in games:
        canvas = PreviewCanvas()
        (mlb_bug if g.sport == "mlb" else nfl_bug).draw(canvas, g)
        tiles.append((g, canvas.to_image(scale=args.scale)))
        print(f"  {g.matchup:<12} {g.away.score}-{g.home.score:<3} {g.state:<5} "
              f"{g.period:<5} {g.status_detail}")

    tw, th = tiles[0][1].size
    pad = 14
    sheet = Image.new("RGB", (tw + pad * 2, (th + pad) * len(tiles) + pad), (28, 28, 32))
    for i, (_, img) in enumerate(tiles):
        sheet.paste(img, (pad, pad + i * (th + pad)))
    sheet.save(args.out)
    print(f"\nwrote {args.out}  ({len(tiles)} bugs, each {WIDTH}x{HEIGHT})")


if __name__ == "__main__":
    main()
