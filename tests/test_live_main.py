"""
One whole poll, end to end, against a fake sheet and a fake ESPN.

WHY: this script writes the production tabs the app reads. Before it
learned a second league (November 2026) nothing pinned what a poll
writes, so "the WNBA is unchanged" was a claim. test_wnba_poll_is_pinned
was written against the WNBA-only code and passed there first; it must
keep passing untouched.

    pip install pytest && python -m pytest tests/ -q
"""

import pytest

import wnba_playbyplay_live as live
from harness import STAMP, TODAY, World, event, play, summary, team

WNBA_SHEET = "1cHokxmusavnAfYr0DqJkNSWZ7Eb8_74L5YGAdleGAuQ"

ATL = team("20", "Atlanta Dream", "ATL")
CON = team("18", "Connecticut Sun", "CONN")
IND = team("5", "Indiana Fever", "IND")
LAS = team("6", "Los Angeles Sparks", "LA")
NYL = team("9", "New York Liberty", "NY")
SEA = team("14", "Seattle Storm", "SEA")
STAR_A = team("96", "Team Clark", "CLK")
STAR_B = team("97", "Team Collier", "COL")

OLD_ROWS = [
    ["399", "2026-09-16", "MIN @ PHX", "1", "9:41", "581", "Phoenix Mercury", "0", "2", "old make", "Y"],
    ["399", "2026-09-16", "MIN @ PHX", "1", "9:02", "542", "Minnesota Lynx", "2", "2", "old answer", "Y"],
]
DONE_ROWS = [
    ["405", TODAY, "SEA @ NYL", "1", "9:30", "570", "New York Liberty", "0", "2", "done make", "Y"],
    ["405", TODAY, "SEA @ NYL", "1", "9:11", "551", "Seattle Storm", "0", "2", "done miss", "N"],
    ["405", TODAY, "SEA @ NYL", "1", "8:48", "528", "Seattle Storm", "3", "2", "done three", "Y"],
]
DONE_FIRST_BASKET = ["405", TODAY, "SEA @ NYL", "Done Scorer", "a5", "New York Liberty", "G",
                     "Layup", "1", "9:30", "2026-09-17 17:20:11"]


def wnba_world(**kwargs):
    """A WNBA evening, mid-slate. Six games on the scoreboard:

    401  CON @ ATL, live, 6:30 left in Q1: both teams have scored.
    402  SEA @ NYL, not tipped: skipped.
    403  an All-Star game: skipped by the team filter.
    404  a preseason game: skipped by the season filter.
    405  SEA @ NYL, final, captured by an earlier poll but never sent to
         grading: not fetched again, graded now.
    406  IND @ LA, live, 9:10 left in Q1: only Indiana has scored.
    """
    tabs = {
        "Play By Play": [live.PBP_HEADER, *OLD_ROWS, *DONE_ROWS],
        "Live First Basket": [live.FB_HEADER, DONE_FIRST_BASKET],
        # No "Post-Tip Dispatch Log" tab: the run has to create it.
        "Live Grading Dispatch Log": [live.DISPATCH_LOG_HEADER],
    }
    events = [
        event("401", CON, ATL, "in", period=1, clock="6:30"),
        event("402", SEA, NYL, "pre"),
        event("403", STAR_A, STAR_B, "in", period=1, clock="6:00"),
        event("404", SEA, ATL, "in", period=1, clock="6:00", slug="preseason"),
        event("405", SEA, NYL, "post", period=4, clock="0.0"),
        event("406", IND, LAS, "in", period=1, clock="9:10"),
    ]
    summaries = {
        # In ESPN's entry order, which is not game order: the 9:20 layup
        # was entered after the 8:50 miss.
        "401": summary(CON, ATL, [
            play(1, "10:00", ATL, "a1", "Jump ball won by Atlanta", kind="Jumpball"),
            play(3, "8:50", CON, "a3", "Con Player misses jumper"),
            play(2, "9:20", ATL, "a2", "Player Two makes driving layup", made=True, home=2,
                 kind="Driving Layup Shot"),
            play(4, "8:10", CON, "a4", "Con Shooter makes 25-foot three", made=True, points=3,
                 away=3, home=2),
            play(5, "7:40", ATL, "a1", "Player One makes jumper", made=True, away=3, home=4),
        ]),
        "406": summary(IND, LAS, [
            play(1, "9:40", LAS, "a8", "Spark misses layup", kind="Layup Shot"),
            play(2, "9:15", IND, "a9", "Fever Guard makes free throw 1 of 2", made=True, points=1,
                 away=1, kind="Free Throw - 1 of 2"),
        ]),
    }
    athletes = {
        "a2": {"fullName": "Player Two", "position": {"abbreviation": "G"}},
        "a9": {"fullName": "Fever Guard", "position": {"abbreviation": "F"}},
    }
    return World(tabs, events, summaries, athletes, **kwargs)


