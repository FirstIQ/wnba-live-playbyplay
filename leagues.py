"""
LIVE PLAY BY PLAY -- league settings.

One watcher, two leagues. Everything that differs between the WNBA and the
NBA lives here: which sheet, which ESPN feed, which teams are real, how
ESPN abbreviates them, and where the post-tip window sits.

Which league a process works on is decided once, by the FIRSTIQ_LEAGUE
environment variable -- the same name and the same rules as the main
pipeline (bball-bots' firstiq/leagues/__init__.py):

    FIRSTIQ_LEAGUE=nba python wnba_playbyplay_live.py

  - Unset or empty means "wnba", which is what every run did before this
    file existed.
  - Anything unrecognised STOPS the run. It never falls back: a typo that
    quietly writes one league's plays into the other league's sheet is the
    worst failure this repo can have, and both sheets are production tabs
    the app reads.

The values are copied from bball-bots (firstiq/leagues/wnba.py, nba.py)
rather than imported. This repo is public, that one is private, and the
watcher stays self-contained on purpose -- see wnba_playbyplay_live.py's
module docstring. If a team is added or renamed there, change it here too.
"""

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

ENV_VAR = "FIRSTIQ_LEAGUE"
DEFAULT_LEAGUE = "wnba"


@dataclass(frozen=True)
class League:
    key: str    # what FIRSTIQ_LEAGUE says
    name: str   # what the logs say
    spreadsheet_id: str
    # ESPN spells the league in two places: the site API path
    # (basketball/<x>) and the core API's leagues/<x>. They happen to
    # match for both leagues, but they are different hosts, so they are
    # kept apart.
    sport_path: str
    league_slug: str
    # Keeps All-Star and exhibition games out. Matched against ESPN's
    # displayName by set containment, so a near-miss silently drops every
    # game that team plays.
    real_teams: frozenset
    team_abbr: Mapping[str, str]
    # (low, high) seconds left in Q1 inside which the post-tip intraday
    # dispatch fires: three to nine minutes of elapsed game time, wide
    # enough for a 5-minute poll to land inside it. It is read off the
    # scoreboard clock, which counts DOWN from the quarter length -- so
    # the numbers depend on how long a quarter is.
    post_tip_window: tuple

    def scoreboard_url(self, yyyymmdd):
        return f"https://site.api.espn.com/apis/site/v2/sports/{self.sport_path}/scoreboard?dates={yyyymmdd}"

    def summary_url(self, event_id):
        return f"https://site.api.espn.com/apis/site/v2/sports/{self.sport_path}/summary?event={event_id}"

    def athlete_url(self, athlete_id):
        return (f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/{self.league_slug}"
                f"/athletes/{athlete_id}?lang=en&region=us")


def _league(team_abbr, **settings):
    return League(real_teams=frozenset(team_abbr), team_abbr=MappingProxyType(dict(team_abbr)), **settings)


WNBA = _league(
    key="wnba",
    name="WNBA",
    spreadsheet_id="1cHokxmusavnAfYr0DqJkNSWZ7Eb8_74L5YGAdleGAuQ",
    sport_path="basketball/wnba",
    league_slug="wnba",
    # 10-minute quarters: 60-420 seconds left is 3-9 minutes in.
    post_tip_window=(60, 420),
    team_abbr={
        "Las Vegas Aces": "LVA", "New York Liberty": "NYL", "Connecticut Sun": "CON",
        "Indiana Fever": "IN", "Chicago Sky": "CHI", "Atlanta Dream": "ATL",
        "Washington Mystics": "WAS", "Minnesota Lynx": "MIN", "Phoenix Mercury": "PHX",
        "Los Angeles Sparks": "LA", "Seattle Storm": "SEA", "Dallas Wings": "DAL",
        "Golden State Valkyries": "GSV", "Toronto Tempo": "TOR", "Portland Fire": "POR",
    },
)

NBA = _league(
    key="nba",
    name="NBA",
    # Not written here yet. Both tracker sheets open for anyone who has
    # their ID, and this repo is public, so the NBA sheet's ID waits for
    # the owner's say. Until then it comes from the environment (the
    # NBA_SPREADSHEET_ID secret in live-playbyplay.yml); with neither, an
    # NBA run stops before it fetches or opens anything.
    spreadsheet_id=os.environ.get("NBA_SPREADSHEET_ID", "").strip(),
    sport_path="basketball/nba",
    league_slug="nba",
    # 12-minute quarters: 180-540 seconds left is 3-9 minutes in. The
    # WNBA's numbers here would fire 5-11 minutes in, after most openings
    # are already decided.
    post_tip_window=(180, 540),
    # ESPN's own displayNames ("LA Clippers", not "Los Angeles Clippers")
    # and ESPN's own codes, several of which are not the ones you would
    # guess (GS, NO, NY, SA, UTAH, WSH). bball-bots builds its Game Key
    # from the same table, so a "nicer" code here would break its dedup
    # against rows this watcher wrote.
    team_abbr={
        "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
        "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
        "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
        "Golden State Warriors": "GS", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
        "LA Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
        "Miami Heat": "MIA", "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN",
        "New Orleans Pelicans": "NO", "New York Knicks": "NY",
        "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL",
        "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX",
        "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC",
        "San Antonio Spurs": "SA", "Toronto Raptors": "TOR", "Utah Jazz": "UTAH",
        "Washington Wizards": "WSH",
    },
)

# The order one poll cycle works through them in.
ALL = (WNBA, NBA)
_BY_KEY = {league.key: league for league in ALL}


def resolve(name=None):
    """The league this process works on. `name` overrides the env var."""
    requested = (name if name is not None else os.environ.get(ENV_VAR, "")).strip().lower()
    if not requested:
        requested = DEFAULT_LEAGUE
    if requested not in _BY_KEY:
        raise ValueError(
            f"Unknown {ENV_VAR}={requested!r}. Valid leagues: {sorted(_BY_KEY)}. "
            f"Refusing to fall back to {DEFAULT_LEAGUE!r} -- a typo here would write "
            f"one league's plays into the other league's spreadsheet."
        )
    return _BY_KEY[requested]
