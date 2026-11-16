# Live Play By Play (WNBA and NBA)

Watches today's WNBA and NBA scoreboards and captures each game's opening
plays into the `Play By Play` / `Live First Basket` tabs of that league's
tracker sheet as soon as they happen, instead of waiting for the main
pipeline to log the whole game as final (1-2+ hours later; for the NBA,
the next morning).

Public by design: this repo holds no prediction or scoring logic, just an
ESPN watcher, so it can poll frequently on unlimited public-repo Actions
minutes.

The repo and file names still say `wnba`. They are historical: renaming
them would break the self-chain and the references in `bball-bots`.

## Two leagues

| | WNBA | NBA |
|---|---|---|
| `FIRSTIQ_LEAGUE` | `wnba` (also the default when unset) | `nba` |
| ESPN feed | `basketball/wnba` | `basketball/nba` |
| Post-tip window (seconds left in Q1) | 60-420 (10-minute quarters) | 180-540 (12-minute quarters) |
| Real teams | 15 | 30 |

Everything that differs is in `leagues.py`: sheet, ESPN paths, the athlete
lookup, real teams, ESPN's team codes and the post-tip window. The values
are copied from `bball-bots` (`firstiq/leagues/*.py`), not imported, so this
repo stays self-contained. **If a team is added or renamed there, change it
here too**: the team filter is matched by exact name, so a near-miss
silently drops every game that team plays.

- `wnba_playbyplay_live.py` works on **one league per run**, chosen by
  `FIRSTIQ_LEAGUE`. An unknown value stops the run; it never falls back,
  because a typo would write one league's plays into the other's sheet.
- `wnba_playbyplay_cycle.py` is what the workflow calls. Each cycle it
  polls the WNBA, then the NBA, **one after the other** in the same job and
  the same `live-pbp` concurrency group, so the two never write at once.
  - A league with no relevant game is not polled that cycle. A poll costs
    four full-tab reads, on a Google quota both leagues share with
    `bball-bots`.
  - One league failing does not stop the other or end the loop. The
    failure shows as an error annotation, the next cycle retries it, and
    the run still ends red.
- The pipeline dispatches (`intraday-updates.yml`, `live-grading.yml`) are
  the same for both leagues: those `bball-bots` workflows run every league.
  Each is sent **once per poll**, covering every game that is due, not once
  per game: the NBA tips many games at the same minute, and the intraday
  workflow has no concurrency group and spends odds credits on every run.

## Dry run

Prints what a poll would write and dispatch. Writes nothing, dispatches
nothing.

- **From GitHub:** Actions -> *Live Play By Play (WNBA and NBA)* -> *Run
  workflow* -> tick `dry_run`. One pass over both leagues, whatever the
  gate says. It runs in its own concurrency group, so it neither waits for
  a live run nor blocks one.
- **Locally:** `FIRSTIQ_DRY_RUN=1 FIRSTIQ_LEAGUE=nba python wnba_playbyplay_live.py`
  (or `--dry-run`). It still needs the service account file, to read, and
  for the NBA `NBA_SPREADSHEET_ID` in the environment.

What it reports per league: whether the service account can **edit** the
sheet (asked of Drive, without writing), each tab it would create, each
`Live First Basket` row, each new `Play By Play` row with the rewrite's row
counts and the tab's grid size, and each dispatch a real run would send if
it has the dispatch token.

Three locks keep it from writing: the script branches around every write,
the Sheets client refuses anything but a read (`sheets.authorize(read_only=True)`),
and Google is asked for read-only access. The workflow's dry-run step is
given no dispatch token.

## How it gets triggered

This is the fragile part, and it's worth understanding before changing it.

`live-playbyplay.yml` fires on `schedule` (`:00` and `:30`). Each firing runs
the gate (`wnba_playbyplay_cycle.py gate`, which asks `wnba_playbyplay_gate.py`
about each league), which looks at today's *real* game times and exits `0`
(poll) if **either** league has a relevant game, or `1` (skip); the poll loop
only runs on `0`. The loop polls every 5 minutes, re-checks the gate between
cycles, and stops as soon as both slates are over so it doesn't hold the
`live-pbp` concurrency group needlessly.

