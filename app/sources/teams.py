"""Static roster tables for MLB and NFL teams, keyed by the same abbreviation
used in colors.py, for rendering a "pick your favorite teams" grid.
"""

MLB_TEAMS = [
    # 'AZ', not 'ARI' -- these abbrevs are matched against what MLBSource emits,
    # and statsapi always says AZ. colors.py aliases both.
    {"abbrev": "AZ", "name": "D-backs", "location": "Phoenix", "group": "National League West"},
    {"abbrev": "ATH", "name": "Athletics", "location": "Sacramento", "group": "American League West"},
    {"abbrev": "ATL", "name": "Braves", "location": "Atlanta", "group": "National League East"},
    {"abbrev": "BAL", "name": "Orioles", "location": "Baltimore", "group": "American League East"},
    {"abbrev": "BOS", "name": "Red Sox", "location": "Boston", "group": "American League East"},
    {"abbrev": "CHC", "name": "Cubs", "location": "Chicago", "group": "National League Central"},
    {"abbrev": "CIN", "name": "Reds", "location": "Cincinnati", "group": "National League Central"},
    {"abbrev": "CLE", "name": "Guardians", "location": "Cleveland", "group": "American League Central"},
    {"abbrev": "COL", "name": "Rockies", "location": "Denver", "group": "National League West"},
    {"abbrev": "CWS", "name": "White Sox", "location": "Chicago", "group": "American League Central"},
    {"abbrev": "DET", "name": "Tigers", "location": "Detroit", "group": "American League Central"},
    {"abbrev": "HOU", "name": "Astros", "location": "Houston", "group": "American League West"},
    {"abbrev": "KC", "name": "Royals", "location": "Kansas City", "group": "American League Central"},
    {"abbrev": "LAA", "name": "Angels", "location": "Anaheim", "group": "American League West"},
    {"abbrev": "LAD", "name": "Dodgers", "location": "Los Angeles", "group": "National League West"},
    {"abbrev": "MIA", "name": "Marlins", "location": "Miami", "group": "National League East"},
    {"abbrev": "MIL", "name": "Brewers", "location": "Milwaukee", "group": "National League Central"},
    {"abbrev": "MIN", "name": "Twins", "location": "Minneapolis", "group": "American League Central"},
    {"abbrev": "NYM", "name": "Mets", "location": "Flushing", "group": "National League East"},
    {"abbrev": "NYY", "name": "Yankees", "location": "Bronx", "group": "American League East"},
    {"abbrev": "PHI", "name": "Phillies", "location": "Philadelphia", "group": "National League East"},
    {"abbrev": "PIT", "name": "Pirates", "location": "Pittsburgh", "group": "National League Central"},
    {"abbrev": "SD", "name": "Padres", "location": "San Diego", "group": "National League West"},
    {"abbrev": "SEA", "name": "Mariners", "location": "Seattle", "group": "American League West"},
    {"abbrev": "SF", "name": "Giants", "location": "San Francisco", "group": "National League West"},
    {"abbrev": "STL", "name": "Cardinals", "location": "St. Louis", "group": "National League Central"},
    {"abbrev": "TB", "name": "Rays", "location": "St. Petersburg", "group": "American League East"},
    {"abbrev": "TEX", "name": "Rangers", "location": "Arlington", "group": "American League West"},
    {"abbrev": "TOR", "name": "Blue Jays", "location": "Toronto", "group": "American League East"},
    {"abbrev": "WSH", "name": "Nationals", "location": "Washington", "group": "National League East"},
]

NFL_TEAMS = [
    {"abbrev": "ARI", "name": "Cardinals", "location": "Arizona", "group": "NFC West"},
    {"abbrev": "ATL", "name": "Falcons", "location": "Atlanta", "group": "NFC South"},
    {"abbrev": "BAL", "name": "Ravens", "location": "Baltimore", "group": "AFC North"},
    {"abbrev": "BUF", "name": "Bills", "location": "Buffalo", "group": "AFC East"},
    {"abbrev": "CAR", "name": "Panthers", "location": "Carolina", "group": "NFC South"},
    {"abbrev": "CHI", "name": "Bears", "location": "Chicago", "group": "NFC North"},
    {"abbrev": "CIN", "name": "Bengals", "location": "Cincinnati", "group": "AFC North"},
    {"abbrev": "CLE", "name": "Browns", "location": "Cleveland", "group": "AFC North"},
    {"abbrev": "DAL", "name": "Cowboys", "location": "Dallas", "group": "NFC East"},
    {"abbrev": "DEN", "name": "Broncos", "location": "Denver", "group": "AFC West"},
    {"abbrev": "DET", "name": "Lions", "location": "Detroit", "group": "NFC North"},
    {"abbrev": "GB", "name": "Packers", "location": "Green Bay", "group": "NFC North"},
    {"abbrev": "HOU", "name": "Texans", "location": "Houston", "group": "AFC South"},
    {"abbrev": "IND", "name": "Colts", "location": "Indianapolis", "group": "AFC South"},
    {"abbrev": "JAX", "name": "Jaguars", "location": "Jacksonville", "group": "AFC South"},
    {"abbrev": "KC", "name": "Chiefs", "location": "Kansas City", "group": "AFC West"},
    {"abbrev": "LAC", "name": "Chargers", "location": "Los Angeles", "group": "AFC West"},
    {"abbrev": "LAR", "name": "Rams", "location": "Los Angeles", "group": "NFC West"},
    {"abbrev": "LV", "name": "Raiders", "location": "Las Vegas", "group": "AFC West"},
    {"abbrev": "MIA", "name": "Dolphins", "location": "Miami", "group": "AFC East"},
    {"abbrev": "MIN", "name": "Vikings", "location": "Minnesota", "group": "NFC North"},
    {"abbrev": "NE", "name": "Patriots", "location": "New England", "group": "AFC East"},
    {"abbrev": "NO", "name": "Saints", "location": "New Orleans", "group": "NFC South"},
    {"abbrev": "NYG", "name": "Giants", "location": "New York", "group": "NFC East"},
    {"abbrev": "NYJ", "name": "Jets", "location": "New York", "group": "AFC East"},
    {"abbrev": "PHI", "name": "Eagles", "location": "Philadelphia", "group": "NFC East"},
    {"abbrev": "PIT", "name": "Steelers", "location": "Pittsburgh", "group": "AFC North"},
    {"abbrev": "SEA", "name": "Seahawks", "location": "Seattle", "group": "NFC West"},
    {"abbrev": "SF", "name": "49ers", "location": "San Francisco", "group": "NFC West"},
    {"abbrev": "TB", "name": "Buccaneers", "location": "Tampa Bay", "group": "NFC South"},
    {"abbrev": "TEN", "name": "Titans", "location": "Tennessee", "group": "AFC South"},
    {"abbrev": "WSH", "name": "Commanders", "location": "Washington", "group": "NFC East"},
]

_BY_SPORT = {"mlb": MLB_TEAMS, "nfl": NFL_TEAMS}


def teams(sport: str) -> list[dict]:
    return _BY_SPORT.get(sport, [])


def team_name(sport: str, abbrev: str) -> str:
    abbrev = abbrev.upper()
    for team in teams(sport):
        if team["abbrev"] == abbrev:
            return f'{team["location"]} {team["name"]}'
    return abbrev
