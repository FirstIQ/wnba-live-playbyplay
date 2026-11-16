"""
Three things the NBA's volume turns from rare into nightly.

WHY: found by the review of the two-league change (November 16, 2026).

1. The NBA tips many games at once. The post-tip dispatch fired once per
   GAME, so five 7:00 tips started five identical intraday runs in the
   pipeline within seconds, each rewriting the same tabs and each spending
   odds credits. It now fires once per POLL.
2. The rewrite clears the tab and then writes rows 2..n. Sheets refuses a
   write past the tab's grid instead of growing it, so the first rewrite
   that outgrew the grid would clear the tab and then fail, and the next
   poll would take the empty tab as the truth. The tab is grown first.
3. A real run with no dispatch token must dispatch nothing.

    pip install pytest && python -m pytest tests/ -q
"""

import dataclasses

import pytest

import leagues
import wnba_playbyplay_live as live
from harness import GridLimitExceeded, STAMP, TODAY, World, event, play, summary, team
from test_live_main import wnba_world

BOS = team("2", "Boston Celtics", "BOS")
NYK = team("18", "New York Knicks", "NY")
CHI = team("4", "Chicago Bulls", "CHI")
CLE = team("5", "Cleveland Cavaliers", "CLE")
MEM = team("29", "Memphis Grizzlies", "MEM")
WSH = team("27", "Washington Wizards", "WSH")

NBA_TEST_SHEET = "nba-test-sheet-0123456789abcdefABCDEF"
INTRADAY = "https://api.github.com/repos/FirstIQ/bball-bots/actions/workflows/intraday-updates.yml/dispatches"
GRADING = "https://api.github.com/repos/FirstIQ/bball-bots/actions/workflows/live-grading.yml/dispatches"


@pytest.fixture
def nba(monkeypatch):
    monkeypatch.setattr(live, "LEAGUE", dataclasses.replace(leagues.NBA, spreadsheet_id=NBA_TEST_SHEET))
    live._athlete_cache.clear()


def three_seven_oclock_tips():
    """Three NBA games that tipped together, all 8:30 into... 8:30 LEFT in Q1,
    none with a basket yet."""
    games = {"601": (BOS, NYK), "602": (CHI, CLE), "603": (MEM, WSH)}
    events = [event(eid, away, home, "in", period=1, clock="8:30") for eid, (away, home) in games.items()]
    summaries = {eid: summary(away, home, [play(1, "12:00", home, "j", "Jump ball", kind="Jumpball")])
                 for eid, (away, home) in games.items()}
    return World({"Play By Play": [live.PBP_HEADER]}, events, summaries, {})


class TestPostTipDispatch:
    def test_games_that_tip_together_send_one_dispatch_not_one_each(self, monkeypatch, nba):
        monkeypatch.setenv("MAIN_PIPELINE_DISPATCH_TOKEN", "test-token")
        world = three_seven_oclock_tips().run(monkeypatch)

        assert world.dispatched == [INTRADAY]
        assert world.tab("Post-Tip Dispatch Log") == [
            live.DISPATCH_LOG_HEADER,
            ["601", TODAY, "BOS @ NY", STAMP], ["602", TODAY, "CHI @ CLE", STAMP], ["603", TODAY, "MEM @ WSH", STAMP],
        ]

    def test_none_of_them_is_dispatched_again_on_the_next_poll(self, monkeypatch, nba):
        monkeypatch.setenv("MAIN_PIPELINE_DISPATCH_TOKEN", "test-token")
        world = three_seven_oclock_tips().run(monkeypatch)
        world.run(monkeypatch)
        assert world.dispatched == [INTRADAY]

    def test_a_failed_dispatch_is_not_logged_so_the_next_poll_retries(self, monkeypatch, nba):
        monkeypatch.setenv("MAIN_PIPELINE_DISPATCH_TOKEN", "test-token")
        world = three_seven_oclock_tips()
        monkeypatch.setattr(live, "trigger_main_pipeline_workflow", lambda token, workflow: False)
        world.run(monkeypatch)
        assert world.tab("Post-Tip Dispatch Log") == [live.DISPATCH_LOG_HEADER]

    def test_a_real_run_with_no_token_dispatches_nothing_and_logs_nothing(self, monkeypatch):
        world = wnba_world().run(monkeypatch)

        assert world.dispatched == []
        assert world.tab("Post-Tip Dispatch Log") == [live.DISPATCH_LOG_HEADER]
        assert world.tab("Live Grading Dispatch Log") == [live.DISPATCH_LOG_HEADER]
        # The plays and first baskets are still written.
        assert [r[0] for r in world.tab("Play By Play")[1:]].count("401") == 4
        assert len(world.tab("Live First Basket")) == 4


