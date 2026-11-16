"""
The same watcher, pointed at the NBA.

WHY: the script was WNBA-only in five places (sheet, ESPN path, team
filter, team codes, athlete lookup) plus one hidden one: the post-tip
window assumed 10-minute quarters. Each gets an NBA fixture here, and one
whole NBA poll runs end to end.

    pip install pytest && python -m pytest tests/ -q
"""

import dataclasses
from datetime import datetime, timezone
from unittest import mock

import pytest

import leagues
import wnba_playbyplay_gate as gate
import wnba_playbyplay_live as live
from harness import STAMP, TODAY, World, event, play, summary, team

BOS = team("2", "Boston Celtics", "BOS")
NYK = team("18", "New York Knicks", "NY")
GSW = team("9", "Golden State Warriors", "GS")
UTA = team("26", "Utah Jazz", "UTAH")
DREAM = team("20", "Atlanta Dream", "ATL")
SUN = team("18", "Connecticut Sun", "CONN")
STAR_A = team("96", "Team Stars", "STR")
STAR_B = team("97", "Team World", "WLD")

NOW = datetime(2026, 11, 17, 0, 30, tzinfo=timezone.utc)

# The stand-in sheet ID: a test never needs the real one.
NBA = dataclasses.replace(leagues.NBA, spreadsheet_id="nba-test-sheet")


@pytest.fixture
def nba(monkeypatch):
    monkeypatch.setattr(live, "LEAGUE", NBA)
    live._athlete_cache.clear()
    return NBA


def use(monkeypatch, league):
    monkeypatch.setattr(live, "LEAGUE", league)
    live._athlete_cache.clear()


class TestPostTipWindow:
    """Three to nine minutes into Q1, read off the scoreboard clock.
    A WNBA quarter is 10 minutes; an NBA quarter is 12."""

    @pytest.mark.parametrize("league, clock, inside", [
        (leagues.WNBA, "7:01", False), (leagues.WNBA, "7:00", True),
        (leagues.WNBA, "1:00", True), (leagues.WNBA, "59.9", False),
        (NBA, "9:01", False), (NBA, "9:00", True),
        (NBA, "3:00", True), (NBA, "2:59", False),
        # The same clock, opposite answers: 3.5 minutes into an NBA game
        # is 1.5 minutes into a WNBA one.
        (NBA, "8:30", True), (leagues.WNBA, "8:30", False),
        # And the old WNBA numbers would have fired 10 minutes into an NBA game.
        (NBA, "2:00", False), (leagues.WNBA, "2:00", True),
    ])
    def test_window(self, monkeypatch, league, clock, inside):
        use(monkeypatch, league)
        game = event("1", BOS, NYK, "in", period=1, clock=clock)
        assert live.is_a_few_minutes_into_first_quarter(game) is inside

    def test_only_the_first_quarter_counts(self, nba):
        assert not live.is_a_few_minutes_into_first_quarter(event("1", BOS, NYK, "in", period=2, clock="6:00"))


class TestTeamFilter:
    def test_an_nba_game_is_relevant_to_the_nba(self):
        assert gate._game_is_relevant_now(event("1", BOS, NYK, "in"), NOW, NBA)

    def test_a_wnba_game_is_never_relevant_to_the_nba(self):
        assert not gate._game_is_relevant_now(event("1", SUN, DREAM, "in"), NOW, NBA)

    def test_an_nba_game_is_never_relevant_to_the_wnba(self):
        assert not gate._game_is_relevant_now(event("1", BOS, NYK, "in"), NOW, leagues.WNBA)

    def test_the_all_star_game_is_left_out(self):
        assert not gate._game_is_relevant_now(event("1", STAR_A, STAR_B, "in"), NOW, NBA)

    def test_team_codes_are_espns(self, nba):
        assert live.get_abbr("Golden State Warriors", "GSW") == "GS"
        assert live.get_abbr("A Team Not In The Table", "ESPN") == "ESPN"


class TestAthleteLookup:
    def athlete(self, name):
        response = mock.Mock()
        response.json = lambda: {"fullName": name, "position": {"abbreviation": "G"}}
        return response

    def test_the_nba_lookup_asks_the_nba(self, monkeypatch, nba):
        get = mock.Mock(return_value=self.athlete("Jalen Brunson"))
        monkeypatch.setattr(live, "_get", get)
        assert live.get_player("3934672") == {"name": "Jalen Brunson", "position": "G"}
        get.assert_called_once_with(
            "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/3934672?lang=en&region=us")

    def test_one_leagues_athlete_is_never_served_for_the_other(self, monkeypatch):
        """ESPN numbers each league's athletes separately, so the same ID
        is two different people."""
        monkeypatch.setattr(live, "_get", mock.Mock(return_value=self.athlete("A WNBA Guard")))
        use(monkeypatch, leagues.WNBA)
        assert live.get_player("77")["name"] == "A WNBA Guard"

        monkeypatch.setattr(live, "LEAGUE", NBA)  # the cache is deliberately NOT cleared
        monkeypatch.setattr(live, "_get", mock.Mock(return_value=self.athlete("An NBA Guard")))
        assert live.get_player("77")["name"] == "An NBA Guard"


