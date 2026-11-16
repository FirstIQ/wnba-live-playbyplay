"""
One poll cycle across both leagues: the gate, the poll, the handoff.

WHY: the two leagues share one job and one Google quota. They must run
one after the other, never at once; a league with no game on must cost
nothing; one league crashing must not stop the other; and the self-chain
must start ONE successor however many leagues still have games.

    pip install pytest && python -m pytest tests/ -q
"""

import os
import subprocess
import sys
from unittest import mock

import pytest
import yaml

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

    def run(argv, env=None, timeout=None, **kwargs):
        key = env["FIRSTIQ_LEAGUE"]
        calls.append({"argv": argv, "league": key, "dry_run": env.get("FIRSTIQ_DRY_RUN", ""),
                      "timeout": timeout})
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
        assert all(c["argv"] == [sys.executable, cycle.WATCHER] for c in calls)
        assert cycle.WATCHER.endswith("wnba_playbyplay_live.py") and os.path.isfile(cycle.WATCHER)

    def test_a_hung_poll_is_given_a_ceiling(self, monkeypatch, runs):
        """The Sheets client has no timeout of its own, and the polls run
        one after the other: a hang in one would hold the other forever."""
        calls, _ = runs
        relevant(monkeypatch, wnba=True, nba=True)
        cycle.poll()
        assert [c["timeout"] for c in calls] == [cycle.POLL_TIMEOUT_SECONDS] * 2
        # Past a poll whose every request retries its full backoff (13 x ~75s).
        assert cycle.POLL_TIMEOUT_SECONDS > 13 * 80

    def test_a_league_with_no_game_on_is_not_polled(self, monkeypatch, runs):
        """Four full-tab reads a poll, on a quota shared with bball-bots."""
        calls, _ = runs
        relevant(monkeypatch, wnba=False, nba=True)
        assert cycle.poll() == 0
        assert [c["league"] for c in calls] == ["nba"]

    @pytest.mark.parametrize("failure", [
        1, OSError("could not start"), subprocess.TimeoutExpired("watcher", 1200)])
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
    """The loop lives in YAML, which no test can run. These pin, exactly,
    the conditions that keep a dry run from writing, keep real runs in one
    writing group, and keep a red loop from costing the slate its successor.
    Each was checked by breaking it and watching the looser version of this
    test stay green."""

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows",
                        "live-playbyplay.yml")
    text = open(path, encoding="utf-8").read()
    workflow = yaml.safe_load(text)
    job = workflow["jobs"]["live-pbp"]
    steps = {step["name"]: step for step in job["steps"] if "name" in step}

    DRY = "Dry run (one pass, writes nothing)"
    LOOP = "Capture live opening play by play (polls every 5 min, up to ~4.4h)"
    HANDOFF = "Hand off to a fresh run if games are still live"

    def test_real_runs_share_one_writing_group_and_a_dry_run_has_its_own(self):
        assert self.workflow["concurrency"] == {
            "group": "${{ inputs.dry_run && 'live-pbp-dry-run' || 'live-pbp' }}",
            "cancel-in-progress": False,
        }

    def test_a_dry_run_never_reaches_the_writing_loop_or_the_handoff(self):
        assert self.steps[self.DRY]["if"] == "${{ inputs.dry_run }}"
        assert self.steps[self.LOOP]["if"] == "${{ steps.gate.outcome == 'success' && !inputs.dry_run }}"
        assert self.steps[self.HANDOFF]["if"] == \
            "${{ !cancelled() && steps.gate.outcome == 'success' && !inputs.dry_run }}"

    def test_the_dry_run_is_off_unless_asked_for(self):
        dry_run = self.workflow[True]["workflow_dispatch"]["inputs"]["dry_run"]  # YAML reads `on:` as True
        assert dry_run["type"] == "boolean" and dry_run["default"] is False

    def test_the_dry_run_step_is_told_to_be_dry_and_is_given_no_dispatch_token(self):
        assert self.steps[self.DRY]["env"] == {
            "FIRSTIQ_DRY_RUN": "true",
            "GOOGLE_CREDENTIALS_PATH": "${{ runner.temp }}/gcreds.json",
            "NBA_SPREADSHEET_ID": "${{ secrets.NBA_SPREADSHEET_ID }}",
        }
        assert self.steps[self.DRY]["run"] == "python wnba_playbyplay_cycle.py poll"
        # Nor through the job or the workflow, which every step inherits.
        assert self.job["env"] == {"PYTHONUNBUFFERED": "1"}
        assert "env" not in self.workflow

    def test_the_loop_gets_the_nba_sheet_id_and_the_dispatch_token(self):
        assert set(self.steps[self.LOOP]["env"]) == {
            "GOOGLE_CREDENTIALS_PATH", "MAIN_PIPELINE_DISPATCH_TOKEN", "NBA_SPREADSHEET_ID"}

    def test_the_loop_polls_through_the_cycle_not_one_league_directly(self):
        loop = self.steps[self.LOOP]["run"]
        assert "python wnba_playbyplay_cycle.py poll || FAILED=1" in loop
        assert "if ! python wnba_playbyplay_cycle.py gate; then" in loop
        assert self.steps["Check whether a game is relevant right now"]["run"] == "python wnba_playbyplay_cycle.py gate"
        assert "wnba_playbyplay_live.py" not in "".join(step.get("run", "") for step in self.job["steps"])

    def test_a_failed_poll_ends_the_run_red_after_the_loop_not_during_it(self):
        loop = self.steps[self.LOOP]["run"].rstrip()
        assert loop.endswith('if [ "$FAILED" -ne 0 ]; then\n'
                             '  echo "::error::At least one poll failed during this run -- '
                             'see the per-league errors above."\n  exit 1\nfi')

    def test_the_built_in_token_can_only_read(self):
        assert self.workflow["permissions"] == {"contents": "read"}
        assert self.job["steps"][0] == {"uses": "actions/checkout@v4", "with": {"persist-credentials": False}}

    def test_the_job_outlasts_a_final_cycle_in_which_both_polls_hit_their_ceiling(self):
        assert self.job["timeout-minutes"] * 60 > 15900 + 2 * cycle.POLL_TIMEOUT_SECONDS
        assert self.job["timeout-minutes"] < 360  # GitHub's hard cap
