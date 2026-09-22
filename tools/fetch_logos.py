"""Download team logos and pre-shrink them to panel size.

    .venv/bin/python tools/fetch_logos.py            # all three sports
    .venv/bin/python tools/fetch_logos.py --sport nfl --size 16

Run once -- deploy/install-pi.sh does it for you. The logos are the leagues'
artwork, so they are downloaded rather than shipped in the repo; after this
one run the panel never touches the network for artwork and works offline.

Everything expensive happens HERE rather than at runtime: fetching, cropping,
and the Lanczos downscale from a 500px source. A Pi Zero rendering at 30fps has
no business resampling images, and the result is identical every time anyway.

Output is assets/logos/<sport>/<size>/<KEY>.png, keyed by the abbreviation the
app already uses for that team, so a lookup is a dict hit. Several sizes are
kept because the panel needs different ones: the score bug has room for 16px
(12 for college, where the rank column takes the space), while the pre-game
matchup screen has most of the panel to itself and can afford 28.

One file per team rather than a packed atlas on purpose: at a few hundred bytes
each the size is irrelevant, and it means a logo that shrinks badly can be
redrawn by hand in any pixel editor and dropped straight in.

Two source quirks, both verified rather than assumed:

  * ESPN's abbreviations are NOT this project's everywhere. Baseball differs on
    exactly two -- statsapi says AZ and CWS where ESPN says ARI and CHW -- so
    those are aliased. NFL matches on all 32. Diffed, not guessed: this is the
    same class of bug as the ARI/AZ favourite that silently never matched.
  * College is keyed by ESPN's numeric team **id**, not an abbreviation. There
    are ~270 Division I teams once FCS opponents are counted, abbreviations
    collide across the divisions, and the scoreboard hands the id over anyway.
    Keying by id removes the whole class of "the logo silently didn't match".
  * FCS teams are fetched too (group 81). They are not in cfb_meta, which only
    tracks the 138 FBS members for the conference picker -- but half of a
    September Saturday is FBS-vs-FCS, so leaving them out meant fifty teams a
    week drawing a plain colour block where a logo should be.

Some logos are black, and a black logo flattened onto a black panel is not dim,
it is GONE -- Iowa and Wake Forest both came out completely empty. ESPN
publishes a `500-dark` variant intended for dark backgrounds, so a logo that
renders almost nothing is refetched from there. That is the team's own artwork
for this exact situation, not a brightness correction, so it doesn't repeat the
mistake of turning Yankee navy into royal blue.
"""

import argparse
import re
import concurrent.futures as cf
import io
import os
import sys

import requests
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sources.teams import teams as team_list          # noqa: E402
from app.sources.cfb_meta import CONFERENCE_TEAMS          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "assets", "logos")

ESPN = "https://a.espncdn.com/i/teamlogos/{path}/{variant}/{code}.png"

# When to prefer ESPN's dark-background variant over the standard logo.
#
# Two triggers, because there are two failure modes on a black panel:
#
#   MIN_INK    the standard logo is essentially gone -- Iowa and Wake Forest are
#              solid black and render zero pixels.
#   DARK_GAIN  the standard logo SURVIVES but is hollow, because black detail
#              inside it vanishes. Ohio State is the case: the wreath shows and
#              the black "OHIO STATE" lettering through the middle does not, so
#              it reads as a broken ring. Its dark variant recovers 1.55x the
#              ink, with the lettering in white.
#
# The gain threshold matters. A blanket "always use dark" would also repaint
# Tennessee's orange T and Texas A&M's maroon ATM in white -- their dark
# variants carry the same ink (ratio 1.00 and 1.03), just recoloured, which is
# the bright-but-wrong trade this project already refused for team colours. At
# 1.4 exactly seven teams swap beyond the blank ones, and every one was checked
# by eye: Ohio State, New Mexico State, San Diego State, Arkansas State,
# Southern Miss, Eastern Washington and the Colorado Rockies.
MIN_INK = 0.10
DARK_GAIN = 1.4

# Drop your own artwork here to override a team entirely -- assets/logos/
# _custom/<sport>/<key>.png, where <key> is the filename this tool would write
# (a team id for college, an abbreviation for NFL/MLB). Useful when ESPN's mark
# is simply the wrong choice at 28 pixels and a block letter or mascot reads
# better. Any size and transparency is fine; it goes through the same crop,
# scale and flatten as a downloaded logo, and it is never overwritten.
CUSTOM_DIR = os.path.join(OUT_DIR, "_custom")

FCS_TEAMS = ("https://sports.core.api.espn.com/v2/sports/football/leagues/"
             "college-football/seasons/{year}/types/2/groups/81/teams?limit=300")

# This project's abbreviation -> ESPN's, where they disagree.
ALIASES = {
    "mlb": {"AZ": "ari", "CWS": "chw"},
    "nfl": {},
}

# What each sport needs. The first is the score bug's size, the rest are for
# the larger pre-game layouts.
SIZES = {"nfl": (16, 28), "mlb": (16, 28), "cfb": (12, 28)}


def fcs_ids(year=2026):
    """Team ids for the FCS, whose teams fill out half of a September slate.

    The ids are already in the ref URLs, so this is one request rather than one
    per team.
    """
    try:
        items = requests.get(FCS_TEAMS.format(year=year), timeout=25).json()["items"]
    except Exception:
        return []
    return [m.group(1) for m in
            (re.search(r"/teams/(\d+)", i.get("$ref", "")) for i in items) if m]


