"""
One poll cycle across both leagues: the gate, the poll, the handoff.

WHY: the two leagues share one job and one Google quota. They must run
one after the other, never at once; a league with no game on must cost
nothing; one league crashing must not stop the other; and the self-chain
must start ONE successor however many leagues still have games.

    pip install pytest && python -m pytest tests/ -q
"""

import sys
from unittest import mock

import pytest

import leagues
import wnba_playbyplay_cycle as cycle
import wnba_playbyplay_gate as gate
import wnba_playbyplay_live as live
import wnba_playbyplay_selfchain as selfchain
from harness import event, team

BOS = team("2", "Boston Celtics", "BOS")
NYK = team("18", "New York Knicks", "NY")
DREAM = team("20", "Atlanta Dream", "ATL")
SUN = team("18", "Connecticut Sun", "CONN")


def scoreboards(monkeypatch, by_league):
    """Each league's scoreboard answers with its own events, or raises."""
    asked = []

    def _get(url, **kwargs):
        asked.append(url)
        key = "wnba" if "/basketball/wnba/" in url else "nba"
        events = by_league[key]
        if isinstance(events, Exception):
            raise events
        response = mock.Mock()
        response.json = lambda: {"events": events}
        return response

    monkeypatch.setattr(live, "_get", _get)
    return asked


def relevant(monkeypatch, **by_key):
    monkeypatch.setattr(gate, "should_poll", lambda league=None: by_key[league.key])


@pytest.fixture
def runs(monkeypatch):
    """Records each league's poll instead of starting a process."""
    calls = []
    codes = {}

    def run(argv, env=None, **kwargs):
        key = env["FIRSTIQ_LEAGUE"]
        calls.append({"argv": argv, "league": key, "dry_run": env.get("FIRSTIQ_DRY_RUN", "")})
        outcome = codes.get(key, 0)
        if isinstance(outcome, Exception):
            raise outcome
        return mock.Mock(returncode=outcome)

    monkeypatch.setattr(cycle.subprocess, "run", run)
    monkeypatch.delenv("FIRSTIQ_DRY_RUN", raising=False)
    return calls, codes


class TestGate:
    def test_each_league_reads_its_own_scoreboard(self, monkeypatch):
        asked = scoreboards(monkeypatch, {"wnba": [], "nba": [event("1", BOS, NYK, "in")]})
        assert gate.should_poll(leagues.NBA) is True
        assert gate.should_poll(leagues.WNBA) is False
        assert "/basketball/nba/scoreboard" in asked[0] and "/basketball/wnba/scoreboard" in asked[1]

    def test_the_gate_still_fails_open(self, monkeypatch):
        scoreboards(monkeypatch, {"wnba": RuntimeError("ESPN down"), "nba": RuntimeError("ESPN down")})
        assert gate.should_poll(leagues.NBA) is True

    @pytest.mark.parametrize("wnba, nba, passes", [
        (True, False, True), (False, True, True), (True, True, True), (False, False, False)])
    def test_the_cycle_gate_passes_if_either_league_has_a_game(self, monkeypatch, wnba, nba, passes):
        relevant(monkeypatch, wnba=wnba, nba=nba)
        assert cycle.any_league_relevant() is passes

    @pytest.mark.parametrize("nba, code", [(True, 0), (False, 1)])
    def test_exit_code_is_what_the_workflow_branches_on(self, monkeypatch, nba, code):
        relevant(monkeypatch, wnba=False, nba=nba)
        with pytest.raises(SystemExit) as stop:
            cycle.main(["gate"])
        assert stop.value.code == code

    def test_a_crash_while_deciding_polls_rather_than_skips(self, monkeypatch):
        """The workflow reads any non-zero exit as 'skip', on a run that
        still reports green."""
        monkeypatch.setattr(gate, "should_poll", mock.Mock(side_effect=RuntimeError("shape changed")))
        with pytest.raises(SystemExit) as stop:
            cycle.main(["gate"])
        assert stop.value.code == 0


