#!/usr/bin/env python3
"""One-off: fill missing referees for past seasons from ESPN match summaries, on a COPY of the database,
saving progress as CSV so an interrupted run loses at most one league-season.

ESPN summaries list the referee from about April 2022 (the collector's ESPN_FIRST_MONTH assumes August
2026). Every checked match is written to the CSV (referee blank if ESPN had none), and `apply` marks those
matches 'espn:<id>' in the copy, so the next `fill` skips them.

  python agent/backfill_referees.py apply  COPY.db CSV               # load earlier progress into the copy
  python agent/backfill_referees.py fill   COPY.db LEAGUE:SEASON     # check one league-season on ESPN
  python agent/backfill_referees.py export COPY.db CSV LEAGUE:SEASON ...
"""
import asyncio
import csv
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collect_cards as cc  # noqa: E402

FIRST_MONTH = "2022-04"
FIELDS = ["league", "season", "kickoff_utc", "home", "away", "referee", "country", "external_id"]


async def fill(path: str, code: str, season: str) -> None:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth
    cc.ESPN_FIRST_MONTH = FIRST_MONTH
    db = cc.DB(path, None)
    async with Stealth().use_async(async_playwright()) as p:
        proxy = os.environ.get("HTTPS_PROXY")
        browser = await p.chromium.launch(headless=True, proxy={"server": proxy} if proxy else None)
        ctx = await browser.new_context(locale="en-GB")
        await ctx.route("**/*", cc._block_heavy)
        try:
            await cc.backfill_referees(ctx, db, code, season)
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
               LEFT JOIN referees r USING (referee_id)
               WHERE l.code = ? AND s.label = ? AND m.status = 'finished' AND m.external_id LIKE 'espn:%'
               ORDER BY m.kickoff_utc""", (code, season)).fetchall()
    con.close()
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        w.writerows(rows)
    return len(rows)


def apply(path: str, src: str) -> tuple[int, int]:
    """Write CSV rows into the copy, matched on league, season, teams and kickoff."""
    db = cc.DB(path, None)
    done = refs = 0
    for r in csv.DictReader(open(src)):
        mid = db.one("""SELECT m.match_id FROM matches m JOIN seasons s USING (season_id)
                        JOIN leagues l USING (league_id)
                        JOIN teams h ON h.team_id = m.home_team_id JOIN teams a ON a.team_id = m.away_team_id
                        WHERE l.code = ? AND s.label = ? AND h.name = ? AND a.name = ? AND m.kickoff_utc = ?""",
                     r["league"], r["season"], r["home"], r["away"], r["kickoff_utc"])
        if mid is None:
            continue
        ref_id = db.referee_id(r["referee"], r["country"], "espn") if r["referee"] else None
        db.con.execute("UPDATE matches SET external_id = ?, referee_id = COALESCE(?, referee_id) WHERE match_id = ?",
                       (r["external_id"], ref_id, mid))
        done += 1
        refs += ref_id is not None
    db.commit()
    db.con.close()
    return done, refs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cmd, db_path, *rest = sys.argv[1:]
    if cmd == "apply":
        print("applied %d checked matches (%d with a referee)" % apply(db_path, rest[0]))
    elif cmd == "fill":
        asyncio.run(fill(db_path, *rest[0].split(":")))
    elif cmd == "export":
        print(f"{export(db_path, rest[0], [tuple(a.split(':')) for a in rest[1:]])} checked matches written")
    else:
        raise SystemExit(__doc__)