def nba_world(**kwargs):
    """An NBA evening. The sheet has the tab bball-bots fills each morning
    and none of the three tabs only this watcher writes.

    501  BOS @ NY, live, 8:30 left in Q1: both teams have scored.
    502  a WNBA game on the NBA scoreboard: skipped by the team filter.
    503  the All-Star game: skipped by the team filter.
    504  UTAH @ GS, not tipped: skipped.
    """
    morning_rows = [
        ["490", "2026-11-15", "LAL @ DEN", "1", "11:40", "700", "Denver Nuggets", "0", "2", "morning make", "Y"],
        ["490", "2026-11-15", "LAL @ DEN", "1", "11:12", "672", "Los Angeles Lakers", "3", "2", "morning three", "Y"],
    ]
    tabs = {"Play By Play": [live.PBP_HEADER, *morning_rows]}
    events = [
        event("501", BOS, NYK, "in", period=1, clock="8:30"),
        event("502", SUN, DREAM, "in", period=1, clock="8:30"),
        event("503", STAR_A, STAR_B, "in", period=1, clock="8:30"),
        event("504", UTA, GSW, "pre"),
    ]
    summaries = {"501": summary(BOS, NYK, [
        play(1, "12:00", NYK, "n1", "Jump ball won by New York", kind="Jumpball"),
        play(2, "11:38", NYK, "n2", "Knick Guard makes 26-foot three point jumper", made=True, points=3, home=3),
        play(3, "11:15", BOS, "n3", "Celtic Wing makes two point dunk", made=True, away=2, home=3,
             kind="Dunk Shot"),
        play(4, "10:50", NYK, "n2", "Knick Guard makes jumper", made=True, away=2, home=5),
    ])}
    athletes = {"n2": {"fullName": "Knick Guard", "position": {"abbreviation": "G"}}}
    world = World(tabs, events, summaries, athletes, **kwargs)
    world.morning_rows = morning_rows
    return world


def test_one_nba_poll_end_to_end(monkeypatch, nba):
    monkeypatch.setenv("MAIN_PIPELINE_DISPATCH_TOKEN", "test-token")
    world = nba_world().run(monkeypatch)

    assert world.client.opened == ["nba-test-sheet"]
    assert world.fetched == [
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20260917",
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=501",
        "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/n2?lang=en&region=us",
    ]
    assert world.tab("Play By Play") == [
        live.PBP_HEADER,
        *world.morning_rows,
        ["501", TODAY, "BOS @ NY", 1, "12:00", 720, "New York Knicks", 0, 0, "Jump ball won by New York", "N"],
        ["501", TODAY, "BOS @ NY", 1, "11:38", 698, "New York Knicks", 0, 3,
         "Knick Guard makes 26-foot three point jumper", "Y"],
        ["501", TODAY, "BOS @ NY", 1, "11:15", 675, "Boston Celtics", 2, 3, "Celtic Wing makes two point dunk", "Y"],
    ]
    assert world.tab("Live First Basket") == [
        live.FB_HEADER,
        ["501", TODAY, "BOS @ NY", "Knick Guard", "n2", "New York Knicks", "G", "3-Pointer", 1, "11:38", STAMP],
    ]
    # 8:30 left is inside the NBA's window, so the post-tip refresh fires.
    assert world.dispatched == [
        "https://api.github.com/repos/FirstIQ/bball-bots/actions/workflows/intraday-updates.yml/dispatches",
        "https://api.github.com/repos/FirstIQ/bball-bots/actions/workflows/live-grading.yml/dispatches",
    ]
    assert world.tab("Post-Tip Dispatch Log") == [live.DISPATCH_LOG_HEADER, ["501", TODAY, "BOS @ NY", STAMP]]
    assert world.tab("Live Grading Dispatch Log") == [live.DISPATCH_LOG_HEADER, ["501", TODAY, "BOS @ NY", STAMP]]
    assert sorted(world.spreadsheet.created) == [
        "Live First Basket", "Live Grading Dispatch Log", "Post-Tip Dispatch Log"]


def test_a_league_with_no_sheet_id_refuses_to_run(monkeypatch):
    """Nothing is fetched and no sheet is opened."""
    use(monkeypatch, dataclasses.replace(leagues.NBA, spreadsheet_id=""))
    world = nba_world()
    with pytest.raises(SystemExit, match="no sheet ID"):
        world.run(monkeypatch)
    assert world.client.opened == [] and world.fetched == []


def test_the_rewrite_logs_its_row_counts_and_how_long_it_took(monkeypatch, nba, capsys):
    """Each poll clears and rewrites the whole tab, and the NBA has about
    four times the WNBA's games. The first NBA night is measured here."""
    nba_world().run(monkeypatch)
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("Play By Play rewrite [nba]"))
    assert "wrote 5 data row(s)" in line
    assert "2 kept" in line and "3 for 1 updated game(s)" in line
    assert "read back 5" in line
    import re
    assert re.search(r"empty or partial for \d+\.\d\ds", line) and re.search(r"\d+\.\d\ds in all", line)
