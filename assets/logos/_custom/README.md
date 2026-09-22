# Your own logos

Drop a PNG here to override a team's artwork completely:

    _custom/<sport>/<key>.png

`<key>` is the filename `tools/fetch_logos.py` writes for that team — a **team
id** for college (Ohio State is `194`), an **abbreviation** for NFL and MLB
(`CLE`, `NYY`). For college you can name the file either the id or the
abbreviation; both are checked.

Any size and transparency works. The file goes through the same crop, scale and
flatten-onto-black as a downloaded logo, so one source image covers every panel
size. It is used instead of downloading, and re-running the fetch tool will
never overwrite it.

Worth doing when ESPN's mark is simply the wrong choice at 28 pixels — a block
letter or a mascot often reads better than a detailed crest.

    .venv/bin/python tools/fetch_logos.py --sport cfb     # picks your file up
