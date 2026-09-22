"""Fantasy rosters: whose plays are worth interrupting the panel for.

Deliberately simple. A team is a name and a list of players; a player is an ESPN
athlete id, a position, and a starting toggle. Only starters are tracked -- a
bench player doing something is not news.

The one structural decision worth explaining is the lookup direction. Alerts are
driven by plays, and a play names athlete ids, so the hot path is
"athlete id -> which of my teams start this guy". That's the reverse of how the
data is entered (team -> roster), so `tracked()` inverts it into a dict rebuilt
only when the rosters change. A player started on three teams produces ONE entry
with three team names, which is what makes the combined alert
("Yung Gunz + Team B — G.Pickens — 12-yard reception") fall out naturally
instead of firing three times.

Defenses are stored as a player whose `athlete_id` is `DEF:<TEAM>` rather than a
real athlete id, since ESPN has no athlete to point at for a team defense. The
play matcher treats that prefix specially.
"""

import time
import uuid
from dataclasses import dataclass, field, asdict

# Roster slots, in the order a fantasy site would show them.
POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]

DEF_PREFIX = "DEF:"


@dataclass
class FantasyPlayer:
    athlete_id: str
    name: str
    position: str
    team: str = ""            # NFL team abbrev, for display and DEF matching
    starting: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def is_defense(self) -> bool:
        return str(self.athlete_id).startswith(DEF_PREFIX)


@dataclass
class FantasyTeam:
    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)


class FantasyBook:
    """Every fantasy team and its roster. Persistence is AppState's job."""

    def __init__(self):
        self.teams: list[FantasyTeam] = []
        self.players: list[FantasyPlayer] = []
        # player.id -> team.id. Kept separate from the player so the same
        # athlete can sit on several rosters as distinct entries.
        self._team_of: dict[str, str] = {}

    # -- mutation -----------------------------------------------------------

    def add_team(self, name: str) -> FantasyTeam:
        team = FantasyTeam(name=(name or "My Team").strip()[:24])
        self.teams.append(team)
        return team

    def remove_team(self, team_id: str) -> None:
        self.teams = [t for t in self.teams if t.id != team_id]
        drop = {p.id for p in self.players if self._team_of.get(p.id) == team_id}
        self.players = [p for p in self.players if p.id not in drop]
        for pid in drop:
            self._team_of.pop(pid, None)

    def rename_team(self, team_id: str, name: str) -> None:
        for t in self.teams:
            if t.id == team_id:
                t.name = (name or t.name).strip()[:24]

    def add_player(self, team_id: str, player: FantasyPlayer) -> FantasyPlayer | None:
        if not any(t.id == team_id for t in self.teams):
            return None
        # Same athlete twice on one roster is a mistake, not a feature.
        for p in self.roster_of(team_id):
            if str(p.athlete_id) == str(player.athlete_id):
                return None
        self.players.append(player)
        self._team_of[player.id] = team_id
        return player

    def remove_player(self, player_id: str) -> bool:
        before = len(self.players)
        self.players = [p for p in self.players if p.id != player_id]
        self._team_of.pop(player_id, None)
        return len(self.players) != before

    def toggle_starting(self, player_id: str) -> bool | None:
        for p in self.players:
            if p.id == player_id:
                p.starting = not p.starting
                return p.starting
        return None

    def clear(self) -> None:
        self.teams.clear()
        self.players.clear()
        self._team_of.clear()

    # -- queries ------------------------------------------------------------

    def team_of(self, player_id: str) -> FantasyTeam | None:
        tid = self._team_of.get(player_id)
        return next((t for t in self.teams if t.id == tid), None)

    def roster_of(self, team_id: str) -> list[FantasyPlayer]:
        return [p for p in self.players if self._team_of.get(p.id) == team_id]

    def grouped_roster(self, team_id: str) -> list[tuple[str, list[FantasyPlayer]]]:
        """Roster split by position, in POSITIONS order, for the UI."""
        roster = self.roster_of(team_id)
        out = []
        for pos in POSITIONS:
            group = [p for p in roster if p.position == pos]
            if group:
                out.append((pos, group))
        # Anything with an unexpected position still needs somewhere to live.
        rest = [p for p in roster if p.position not in POSITIONS]
        if rest:
            out.append(("OTHER", rest))
        return out

    def tracked(self) -> dict[str, dict]:
        """athlete_id -> {"name", "position", "teams": [team names]}.

        Starters only, and merged across fantasy teams so a player started in
        three leagues yields one entry naming all three.
        """
        out: dict[str, dict] = {}
        for p in self.players:
            if not p.starting:
                continue
            team = self.team_of(p.id)
            key = str(p.athlete_id)
            entry = out.setdefault(key, {
                "name": p.name, "position": p.position,
                "nfl_team": p.team, "teams": [],
            })
            if team and team.name not in entry["teams"]:
                entry["teams"].append(team.name)
        return out

    @property
    def starters(self) -> list[FantasyPlayer]:
        return [p for p in self.players if p.starting]

    # -- serialization ------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "fantasy_teams": [asdict(t) for t in self.teams],
            "fantasy_players": [
                {**asdict(p), "team_id": self._team_of.get(p.id, "")}
                for p in self.players
            ],
        }

    def load(self, data: dict) -> None:
        self.clear()
        for raw in (data or {}).get("fantasy_teams", []) or []:
            try:
                self.teams.append(FantasyTeam(**raw))
            except (TypeError, ValueError):
                continue  # a config from another version shouldn't stop boot
        for raw in (data or {}).get("fantasy_players", []) or []:
            try:
                raw = dict(raw)
                team_id = raw.pop("team_id", "")
                player = FantasyPlayer(**raw)
            except (TypeError, ValueError):
                continue
            if any(t.id == team_id for t in self.teams):
                self.players.append(player)
                self._team_of[player.id] = team_id
