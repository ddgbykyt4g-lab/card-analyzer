#!/usr/bin/env python3
"""Data health check for the Saturday run: catches a source that broke quietly.

The collector logs source failures and carries on, so a green GitHub run can still mean stale data.
Checks: results missing for matches played 2-10 days ago, source errors in the collector log, bets left
pending well after kickoff, matches about to kick off with no referee, the Betfair price log going quiet. Prints "OK" or one problem per line; exit code is always 0.
Usage: python agent/health.py cards.db [collector.log]
"""
import csv
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ISO = "%Y-%m-%dT%H:%M:%SZ"
LOG_PROBLEMS = re.compile(r"Traceback|step failed|no rows at|fixtures\.csv unavailable|Unmatched team|"
                          r"HTTP [45]\d\d|giving up")
# Hours ahead by which each league has named its referees (La Liga: the day before, by 16:00 Madrid)
REF_HOURS = {"EPL": 48, "SPL": 48, "BUNDES": 48, "SERIEA": 48, "LIGUE1": 48, "LALIGA": 18}
STALE_RESULTS = 3  # more unfinished matches than this from 2-10 days ago means results stopped arriving
EXCHANGE_DIR = Path(__file__).resolve().parents[1] / "data" / "exchange"
EXCHANGE_STALE = timedelta(days=3)


def exchange_problem(con: sqlite3.Connection, now: datetime, folder: Path = EXCHANGE_DIR) -> str | None:
    """Once the Betfair logger has run, flag it going quiet while matches are being played."""
    files = sorted(folder.glob("20??-??.csv"))
    if not files:
        return None  # not set up yet
    latest = ""
    for f in files[-2:]:
        with f.open(newline="") as fh:
            latest = max([latest] + [r["captured_at"] for r in csv.DictReader(fh)])
    since = latest or (now - EXCHANGE_STALE).strftime(ISO)  # matches played since the last logged price
    played = con.execute("SELECT COUNT(*) FROM matches WHERE kickoff_utc BETWEEN ? AND ?",
                         (since, now.strftime(ISO))).fetchone()[0]
    if latest < (now - EXCHANGE_STALE).strftime(ISO) and played:
        return (f"Betfair price log: nothing logged since {latest[:16].replace('T', ' ') or 'ever'} UTC while {played} matches were "
                "played. Check the 'Betfair prices' workflow in the Actions tab.")
    return None


def problems(db: str, log: str | None, now: datetime) -> list[str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = []
    lo, hi = (now - timedelta(days=10)).strftime(ISO), (now - timedelta(days=2)).strftime(ISO)
    for code, n in con.execute(
            """SELECT l.code, COUNT(*) FROM matches m JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
               WHERE m.status IN ('scheduled', 'postponed') AND m.kickoff_utc BETWEEN ? AND ?
               GROUP BY l.code""", (lo, hi)):
        if n > STALE_RESULTS:
            out.append(f"{code}: {n} matches from the last 10 days have no result. football-data may have stopped "
                       "updating, so prices use stale data.")
    stuck = con.execute("""SELECT COUNT(*) FROM value_bets vb JOIN odds_snapshots o USING (snapshot_id)
                           JOIN matches m ON m.match_id = o.match_id
                           WHERE vb.result = 'pending' AND m.kickoff_utc < ?""",
                        ((now - timedelta(days=4)).strftime(ISO),)).fetchone()[0]
    if stuck:
        out.append(f"{stuck} bet(s) still pending more than 4 days after kickoff (cards not loaded?).")
    for code, hours in REF_HOURS.items():  # referees are published before these windows; a gap means a feed broke
        n = con.execute(
            """SELECT COUNT(*) FROM matches m JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
               WHERE l.code = ? AND m.status = 'scheduled' AND m.referee_id IS NULL AND m.kickoff_utc BETWEEN ? AND ?""",
            (code, now.strftime(ISO), (now + timedelta(hours=hours)).strftime(ISO))).fetchone()[0]
        if n:
            out.append(f"{code}: {n} match(es) in the next {hours} hours have no referee. The referee feed may have "
                       "broken, so those prices use the league-average referee.")
    if msg := exchange_problem(con, now):
        out.append(msg)
    con.close()
    if log:
        seen: dict[str, int] = {}
        for line in open(log, errors="replace"):
            if LOG_PROBLEMS.search(line):
                key = re.sub(r"\s+", " ", line.split(" ", 2)[-1].strip())[:140]
                key = re.sub(r"(attempt|retry) \d+.*", "", key)
                seen[key] = seen.get(key, 0) + 1
        for key, n in list(seen.items())[:6]:
            out.append(f"Collector: {key}" + (f" (x{n})" if n > 1 else ""))
        if len(seen) > 6:
            out.append(f"...and {len(seen) - 6} more collector warnings (see the Actions log).")
    return out


def main() -> int:
    found = problems(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, datetime.now(timezone.utc))
    print("\n".join(found) if found else "OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