@pytest.fixture
def dispatch_token(monkeypatch):
    monkeypatch.setenv("MAIN_PIPELINE_DISPATCH_TOKEN", "test-token")


def test_wnba_poll_is_pinned(monkeypatch, dispatch_token):
    world = wnba_world().run(monkeypatch)

    # Which sheet, and which ESPN feeds.
    assert world.client.opened == [WNBA_SHEET]
    assert world.fetched == [
        "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates=20260917",
        "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event=401",
        "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/athletes/a2?lang=en&region=us",
        "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event=406",
        "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/athletes/a9?lang=en&region=us",
    ]

    # The opening plays: untouched rows kept, both live games rewritten in
    # game order, 401 cut at the play that put the second team on the board.
    assert world.tab("Play By Play") == [
        live.PBP_HEADER,
        *OLD_ROWS,
        *DONE_ROWS,
        ["401", TODAY, "CON @ ATL", 1, "10:00", 600, "Atlanta Dream", 0, 0, "Jump ball won by Atlanta", "N"],
        ["401", TODAY, "CON @ ATL", 1, "9:20", 560, "Atlanta Dream", 0, 2, "Player Two makes driving layup", "Y"],
        ["401", TODAY, "CON @ ATL", 1, "8:50", 530, "Connecticut Sun", 0, 0, "Con Player misses jumper", "N"],
        ["401", TODAY, "CON @ ATL", 1, "8:10", 490, "Connecticut Sun", 3, 2, "Con Shooter makes 25-foot three", "Y"],
        ["406", TODAY, "IN @ LA", 1, "9:40", 580, "Los Angeles Sparks", 0, 0, "Spark misses layup", "N"],
        ["406", TODAY, "IN @ LA", 1, "9:15", 555, "Indiana Fever", 1, 0, "Fever Guard makes free throw 1 of 2", "Y"],
    ]
    assert world.spreadsheet.tabs["Play By Play"].ops == [
        "get_all_values", "get_all_values", "clear", "update:A1", "update:A2:K12", "get_all_values"]

    # One first-basket row per game, appended once.
    assert world.tab("Live First Basket") == [
        live.FB_HEADER,
        DONE_FIRST_BASKET,
        ["401", TODAY, "CON @ ATL", "Player Two", "a2", "Atlanta Dream", "G", "Layup", 1, "9:20", STAMP],
        ["406", TODAY, "IN @ LA", "Fever Guard", "a9", "Indiana Fever", "F", "Jump Shot", 1, "9:15", STAMP],
    ]
    assert world.spreadsheet.tabs["Live First Basket"].writes == ["append_rows"]

    # Dispatches: the post-tip refresh for the one game inside the window
    # (6:30 left; 9:10 left is too early), then one grading run covering
    # both decided games, sent only after the plays were written.
    assert world.dispatched == [
        "https://api.github.com/repos/FirstIQ/bball-bots/actions/workflows/intraday-updates.yml/dispatches",
        "https://api.github.com/repos/FirstIQ/bball-bots/actions/workflows/live-grading.yml/dispatches",
    ]
    assert world.spreadsheet.created == ["Post-Tip Dispatch Log"]
    assert world.tab("Post-Tip Dispatch Log") == [
        live.DISPATCH_LOG_HEADER, ["401", TODAY, "CON @ ATL", STAMP]]
    assert world.tab("Live Grading Dispatch Log") == [
        live.DISPATCH_LOG_HEADER, ["401", TODAY, "CON @ ATL", STAMP], ["405", TODAY, "SEA @ NYL", STAMP]]


def test_a_second_poll_changes_nothing_it_already_wrote(monkeypatch, dispatch_token):
    """No duplicate rows: the app joins 'Live First Basket' to its slate
    and nothing downstream dedupes it."""
    world = wnba_world().run(monkeypatch)
    first_basket_rows = list(world.tab("Live First Basket"))
    grading_rows = list(world.tab("Live Grading Dispatch Log"))
    posts = len(world.dispatched)

    world.run(monkeypatch)

    assert world.tab("Live First Basket") == first_basket_rows
    assert world.tab("Live Grading Dispatch Log") == grading_rows
    assert len(world.dispatched) == posts
    assert [r[0] for r in world.tab("Play By Play")[1:]].count("401") == 4
