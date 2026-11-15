"""
The live opening-plays capture (fetch_game_data, find_first_basket).

WHY: on 401857190 (CON @ ATL, 2026-09-17) a broken live feed showed every
period 1 shot missed. This writer saved 207 plays through period 3 and
recorded a period 3 layup as the first basket. The capture now orders
plays by game time, never reads past period 1, counts a score the app's
way, and refuses a feed whose running score it cannot explain.

    pip install pytest && python -m pytest tests/ -q
"""

from unittest import mock

import pytest

import wnba_playbyplay_live as live

HOME, AWAY = "20", "18"


def play(seq, period, clock, team, made=False, points=None, away=0, home=0, text=""):
    if points is None:
        points = 2 if made else 0
    return {"sequenceNumber": str(seq), "period": {"number": period}, "clock": {"displayValue": clock},
            "team": {"id": team}, "scoringPlay": made, "scoreValue": points,
            "awayScore": away, "homeScore": home, "text": text or f"play {seq}",
            "participants": [{"athlete": {"id": f"a{seq}"}}], "type": {"text": "Layup Shot"}}


def summary(plays):
    return {"plays": plays, "header": {"competitions": [{"competitors": [
        {"homeAway": "home", "team": {"id": HOME, "displayName": "Atlanta Dream"}},
        {"homeAway": "away", "team": {"id": AWAY, "displayName": "Connecticut Sun"}}]}]}}


@pytest.fixture
def feed():
    holder = {}
    response = mock.Mock()
    response.json = lambda: summary(holder["plays"])
    with mock.patch.object(live, "_get", return_value=response), \
            mock.patch.object(live, "get_player", side_effect=lambda aid: {"name": aid, "position": "G"}):
        yield holder


def clocks(rows):
    return [(r[0], r[1]) for r in rows]


def test_plays_are_cut_in_game_order(feed):
    feed["plays"] = [
        play(1, 1, "10:00", HOME),
        play(2, 1, "9:20", HOME, made=True, home=2),
        play(3, 1, "8:50", AWAY, made=True, away=4, home=2),
        play(4, 1, "9:00", AWAY, made=True, away=2, home=2),  # entered late, happened earlier
    ]
    rows, done, fb = live.fetch_game_data("E")
    assert clocks(rows) == [(1, "10:00"), (1, "9:20"), (1, "9:00")] and done


def test_nothing_after_period_1_and_the_first_basket_is_period_1_only(feed):
    """401857190's shape: misses all period 1, scores only in period 3."""
    feed["plays"] = [play(i, 1, f"{9 - i}:00", HOME) for i in range(5)] + \
        [play(20, 3, "8:27", HOME, made=True, home=2), play(21, 3, "7:03", AWAY, made=True, away=2, home=2)]
    rows, done, fb = live.fetch_game_data("E")
    assert {r[0] for r in rows} == {1}
    assert fb is None
    assert not done  # neither team on the board after period 1: keep re-fetching


def test_a_true_period_2_first_score_ends_the_capture_at_period_1(feed):
    feed["plays"] = [play(1, 1, "9:00", HOME, made=True, home=2), play(2, 1, "0.0", AWAY, home=2),
                     play(3, 2, "9:00", AWAY, made=True, away=2, home=2)]
    rows, done, fb = live.fetch_game_data("E")
    assert clocks(rows) == [(1, "9:00"), (1, "0.0")] and done


def test_points_with_no_flagged_score_are_rejected(feed):
    feed["plays"] = [play(1, 1, "9:00", HOME, made=True, home=2), play(2, 1, "8:00", AWAY, away=2, home=2)]
    with pytest.raises(live.UntrustedFeed, match="away team"):
        live.fetch_game_data("E")


def test_a_third_teams_score_is_rejected(feed):
    feed["plays"] = [play(1, 1, "9:00", "99", made=True, home=2)]
    with pytest.raises(live.UntrustedFeed, match="not one of this game's two teams"):
        live.fetch_game_data("E")


def test_a_flagged_play_worth_nothing_is_not_a_score(feed):
    feed["plays"] = [play(1, 1, "9:00", HOME, made=True, points=0), play(2, 1, "8:00", HOME, made=True, home=2)]
    rows, done, fb = live.fetch_game_data("E")
    assert [r[-1] for r in rows] == ["N", "Y"]
    assert fb["clock"] == "8:00"


def test_sub_minute_clocks_parse():
    assert live.parse_clock_to_seconds("45.3") == 45
    assert live.parse_clock_to_seconds("9:47") == 587
    assert live.parse_clock_to_seconds("9") is None
