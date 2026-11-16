"""
Keep the tests independent of the shell they run in.

The watcher reads its league, its dry-run switch, its tokens and the NBA
sheet ID from the environment, and it resolves the league at import. A
developer's own FIRSTIQ_LEAGUE=nba or FIRSTIQ_DRY_RUN=1 would otherwise
fail the WNBA pin for a reason that has nothing to do with the code.
"""

import os

import pytest

WATCHER_ENV = ("FIRSTIQ_LEAGUE", "FIRSTIQ_DRY_RUN", "NBA_SPREADSHEET_ID",
               "MAIN_PIPELINE_DISPATCH_TOKEN", "SELF_DISPATCH_TOKEN")

# At import, before any test module imports the watcher.
for _name in WATCHER_ENV:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _clean_watcher_env(monkeypatch):
    for name in WATCHER_ENV:
        monkeypatch.delenv(name, raising=False)
