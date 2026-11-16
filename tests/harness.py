"""
A fake tracker sheet and a fake ESPN, so wnba_playbyplay_live.main() can
run end to end in a test: no network, no credentials, no spreadsheet.

Only stable names are patched (live._get, live.requests.post,
live.Credentials, live.sheets_client.authorize, live.datetime), so the
same test can run against the code before and after a refactor. That is
the point: test_live_main.py pins what one poll writes for the WNBA.
"""

from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import gspread

import wnba_playbyplay_live as live

NOW = datetime(2026, 9, 17, 19, 12, 5, tzinfo=ZoneInfo("America/New_York"))
TODAY = "2026-09-17"
STAMP = "2026-09-17 19:12:05"

# What Sheets runs as a formula when a cell is written USER_ENTERED.
FORMULA_STARTS = ("=", "+", "-", "@")


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class GridLimitExceeded(Exception):
    """What Sheets answers (a 400) when a write names a row past the tab's
    grid: it refuses the write, it does not grow the tab."""


class FakeWorksheet:
    def __init__(self, title, values, row_count=5000):
        self.title = title
        self.values = [list(r) for r in values]
        self.row_count = row_count
        self.ops = []
        self.formulas = []  # cells Sheets would have run instead of storing
        self.fail_on = None  # an op name that raises instead of running

    @property
    def writes(self):
        return [op for op in self.ops if op != "get_all_values"]

    def get_all_values(self):
        self.ops.append("get_all_values")
        return [list(r) for r in self.values]

    def _typed(self, row, value_input_option):
        """The row as Sheets keeps it. USER_ENTERED reads a string as if it
        were typed into the cell: a leading apostrophe means "this is text"
        and is not kept, and a leading = + - or @ makes the cell a formula.
        The fake does not run formulas; it lists them in `formulas`. RAW
        stores every value as it was sent."""
        if value_input_option != "USER_ENTERED":
            return list(row)
        kept = []
        for cell in row:
            if isinstance(cell, str) and cell.startswith("'"):
                cell = cell[1:]
            elif isinstance(cell, str) and cell.startswith(FORMULA_STARTS):
                self.formulas.append(cell)
            kept.append(cell)
        return kept

    def append_row(self, row, value_input_option=None):
        self.ops.append("append_row")
        self.values.append(self._typed(row, value_input_option))

    def append_rows(self, rows, value_input_option=None):
        self.ops.append("append_rows")
        self.values.extend(self._typed(r, value_input_option) for r in rows)

    def _maybe_fail(self, op):
        if self.fail_on == op:
            raise RuntimeError(f"{op} failed")

    def clear(self):
        self._maybe_fail("clear")
        self.ops.append("clear")
        self.values = []

    def resize(self, rows=None, cols=None):
        self._maybe_fail("resize")
        self.ops.append(f"resize:{rows}")
        self.row_count = rows

    def update(self, *, range_name, values, value_input_option=None):
        self._maybe_fail(f"update:{range_name.split(':')[0]}")
        first = int("".join(ch for ch in range_name.split(":")[0] if ch.isdigit())) - 1
        if first + len(values) > self.row_count:
            raise GridLimitExceeded(f"{range_name} exceeds grid limits. Max rows: {self.row_count}")
        self.ops.append(f"update:{range_name}")
        while len(self.values) < first + len(values):
            self.values.append([])
        for offset, row in enumerate(values):
            self.values[first + offset] = self._typed(row, value_input_option)


class FakeSpreadsheet:
    def __init__(self, tabs):
        self.tabs = {title: FakeWorksheet(title, values) for title, values in tabs.items()}
        self.created = []

    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.created.append(title)
        self.tabs[title] = FakeWorksheet(title, [], row_count=rows)
        return self.tabs[title]


class FakeTransport:
    """Stands in for gspread's http_client: only the dry run's
    can-this-account-edit check talks to it directly."""

    def __init__(self, can_edit):
        self.can_edit = can_edit
        self.requests = []

    def request(self, method, endpoint, **kwargs):
        self.requests.append((method, endpoint))
        response = mock.Mock()
        response.json = lambda: {"capabilities": {"canEdit": self.can_edit}}
        return response


class FakeClient:
    def __init__(self, spreadsheet, can_edit=True):
        self.spreadsheet = spreadsheet
        self.opened = []
        self.http_client = FakeTransport(can_edit)

    def open_by_key(self, key):
        self.opened.append(key)
        return self.spreadsheet


def team(team_id, name, espn_abbr):
    return {"id": team_id, "displayName": name, "abbreviation": espn_abbr}


def event(event_id, away, home, state, period=0, clock="0.0", slug="regular-season",
          tip="2026-09-17T23:00Z"):
    return {"id": event_id, "date": tip, "season": {"slug": slug},
            "status": {"type": {"state": state}, "period": period, "displayClock": clock},
            "competitions": [{"competitors": [{"homeAway": "home", "team": home},
                                              {"homeAway": "away", "team": away}]}]}


def play(seq, clock, by, athlete, text, made=False, points=None, away=0, home=0,
         kind="Jump Shot", period=1):
    if points is None:
        points = 2 if made else 0
    return {"sequenceNumber": str(seq), "period": {"number": period}, "clock": {"displayValue": clock},
            "team": {"id": by["id"]}, "scoringPlay": made, "scoreValue": points,
            "awayScore": away, "homeScore": home, "text": text,
            "participants": [{"athlete": {"id": athlete}}], "type": {"text": kind}}


def summary(away, home, plays):
    return {"plays": plays, "header": {"competitions": [{"competitors": [
        {"homeAway": "home", "team": home}, {"homeAway": "away", "team": away}]}]}}


class World:
    """Everything one run of main() can touch, and everything it did."""

    def __init__(self, tabs, events, summaries, athletes, can_edit=True):
        self.spreadsheet = FakeSpreadsheet(tabs)
        self.client = FakeClient(self.spreadsheet, can_edit)
        self.events, self.summaries, self.athletes = events, summaries, athletes
        self.fetched = []       # every ESPN URL asked for, in order
        self.dispatched = []    # every workflow dispatch URL posted to
        self.authorize_calls = []
        self.scopes = None

    def _get(self, url, **kwargs):
        self.fetched.append(url)
        response = mock.Mock()
        if "/scoreboard?dates=" in url:
            response.json = lambda: {"events": self.events}
        elif "/summary?event=" in url:
            response.json = lambda: self.summaries[url.split("event=")[1]]
        elif "/athletes/" in url:
            response.json = lambda: self.athletes[url.split("/athletes/")[1].split("?")[0]]
        else:
            raise AssertionError(f"unexpected ESPN URL {url}")
        return response

    def _post(self, url, **kwargs):
        self.dispatched.append(url)
        return mock.Mock(status_code=204)

    def _credentials(self, path, scopes):
        self.scopes = scopes
        return object()

    def _authorize(self, creds, **kwargs):
        self.authorize_calls.append(kwargs)
        return self.client

    def tab(self, title):
        return self.spreadsheet.tabs[title].values

    def writes(self):
        """{tab: [write ops]} for every tab that was written to."""
        return {t: ws.writes for t, ws in self.spreadsheet.tabs.items() if ws.writes}

    def run(self, monkeypatch):
        monkeypatch.setattr(live, "_get", self._get)
        monkeypatch.setattr(live.requests, "post", self._post)
        monkeypatch.setattr(live.Credentials, "from_service_account_file", self._credentials)
        monkeypatch.setattr(live.sheets_client, "authorize", self._authorize)
        monkeypatch.setattr(live, "datetime", FrozenDatetime)
        live._athlete_cache.clear()
        live.main()
        return self
