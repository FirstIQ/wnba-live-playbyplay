"""
Which play is the game's first basket, and when an existing row is wrong.

WHY THIS FILE EXISTS: on 2026-10-04, LVA @ GSV (event 401918296), the
'Live First Basket' row said A'ja Wilson -- her 7:51 jumper, which landed
at 5-2 and was the game's THIRD score. Chelsea Gray's 8:27 three was first.
The row was written once and never looked at again, while the Play By Play
tab this same script rewrites every poll showed Gray, so the app put both
answers on one screen.

    pip install pytest && python -m pytest tests/ -q
"""

import pytest

import wnba_playbyplay_live as live

TEAMS = {"9": "Las Vegas Aces", "129689": "Golden State Valkyries"}
PLAYERS = {
    "2529122": {"name": "Chelsea Gray", "position": "G"},
    "3149391": {"name": "A'ja Wilson", "position": "F"},
    "4398935": {"name": "Veronica Burton", "position": "G"},
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(live, "get_player", lambda athlete_id: PLAYERS[athlete_id])


def play(seq, clock, team, athlete, scoring, value, away, home, type_text="Jump Shot"):
    return {
        "sequenceNumber": str(seq),
        "period": {"number": 1},
        "clock": {"displayValue": clock},
        "team": {"id": team},
        "participants": [{"athlete": {"id": athlete}}],
        "scoringPlay": scoring,
        "scoreValue": value,
        "awayScore": away,
        "homeScore": home,
        "type": {"text": type_text},
    }


GRAY_THREE = play(20, "8:27", "9", "2529122", True, 3, 3, 0)
BURTON_FT_1 = play(24, "8:01", "129689", "4398935", True, 1, 3, 1, "Free Throw - 1 of 2")
BURTON_FT_2 = play(25, "8:01", "129689", "4398935", True, 1, 3, 2, "Free Throw - 2 of 2")
WILSON_JUMPER = play(26, "7:51", "9", "3149391", True, 2, 5, 2)


class TestFindFirstBasket:
    def test_the_feed_in_order_names_the_first_score(self):
        found = live.find_first_basket([GRAY_THREE, BURTON_FT_1, BURTON_FT_2, WILSON_JUMPER], TEAMS)
        assert found["name"] == "Chelsea Gray"
        assert found["method"] == "3-Pointer"
        assert found["team"] == "Las Vegas Aces"

    def test_an_out_of_order_feed_is_sorted_by_sequence(self):
        found = live.find_first_basket([WILSON_JUMPER, BURTON_FT_2, GRAY_THREE, BURTON_FT_1], TEAMS)
        assert found["name"] == "Chelsea Gray"

    def test_a_feed_missing_the_first_points_records_nothing(self):
        # Gray's three absent (or not yet flagged as a score): the first
        # flagged play lands at 3-1 for 1 point, so earlier points exist.
        assert live.find_first_basket([BURTON_FT_1, BURTON_FT_2, WILSON_JUMPER], TEAMS) is None

    def test_an_unflagged_first_score_records_nothing(self):
        unflagged = dict(GRAY_THREE, scoringPlay=False)
        assert live.find_first_basket([unflagged, BURTON_FT_1, BURTON_FT_2, WILSON_JUMPER], TEAMS) is None

    def test_a_free_throw_can_be_the_first_basket(self):
        opener = play(5, "9:40", "129689", "4398935", True, 1, 0, 1, "Free Throw - 1 of 2")
        assert live.find_first_basket([opener, WILSON_JUMPER], TEAMS)["name"] == "Veronica Burton"

    def test_string_scores_are_read_as_numbers(self):
        as_strings = dict(GRAY_THREE, awayScore="3", homeScore="0")
        assert live.find_first_basket([as_strings], TEAMS)["name"] == "Chelsea Gray"


WRONG_ROW = ["401918296", "2026-10-04", "LVA @ GSV", "A'ja Wilson", "3149391", "Las Vegas Aces",
             "F", "Jump Shot", "1", "7:51", "2026-10-04 15:04:11"]


class TestFirstBasketCorrection:
    def test_a_row_naming_another_scorer_is_rewritten_keeping_its_keys_and_moment(self):
        gray = live.find_first_basket([GRAY_THREE], TEAMS)
        corrected = live.first_basket_correction(WRONG_ROW, gray)
        assert corrected == ["401918296", "2026-10-04", "LVA @ GSV", "Chelsea Gray", "2529122",
                             "Las Vegas Aces", "G", "3-Pointer", 1, "8:27", "2026-10-04 15:04:11"]

    def test_a_row_already_naming_the_scorer_is_left_alone(self):
        wilson = live.find_first_basket([play(1, "9:50", "9", "3149391", True, 2, 2, 0)], TEAMS)
        assert live.first_basket_correction(WRONG_ROW, wilson) is None

    def test_no_answer_this_poll_never_rewrites_a_row(self):
        assert live.first_basket_correction(WRONG_ROW, None) is None
