"""
The dry run: say what a poll would write, write nothing, dispatch nothing.

WHY: the first NBA run writes to a production sheet the app reads, and
nobody had checked that it would write the right thing, or that the
service account could write there at all. The dry run answers both
without touching the sheet. It is guarded three ways, each tested here:
main() branches around every write, the Sheets client refuses anything
but a read, and the credentials are asked for with read-only scopes.

    pip install pytest && python -m pytest tests/ -q
"""

from unittest import mock

import pytest

import sheets
import wnba_playbyplay_live as live
import wnba_playbyplay_selfchain as selfchain
from test_live_main import wnba_world


@pytest.fixture
def dry(monkeypatch):
    monkeypatch.setenv("FIRSTIQ_DRY_RUN", "true")


class TestTheSwitch:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "maybe"])
    def test_on(self, monkeypatch, value):
        """Anything not plainly 'off' is on: a misread switch must fail
        toward not writing."""
        monkeypatch.setenv("FIRSTIQ_DRY_RUN", value)
        assert live.dry_run_enabled() is True

    @pytest.mark.parametrize("value", [None, "", "0", "false", "False", "no", "off"])
    def test_off(self, monkeypatch, value):
        monkeypatch.delenv("FIRSTIQ_DRY_RUN", raising=False)
        if value is not None:
            monkeypatch.setenv("FIRSTIQ_DRY_RUN", value)
        assert live.dry_run_enabled() is False

    def test_the_flag_turns_it_on_too(self, monkeypatch):
        monkeypatch.delenv("FIRSTIQ_DRY_RUN", raising=False)
        assert live.dry_run_enabled(["--dry-run"]) is True
        assert live.dry_run_enabled([]) is False


class TestADryPoll:
    def test_nothing_is_written_created_or_dispatched(self, monkeypatch, dry):
        monkeypatch.setenv("MAIN_PIPELINE_DISPATCH_TOKEN", "test-token")
        world = wnba_world()
        before = {title: [list(r) for r in ws.values] for title, ws in world.spreadsheet.tabs.items()}

        world.run(monkeypatch)

        assert world.writes() == {}
        assert world.spreadsheet.created == []
        assert world.dispatched == []
        assert {title: ws.values for title, ws in world.spreadsheet.tabs.items()} == before

    def test_the_sheet_is_opened_read_only(self, monkeypatch, dry):
        world = wnba_world().run(monkeypatch)
        assert world.authorize_calls == [{"read_only": True}]
        assert world.scopes and all(scope.endswith(".readonly") for scope in world.scopes)

    def test_a_real_run_keeps_its_writing_scopes(self, monkeypatch):
        monkeypatch.delenv("FIRSTIQ_DRY_RUN", raising=False)
        world = wnba_world().run(monkeypatch)
        assert world.authorize_calls == [{"read_only": False}]
        assert world.scopes == ["https://www.googleapis.com/auth/spreadsheets",
                                "https://www.googleapis.com/auth/drive"]

    def test_it_prints_what_it_would_have_written(self, monkeypatch, dry, capsys):
        wnba_world().run(monkeypatch)
        out = capsys.readouterr().out
        # The plays, the first baskets, the tab it would create, both dispatches.
        assert "DRY RUN" in out
        assert "['401', '2026-09-17', 'CON @ ATL', 1, '9:20', 560, 'Atlanta Dream', 0, 2, " \
               "'Player Two makes driving layup', 'Y']" in out
        assert "'Player Two', 'a2', 'Atlanta Dream', 'G', 'Layup'" in out
        assert "would create the 'Post-Tip Dispatch Log' tab" in out
        assert "would dispatch intraday-updates.yml" in out
        assert "would dispatch live-grading.yml" in out
        assert "would rewrite 'Play By Play': 11 data row(s)" in out

    def test_it_reports_dispatches_even_without_a_token(self, monkeypatch, dry, capsys):
        """The dry-run workflow step is never given the token."""
        monkeypatch.delenv("MAIN_PIPELINE_DISPATCH_TOKEN", raising=False)
        world = wnba_world().run(monkeypatch)
        assert "would dispatch live-grading.yml" in capsys.readouterr().out
        assert world.dispatched == []

    @pytest.mark.parametrize("can_edit, says", [
        (True, "can edit this sheet: yes"), (False, "can edit this sheet: NO")])
    def test_it_reports_whether_the_account_could_have_written(self, monkeypatch, dry, capsys, can_edit, says):
        world = wnba_world(can_edit=can_edit).run(monkeypatch)
        assert says in capsys.readouterr().out
        assert [method.upper() for method, _ in world.client.http_client.requests] == ["GET"]

    def test_a_failed_access_check_says_unknown_and_carries_on(self, monkeypatch, dry, capsys):
        world = wnba_world()
        world.client.http_client.request = mock.Mock(
            side_effect=RuntimeError("Drive API has not been used in project 123456789012"))
        world.run(monkeypatch)
        out = capsys.readouterr().out
        assert "can edit this sheet: unknown (RuntimeError)" in out and "would rewrite 'Play By Play'" in out
        # Drive's own words name the file and the Google Cloud project.
        assert "123456789012" not in out

    def test_the_sheet_id_is_never_printed(self, monkeypatch, dry, capsys):
        """This repo's Actions logs are public."""
        world = wnba_world().run(monkeypatch)
        assert world.client.opened[0] not in capsys.readouterr().out


class TestTheReadOnlyClient:
    """sheets.authorize(read_only=True): the guard that still holds if
    main() ever grows a write the dry run forgot to branch around."""

    @pytest.fixture
    def transport(self, monkeypatch):
        inner = mock.Mock(return_value="response")
        client = mock.Mock()
        client.http_client.request = inner
        monkeypatch.setattr(sheets.gspread, "authorize", lambda creds: client)
        return sheets.authorize(object(), read_only=True).http_client, inner

    def test_reads_go_through(self, transport):
        client, inner = transport
        assert client.request("get", "/v4/spreadsheets/x") == "response"
        inner.assert_called_once()

    @pytest.mark.parametrize("call", [
        (("post", "/v4/spreadsheets/x/values/A1:append"), {}),
        (("put", "/v4/spreadsheets/x/values/A1"), {}),
        (("delete", "/x"), {}),
        ((), {"method": "POST", "endpoint": "/x:batchUpdate"}),
        ((), {}),  # a verb it cannot read is refused, not guessed
    ])
    def test_every_write_is_refused_before_it_is_sent(self, transport, call):
        client, inner = transport
        args, kwargs = call
        with pytest.raises(sheets.ReadOnlyViolation):
            client.request(*args, **kwargs)
        inner.assert_not_called()

    def test_a_normal_client_still_writes(self, monkeypatch):
        inner = mock.Mock(return_value="response")
        client = mock.Mock()
        client.http_client.request = inner
        monkeypatch.setattr(sheets.gspread, "authorize", lambda creds: client)
        assert sheets.authorize(object()).http_client.request("post", "/x") == "response"


class TestDispatchesInADryRun:
    def test_the_pipeline_dispatch_never_posts(self, monkeypatch, dry):
        post = mock.Mock()
        monkeypatch.setattr(live.requests, "post", post)
        assert live.trigger_main_pipeline_workflow("token", "live-grading.yml") is False
        post.assert_not_called()

    def test_the_self_dispatch_never_posts(self, monkeypatch, dry):
        post = mock.Mock()
        monkeypatch.setattr(selfchain.requests, "post", post)
        assert selfchain.dispatch_self("token") is False
        post.assert_not_called()
