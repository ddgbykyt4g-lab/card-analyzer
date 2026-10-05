#!/usr/bin/env python3
"""One-off: fill missing referees for past seasons from ESPN match summaries, on a COPY of the database,
and export them as CSV for review before anything goes live.

ESPN summaries list the referee from about April 2022 (the collector's ESPN_FIRST_MONTH assumes August
2026). Checked matches get external_id 'espn:<id>', so an interrupted run resumes where it stopped.
Usage: python agent/backfill_referees.py COPY.db OUT.csv LEAGUE:SEASON ...
"""
import asyncio
import csv
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collect_cards as cc  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from playwright_stealth import Stealth  # noqa: E402

FIRST_MONTH = "2022-04"


async def run(path: str, pairs: list[tuple[str, str]]) -> None:
    cc.ESPN_FIRST_MONTH = FIRST_MONTH
    db = cc.DB(path, None)
    async with Stealth().use_async(async_playwright()) as p:
        proxy = os.environ.get("HTTPS_PROXY")
        browser = await p.chromium.launch(headless=True, proxy={"server": proxy} if proxy else None)
        ctx = await browser.new_context(locale="en-GB")
        await ctx.route("**/*", cc._block_heavy)
        try:
            await cc._gather_logged("ESPN referees", (cc.backfill_referees(ctx, db, c, s) for c, s in pairs))
        finally:
            await browser.close()
    db.con.close()


def export(path: str, out: str, pairs: list[tuple[str, str]]) -> int:
    con = cc.sqlite3.connect(path)
    rows = []
    for code, season in pairs:
        rows += con.execute(
            """SELECT l.code, s.label, m.kickoff_utc, h.name, a.name, r.name, r.country, m.external_id
               FROM matches m JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
               JOIN teams h ON h.team_id = m.home_team_id JOIN teams a ON a.team_id = m.away_team_id
               JOIN referees r USING (referee_id)
               WHERE l.code = ? AND s.label = ? AND m.status = 'finished'
               ORDER BY m.kickoff_utc""", (code, season)).fetchall()
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["league", "season", "kickoff_utc", "home", "away", "referee", "country", "external_id"])
        w.writerows(rows)
    return len(rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db_path, out_csv, *args = sys.argv[1:]
    pairs = [tuple(a.split(":")) for a in args]
    asyncio.run(run(db_path, pairs))
    print(f"{export(db_path, out_csv, pairs)} matches with a referee written to {out_csv}")