def sources(sport: str):
    """[(key, espn_code, path)] -- what to fetch and what to call it."""
    if sport == "cfb":
        # Keyed by id, and covering both divisions. See the module docstring.
        ids = [tid for teams in CONFERENCE_TEAMS.values() for tid, _a, _n in teams]
        ids += [i for i in fcs_ids() if i not in set(ids)]
        return [(tid, tid, "ncaa") for tid in ids]
    path = {"nfl": "nfl", "mlb": "mlb"}[sport]
    alias = ALIASES.get(sport, {})
    return [(t["abbrev"].upper(), alias.get(t["abbrev"].upper(), t["abbrev"].lower()), path)
            for t in team_list(sport)]


def shrink(raw: bytes, size: int) -> Image.Image:
    """500px source -> `size` px, on black, without distortion."""
    img = Image.open(io.BytesIO(raw)).convert("RGBA")

    # Crop to the mark itself. ESPN pads these, and padding is pixels we cannot
    # spare -- an uncropped 16px logo can waste a third of its width on nothing.
    box = img.split()[3].getbbox()
    if box:
        img = img.crop(box)

    w, h = img.size
    scale = size / max(w, h)
    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(img, ((size - img.width) // 2, (size - img.height) // 2))

    # Flatten onto black, because that is what the panel is. Semi-transparent
    # antialiased edges have to resolve against the real background or they
    # come out as grey fringing.
    flat = Image.new("RGB", (size, size), (0, 0, 0))
    flat.paste(square, (0, 0), square)

    # Deliberately NOT brightness-corrected. Lifting a dark logo until it is
    # clearly visible also changes what colour it is -- Yankee navy comes back
    # as royal blue -- and a logo that is dim but right beats one that is
    # bright but wrong. The abbreviation next to it is what guarantees you can
    # still tell who is playing.
    return flat


def _get(path, code, variant):
    r = requests.get(ESPN.format(path=path, code=code, variant=variant), timeout=25)
    return r.content if r.status_code == 200 and r.content else None


def _ink(img) -> float:
    px = img.load()
    lit = sum(1 for y in range(img.height) for x in range(img.width) if any(px[x, y]))
    return lit / float(img.width * img.height)


def custom_for(sport, *keys):
    """Your own artwork for a team, if you've dropped some in. See CUSTOM_DIR."""
    for key in keys:
        if not key:
            continue
        for ext in (".png", ".PNG"):
            path = os.path.join(CUSTOM_DIR, sport, f"{key}{ext}")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    return path, f.read()
    return None, None


def fetch_raw(job, sport=None):
    """The source artwork for one team, or a string explaining why not.

    Your own file wins outright. Otherwise the standard logo is measured
    against the dark-background variant and the better one on black is kept --
    see MIN_INK and DARK_GAIN.
    """
    key, code, path = job
    try:
        where, blob = custom_for(sport, key, code)
        if blob is not None:
            return key, blob

        blob = _get(path, code, "500")
        if blob is None:
            return key, "HTTP error on the standard logo"

        # The dark variant is fetched for every team rather than only for the
        # ones that look broken, because "hollow" can't be spotted from the
        # standard logo alone -- it needs the comparison. One extra request per
        # team, in a tool that runs about once a year.
        plain = _ink(shrink(blob, 28))
        dark = _get(path, code, "500-dark")
        if dark is not None:
            lit = _ink(shrink(dark, 28))
            rescues_a_blank = plain < MIN_INK and lit > plain
            fills_a_hollow = plain > 0 and lit / plain >= DARK_GAIN
            if rescues_a_blank or fills_a_hollow:
                return key, dark
        return key, blob
    except Exception as e:
        return key, f"{type(e).__name__}: {e}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sport", choices=("nfl", "mlb", "cfb"), action="append")
    p.add_argument("--size", type=int, action="append",
                   help="override the sizes for this sport (repeatable)")
    args = p.parse_args()
    sports = args.sport or ["nfl", "mlb", "cfb"]

    for sport in sports:
        jobs = sources(sport)
        # Fetch the 500px source ONCE per team and shrink it to every size,
        # rather than downloading the same artwork two or three times.
        raw = {}
        with cf.ThreadPoolExecutor(10) as pool:
            for key, result in pool.map(lambda j: fetch_raw(j, sport), jobs):
                raw[key] = result

        for size in (args.size or SIZES.get(sport, (16,))):
            dest = os.path.join(OUT_DIR, sport, str(size))
            os.makedirs(dest, exist_ok=True)
            ok, bad = 0, []
            for key, blob in raw.items():
                if isinstance(blob, str):
                    bad.append((key, blob))
                    continue
                try:
                    shrink(blob, size).save(os.path.join(dest, f"{key}.png"))
                    ok += 1
                except Exception as e:
                    bad.append((key, f"{type(e).__name__}: {e}"))
            total = sum(os.path.getsize(os.path.join(dest, f)) for f in os.listdir(dest))
            print(f"{sport} @{size}px: {ok}/{len(jobs)} -> {dest}  ({total/1024:.0f} KB)")
            for key, why in bad:
                print(f"   MISSING {key}: {why}")


if __name__ == "__main__":
    main()
