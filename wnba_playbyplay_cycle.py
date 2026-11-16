"""
LIVE PLAY BY PLAY -- ONE CYCLE, EVERY LEAGUE
----------------------------------------------------------------------------
live-playbyplay.yml's loop calls this instead of calling the watcher
directly, so that one job covers every league in leagues.py:

    python wnba_playbyplay_cycle.py gate   # exit 0 = poll, 1 = skip
    python wnba_playbyplay_cycle.py poll   # one poll of each league

The rules it exists to keep:

  - ONE AFTER THE OTHER, never at once. Both leagues poll inside the same
    job and the same `live-pbp` concurrency group, WNBA first, then NBA.
    The two write different sheets but share one service account, and so
    one Google per-minute quota, with each other and with bball-bots.

  - A LEAGUE WITH NO GAME ON IS NOT POLLED. Every poll costs four full-tab
    reads before it looks at a single game. The leagues' seasons barely
    overlap, so without this the idle league would spend that quota every
    five minutes of the other's season. Each league is gated on its own
    slate, with the same gate (and the same fail-open stance) the WNBA
    always had -- so the WNBA polls exactly when it did before.

  - ONE LEAGUE FAILING DOES NOT STOP THE OTHER. Each poll is its own
    process, with FIRSTIQ_LEAGUE set for it: a crash, or a bad setting,
    in one cannot take the other down, and nothing is shared between
    them in memory. A failure is printed as a GitHub error annotation and
    `poll` exits 1, so the workflow can finish the cycle, keep looping,
    and still end the run red.

  - THE GATE PASSES IF EITHER LEAGUE HAS A RELEVANT GAME, and a crash
    while deciding means "poll". The workflow reads any non-zero exit
    from the gate step as "skip", on a run that still reports green --
    see wnba_playbyplay_gate.py.

In a dry run (FIRSTIQ_DRY_RUN, see wnba_playbyplay_live.py) `poll` looks
at every league whatever the gate says, so the output always shows both.
"""

import os
import subprocess
import sys

import leagues
import wnba_playbyplay_gate as gate
import wnba_playbyplay_live as live

# Beside this file, wherever the job's working directory is.
WATCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wnba_playbyplay_live.py")

# A poll normally takes well under a minute. The Sheets client has no
# timeout of its own, so without a ceiling one hung request would hold the
# other league (and the whole loop) until the job's 300-minute limit, which
# also skips the hand-off. 15 minutes is far past a poll that is merely slow:
# every request retrying its full backoff (sheets.py) still finishes inside it.
POLL_TIMEOUT_SECONDS = 900


def any_league_relevant():
    relevant = False
    for league in leagues.ALL:
        if gate.should_poll(league):
            print(f"{league.name}: a game is live, starting soon, or recently finished.")
            relevant = True
        else:
            print(f"{league.name}: nothing relevant right now.")
    return relevant


def poll():
    """One poll of each league that has a game on. Returns the exit code:
    0 if every poll that ran succeeded, 1 if any failed."""
    dry_run = live.dry_run_enabled()
    failed = []
    for league in leagues.ALL:
        if not dry_run and not gate.should_poll(league):
            print(f"--- {league.name}: nothing relevant right now -- not polled this cycle.")
            continue
        print(f"--- {league.name} ---", flush=True)
        try:
            code = subprocess.run(
                [sys.executable, WATCHER],
                env={**os.environ, leagues.ENV_VAR: league.key},
                timeout=POLL_TIMEOUT_SECONDS,
            ).returncode
        except subprocess.TimeoutExpired:
            print(f"The {league.name} poll was stopped after {POLL_TIMEOUT_SECONDS}s without finishing. "
                  f"If it was mid-rewrite, check the 'Play By Play' tab.")
            code = 1
        except Exception as e:
            print(f"Could not start the {league.name} poll: {type(e).__name__}")
            code = 1
        if code != 0:
            failed.append(league.name)
            print(f"::error::{league.name} poll failed (exit {code}). "
                  f"The other league still runs; the next cycle retries this one.")
    return 1 if failed else 0


def main(argv):
    if argv == ["gate"]:
        try:
            relevant = any_league_relevant()
        except Exception as e:
            print(f"Could not decide ({e}) -- failing open, will poll.")
            relevant = True
        if relevant:
            raise SystemExit(0)
        print("Nothing relevant in any league -- skipping.")
        raise SystemExit(1)
    if argv == ["poll"]:
        raise SystemExit(poll())
    raise SystemExit("usage: python wnba_playbyplay_cycle.py gate|poll")


if __name__ == "__main__":
    # Keep this process's lines in order with each league's own output.
    sys.stdout.reconfigure(line_buffering=True)
    main(sys.argv[1:])