class TestPoll:
    def test_both_leagues_run_one_after_the_other_wnba_first(self, monkeypatch, runs):
        calls, _ = runs
        relevant(monkeypatch, wnba=True, nba=True)
        assert cycle.poll() == 0
        assert [c["league"] for c in calls] == ["wnba", "nba"]
        assert all(c["argv"] == [sys.executable, "wnba_playbyplay_live.py"] for c in calls)

    def test_a_league_with_no_game_on_is_not_polled(self, monkeypatch, runs):
        """Four full-tab reads a poll, on a quota shared with bball-bots."""
        calls, _ = runs
        relevant(monkeypatch, wnba=False, nba=True)
        assert cycle.poll() == 0
        assert [c["league"] for c in calls] == ["nba"]

    @pytest.mark.parametrize("failure", [1, OSError("could not start")])
    def test_one_league_failing_does_not_stop_the_other(self, monkeypatch, runs, failure, capsys):
        calls, codes = runs
        codes["wnba"] = failure
        relevant(monkeypatch, wnba=True, nba=True)
        assert cycle.poll() == 1  # the run still ends red
        assert [c["league"] for c in calls] == ["wnba", "nba"]
        assert "::error::" in capsys.readouterr().out

    def test_a_dry_run_looks_at_every_league_whatever_the_gate_says(self, monkeypatch, runs):
        calls, _ = runs
        monkeypatch.setenv("FIRSTIQ_DRY_RUN", "true")
        relevant(monkeypatch, wnba=False, nba=False)
        assert cycle.poll() == 0
        assert [(c["league"], c["dry_run"]) for c in calls] == [("wnba", "true"), ("nba", "true")]


class TestHandoff:
    @pytest.fixture
    def posts(self, monkeypatch):
        sent = []
        monkeypatch.setattr(selfchain.requests, "post",
                            lambda url, **kw: sent.append(url) or mock.Mock(status_code=204))
        monkeypatch.setenv("SELF_DISPATCH_TOKEN", "test-token")
        monkeypatch.delenv("FIRSTIQ_DRY_RUN", raising=False)
        return sent

    def test_one_successor_when_only_the_nba_is_still_on(self, monkeypatch, posts):
        scoreboards(monkeypatch, {"wnba": [], "nba": [event("1", BOS, NYK, "in")]})
        selfchain.main()
        assert posts == ["https://api.github.com/repos/FirstIQ/wnba-live-playbyplay"
                         "/actions/workflows/live-playbyplay.yml/dispatches"]

    def test_one_successor_not_two_when_both_leagues_are_on(self, monkeypatch, posts):
        scoreboards(monkeypatch, {"wnba": [event("2", SUN, DREAM, "in")], "nba": [event("1", BOS, NYK, "in")]})
        selfchain.main()
        assert len(posts) == 1

    def test_no_successor_when_both_slates_are_over(self, monkeypatch, posts):
        scoreboards(monkeypatch, {"wnba": [], "nba": []})
        selfchain.main()
        assert posts == []

    def test_one_leagues_outage_does_not_hide_the_others_live_game(self, monkeypatch, posts):
        scoreboards(monkeypatch, {"wnba": RuntimeError("ESPN down"), "nba": [event("1", BOS, NYK, "in")]})
        selfchain.main()
        assert len(posts) == 1

    def test_it_still_fails_closed(self, monkeypatch, posts):
        """A persistent outage must not chain 4.4-hour runs forever."""
        scoreboards(monkeypatch, {"wnba": RuntimeError("ESPN down"), "nba": RuntimeError("ESPN down")})
        selfchain.main()
        assert posts == []

    def test_a_dry_run_starts_nothing(self, monkeypatch, posts, capsys):
        monkeypatch.setenv("FIRSTIQ_DRY_RUN", "1")
        scoreboards(monkeypatch, {"wnba": [], "nba": [event("1", BOS, NYK, "in")]})
        selfchain.main()
        assert posts == []
        assert "DRY RUN" in capsys.readouterr().out


class TestWorkflow:
    """The loop lives in YAML, which no test can run. These pin the three
    things that would silently undo the above."""

    text = open(".github/workflows/live-playbyplay.yml", encoding="utf-8").read()

    def test_the_loop_polls_through_the_cycle_not_one_league_directly(self):
        assert "python wnba_playbyplay_cycle.py poll" in self.text
        assert "python wnba_playbyplay_cycle.py gate" in self.text
        assert "python wnba_playbyplay_live.py" not in self.text

    def test_real_runs_still_share_the_one_writing_group(self):
        assert "'live-pbp'" in self.text and "cancel-in-progress: false" in self.text

    def test_a_dry_run_is_offered_and_never_given_a_dispatch_token(self):
        assert "dry_run:" in self.text
        dry_step = self.text.split("- name: Dry run")[1].split("- name:")[0]
        assert "FIRSTIQ_DRY_RUN" in dry_step
        assert "DISPATCH_TOKEN" not in dry_step