class TestTheTabsGrid:
    """wnba_world's rewrite writes 11 data rows: 12 with the header."""

    def test_a_rewrite_that_fits_does_not_touch_the_grid(self, monkeypatch):
        world = wnba_world()
        world.spreadsheet.tabs["Play By Play"].row_count = 12
        world.run(monkeypatch)
        assert not any(op.startswith("resize") for op in world.spreadsheet.tabs["Play By Play"].ops)

    def test_a_rewrite_past_the_grid_grows_the_tab_before_it_clears(self, monkeypatch):
        world = wnba_world()
        tab = world.spreadsheet.tabs["Play By Play"]
        tab.row_count = 11  # one row short

        world.run(monkeypatch)

        assert tab.writes == ["resize:5012", "clear", "update:A1", "update:A2:K12"]
        assert len(tab.values) == 12

    def test_the_fake_sheet_really_does_refuse_a_write_past_its_grid(self):
        """Or the test above would pass with the growing removed."""
        world = wnba_world()
        tab = world.spreadsheet.tabs["Play By Play"]
        tab.row_count = 11
        with pytest.raises(GridLimitExceeded):
            tab.update(range_name="A2:K12", values=[[""]] * 11)

    def test_if_the_tab_cannot_be_grown_nothing_is_cleared(self, monkeypatch):
        world = wnba_world()
        tab = world.spreadsheet.tabs["Play By Play"]
        tab.row_count = 11
        tab.fail_on = "resize"
        before = [list(r) for r in tab.values]

        with pytest.raises(RuntimeError, match="resize failed"):
            world.run(monkeypatch)

        assert tab.values == before and "clear" not in tab.ops

    def test_a_dry_run_says_it_would_grow_the_tab_and_does_not(self, monkeypatch, capsys):
        monkeypatch.setenv("FIRSTIQ_DRY_RUN", "1")
        world = wnba_world()
        tab = world.spreadsheet.tabs["Play By Play"]
        tab.row_count = 11

        world.run(monkeypatch)

        assert "would first grow 'Play By Play' from 11 to 5012 rows" in capsys.readouterr().out
        assert tab.writes == [] and tab.row_count == 11


def test_a_rewrite_that_fails_part_way_says_so_and_for_how_long(monkeypatch, capsys):
    """The case the timing line exists for: the tab was cleared and the rows
    did not land."""
    world = wnba_world()
    world.spreadsheet.tabs["Play By Play"].fail_on = "update:A2"

    with pytest.raises(RuntimeError):
        world.run(monkeypatch)

    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("Play By Play rewrite [wnba]"))
    assert "FAILED" in line and "empty or partial" in line


class TestOnlyTheLaterLeagueIsRefusedASharedSheet:
    def test_a_wrong_nba_id_never_stops_the_wnba(self, monkeypatch):
        wrong = dataclasses.replace(leagues.NBA, spreadsheet_id=leagues.WNBA.spreadsheet_id)
        monkeypatch.setattr(leagues, "ALL", (leagues.WNBA, wrong))
        assert leagues.sheet_problem(leagues.WNBA) is None
        assert leagues.sheet_problem(wrong) == "its sheet ID is another league's sheet"


def test_the_script_runs_through_the_wrapper_that_keeps_ids_out_of_a_crash():
    source = open(live.__file__, encoding="utf-8").read()
    assert source.rstrip().endswith('if __name__ == "__main__":\n    run()')


def test_an_nba_dry_run_never_prints_its_sheet_id(monkeypatch, nba, capsys):
    monkeypatch.setenv("FIRSTIQ_DRY_RUN", "1")
    three_seven_oclock_tips().run(monkeypatch)
    out = capsys.readouterr()
    assert "DRY RUN" in out.out and NBA_TEST_SHEET not in out.out + out.err