**GitHub's `schedule` trigger is best-effort and has failed hard here.** On
2026-08-27 it delivered **0 of ~21** expected hourly firings while the
workflow was `active`, the YAML valid, the repo public and un-archived, and
manual dispatch working fine. Earlier: the 5-minute cron landed roughly
hourly (`9d6564a`), and one of the 3 fixed windows vanished entirely
(`1829526`). Do not assume a scheduled firing will arrive.

`wnba_playbyplay_selfchain.py` is the mitigation: when a run ends with games
still live **in either league**, it starts one fresh run via the API, so
**one** delivered firing sustains coverage for a whole slate. The cron is the
cold-start path, not an hourly dependency.

## Required secrets

| Secret | Required? | What it does |
|---|---|---|
| `GOOGLE_CREDENTIALS_JSON` | **Yes** | Service account JSON. The account needs **Editor** on both tracker sheets; a dry run reports whether it has it. Without the secret the run fails. |
| `NBA_SPREADSHEET_ID` | **Yes, for the NBA** | The NBA tracker sheet's ID. It is not written in `leagues.py` (see the note there). The ID alone, not the sheet's URL, with no spaces or line breaks. Without it (or if it is malformed, or is the WNBA sheet's) every NBA poll stops before it opens anything and the run ends red; the WNBA is unaffected. |
| `SELF_DISPATCH_TOKEN` | Strongly recommended | Lets a run hand off to its successor. Without it, coverage ends whenever the ~265-minute loop cap is hit and nothing restarts until a scheduled firing actually lands. |
| `MAIN_PIPELINE_DISPATCH_TOKEN` | Optional | Dispatches the private pipeline's `intraday-updates.yml` (post-tip) and `live-grading.yml` (once both teams have scored). Also used as a fallback for the handoff if `SELF_DISPATCH_TOKEN` is unset. |

### Creating `SELF_DISPATCH_TOKEN`

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → *Generate new token*
2. **Repository access**: Only select repositories → `FirstIQ/wnba-live-playbyplay`
3. **Permissions** → Repository permissions → **Actions: Read and write**
4. Generate, copy the token
5. In this repo: Settings → Secrets and variables → Actions → *New repository secret*, named `SELF_DISPATCH_TOKEN`

Set an expiry you'll actually renew — when it lapses the handoff silently
stops and the tracker quietly goes back to depending on the cron alone.

> `GITHUB_TOKEN` **cannot** be used here. GitHub deliberately refuses to start
> a new workflow run from an event raised with it, to prevent exactly the kind
> of recursion the handoff performs on purpose. With no usable token the
> handoff step logs and no-ops.

## Failure modes worth knowing

- **The gate fails open, the handoff fails closed.** An unnecessary poll is
  free; an unnecessary handoff chains another 4.4-hour job, so anything
  uncertain stops chaining and defers to the cron.
- **The gate step is `continue-on-error: true`** — that's how "skip" is
  signalled. So a *crash* in the gate also reads as "skip", and the run still
  reports green. `should_poll()` wraps its whole body to avoid this; keep it
  that way when editing.
- **Sheets refuses a write past a tab's grid; it does not grow the tab.**
  The rewrite would then clear `Play By Play` and fail to refill it. So the
  tab is grown (by what it needs plus 5,000 rows) *before* it is cleared; if
  growing fails the run stops with the tab untouched.
- **A run that dies mid-write can leave `Play By Play` empty**, since the
  script does `clear()` then rewrites the whole tab. A poll is stopped after
  20 minutes (`POLL_TIMEOUT_SECONDS`), far past a slow one, so a hung
  request cannot hold the other league. The NBA has about four
  times the WNBA's games, so each rewrite logs one line to measure it:
  `Play By Play rewrite [nba]: wrote N data row(s) (K kept, U for G updated
  game(s)); read back N; tab empty or partial for X.XXs, Y.YYs in all.`
  A rewrite that fails part-way logs `Play By Play rewrite [nba]: FAILED ...`
  before the error.
- **The scoreboard is "today" in Eastern time.** After midnight ET a late
  game drops off both the gate and the poll. Openings are captured within
  minutes of the tip, so this only shortens the retries for a late West
  Coast game whose feed was rejected or whose grading dispatch failed.
