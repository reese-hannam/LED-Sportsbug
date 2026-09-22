"""The seam between data sources and renderers.

Every sport normalizes into Game. Renderers only ever see this shape, so adding
NBA/NHL later means writing one source module and one renderer -- nothing else moves.
"""

from dataclasses import dataclass, field

# Game lifecycle, normalized across sports.
PRE = "pre"
LIVE = "live"
FINAL = "final"


@dataclass
class Team:
    abbrev: str
    score: int = 0
    color: tuple = (255, 255, 255)
    name: str = ""
    record: str = ""


@dataclass
class Game:
    id: str
    sport: str
    away: Team
    home: Team
    state: str = PRE
    # Short period label the renderer can print as-is: "T7", "3RD", "F/10".
    period: str = ""
    # Human status for the control center list: "In Progress", "Final", "7:05 PM".
    status_detail: str = ""
    start_utc: str = ""
    # Sport-specific payload. MLB: balls/strikes/outs/bases/inning_state.
    # NFL: down/distance/possession/yardline/clock.
    detail: dict = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.state == LIVE

    @property
    def matchup(self) -> str:
        return f"{self.away.abbrev} @ {self.home.abbrev}"


class Source:
    """Interface every sport source implements."""

    sport = "unknown"
    # How often the orchestrator should poll this source, in seconds.
    poll_interval = 12.0

    def fetch(self) -> list[Game]:
        raise NotImplementedError


def sort_games(games: list[Game]) -> list[Game]:
    """Live first, then upcoming, then finals -- the order worth rotating through."""
    rank = {LIVE: 0, PRE: 1, FINAL: 2}
    return sorted(games, key=lambda g: (rank.get(g.state, 3), g.start_utc, g.id))


def parse_odds(comp: dict) -> tuple[str, str]:
    """(spread, over/under) for a competition, as short display strings.

    ESPN puts these on the scoreboard payload already -- `odds[0].details` is
    the line as a book would write it ("LAR -3.5", "PK"), and `overUnder` is
    the total. Nothing extra is fetched for this.

    Only meaningful before kickoff: the feed keeps serving the pre-game line
    once a game is underway, where it is stale trivia next to a live score, so
    the renderers only ask for it on a game that hasn't started.

    Both come back "" when absent, which is normal -- odds show up a few days
    out, and never for some games.
    """
    odds = comp.get("odds") or []
    if not odds:
        return ("", "")
    first = odds[0] or {}

    spread = str(first.get("details") or "").strip().upper()

    total = first.get("overUnder")
    if isinstance(total, (int, float)):
        # 48.5 -> "48.5" but 48.0 -> "48": the trailing ".0" is noise on a
        # panel where every pixel of width is contested.
        total = f"{total:g}"
    else:
        total = ""

    return (spread, total)
