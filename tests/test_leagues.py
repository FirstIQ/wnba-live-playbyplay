"""
League settings: which sheet, which ESPN feed, which teams.

WHY: one watcher now writes two production sheets. The setting that picks
between them is one environment variable, and the failure to fear is a
typo that quietly writes one league's plays into the other league's
sheet. So an unknown value has to stop the run, and the WNBA's values
have to be exactly what the script had hardcoded before.

    pip install pytest && python -m pytest tests/ -q
"""

import dataclasses

import pytest

import leagues


class TestResolve:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_unset_or_empty_is_the_wnba(self, monkeypatch, value):
        """What every run did before the setting existed."""
        monkeypatch.delenv(leagues.ENV_VAR, raising=False)
        if value is not None:
            monkeypatch.setenv(leagues.ENV_VAR, value)
        assert leagues.resolve() is leagues.WNBA

    @pytest.mark.parametrize("value", ["nba", " NBA "])
    def test_the_env_var_picks_the_nba(self, monkeypatch, value):
        monkeypatch.setenv(leagues.ENV_VAR, value)
        assert leagues.resolve() is leagues.NBA

    @pytest.mark.parametrize("value", ["nbaa", "wnba,nba", "all", "nfl"])
    def test_a_typo_stops_the_run_instead_of_falling_back(self, monkeypatch, value):
        monkeypatch.setenv(leagues.ENV_VAR, value)
        with pytest.raises(ValueError, match="Unknown FIRSTIQ_LEAGUE"):
            leagues.resolve()

    def test_an_explicit_name_beats_the_env_var(self, monkeypatch):
        monkeypatch.setenv(leagues.ENV_VAR, "nba")
        assert leagues.resolve("wnba") is leagues.WNBA

    def test_the_cycle_runs_the_wnba_first_then_the_nba(self):
        assert [league.key for league in leagues.ALL] == ["wnba", "nba"]


class TestWnbaIsUnchanged:
    """Literal copies of what wnba_playbyplay_live.py hardcoded."""

    def test_sheet_and_feeds(self):
        wnba = leagues.WNBA
        assert wnba.spreadsheet_id == "1cHokxmusavnAfYr0DqJkNSWZ7Eb8_74L5YGAdleGAuQ"
        assert wnba.scoreboard_url("20260917") == \
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates=20260917"
        assert wnba.summary_url("401") == \
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event=401"
        assert wnba.athlete_url("a2") == \
            "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/athletes/a2?lang=en&region=us"

    def test_teams(self):
        assert dict(leagues.WNBA.team_abbr) == {
            "Las Vegas Aces": "LVA", "New York Liberty": "NYL", "Connecticut Sun": "CON",
            "Indiana Fever": "IN", "Chicago Sky": "CHI", "Atlanta Dream": "ATL",
            "Washington Mystics": "WAS", "Minnesota Lynx": "MIN", "Phoenix Mercury": "PHX",
            "Los Angeles Sparks": "LA", "Seattle Storm": "SEA", "Dallas Wings": "DAL",
            "Golden State Valkyries": "GSV", "Toronto Tempo": "TOR", "Portland Fire": "POR",
        }
        assert leagues.WNBA.real_teams == set(leagues.WNBA.team_abbr)

    def test_post_tip_window(self):
        assert leagues.WNBA.post_tip_window == (60, 420)


class TestNba:
    def test_feeds(self):
        nba = leagues.NBA
        assert nba.scoreboard_url("20261116") == \
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261116"
        assert nba.summary_url("501") == \
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=501"
        assert nba.athlete_url("3975") == \
            "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/3975?lang=en&region=us"

    def test_all_thirty_teams_under_espns_own_names_and_codes(self):
        nba = leagues.NBA
        assert len(nba.real_teams) == 30
        assert "LA Clippers" in nba.real_teams and "Los Angeles Clippers" not in nba.real_teams
        # ESPN's codes, not the ones you would guess. bball-bots builds its
        # Game Key from the same table, so a "nicer" code breaks its dedup.
        assert {nba.team_abbr[t] for t in (
            "Golden State Warriors", "New Orleans Pelicans", "New York Knicks",
            "San Antonio Spurs", "Utah Jazz", "Washington Wizards")} == {"GS", "NO", "NY", "SA", "UTAH", "WSH"}

    def test_the_sheet_is_not_the_wnba_sheet(self):
        assert leagues.NBA.spreadsheet_id != leagues.WNBA.spreadsheet_id


@pytest.mark.parametrize("league", leagues.ALL, ids=lambda league: league.key)
class TestEveryLeague:
    def test_every_real_team_has_a_code(self, league):
        assert set(league.team_abbr) == set(league.real_teams)

    def test_the_post_tip_window_is_three_to_nine_minutes_into_the_first_quarter(self, league):
        quarter = {"wnba": 600, "nba": 720}[league.key]
        assert league.post_tip_window == (quarter - 540, quarter - 180)

    def test_settings_cannot_be_changed_mid_run(self, league):
        with pytest.raises(dataclasses.FrozenInstanceError):
            league.spreadsheet_id = "another-sheet"
        with pytest.raises(TypeError):
            league.team_abbr["Some Team"] = "XXX"
        with pytest.raises(AttributeError):
            league.real_teams.add("Some Team")


def test_no_team_plays_in_both_leagues():
    """The team filter is the last guard against one league's game landing
    in the other league's sheet."""
    assert not (leagues.WNBA.real_teams & leagues.NBA.real_teams)
