#!/usr/bin/env python3
"""Records bookmaker card prices read off screenshots into odds_snapshots, so the model can later be
compared with the market on every logged match, bet on or not.

Input file, one block per screen:
    @ 2026-10-24 10:28 | Valencia v Villarreal | total        (UK time | fixture | total, home or away)
    @ 2026-10-24 10:30 | Valencia v Villarreal | total | bet365   (optional 4th field: the bookmaker; else --book)
    3.5 1/8 4/1          line, over odds, under odds ("-" where a price is missing)
    4+ 2/5               "4 or more cards" screens: over-only, stored as line 3.5
Odds are fractional or decimal. Re-running a file is harmless (duplicates are ignored).
A block whose match can't be found within 7 days of its time, or that is timed after kickoff, is skipped
with a SKIPPED line; the rest of the file is still loaded.
Usage: python agent/log_prices.py --db cards.db prices.txt [--book "Sky Bet"]
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collect_cards import DB  # noqa: E402
from price_sheet import MARKETS, find_match, parse_odds  # noqa: E402
from value_finder import ISO  # noqa: E402

UK = ZoneInfo("Europe/London")
DAYS_AHEAD = 7  # how far after the screenshot the match may be (check_prices and experiment A's pick: 7)
HEADER = re.compile(r"^@\s*(?P<when>\d{4}-\d\d-\d\d \d\d:\d\d)\s*\|\s*(?P<home>.+?)\s+v\s+(?P<away>.+?)\s*\|\s*"
                    r"(?P<market>total|home|away)(?:\s*\|\s*(?P<book>[^|]+?))?\s*$", re.I)


def parse_price(text: str) -> float | None:
    return None if text.strip() in ("-", "") else parse_odds(text.strip())


def parse_row(text: str) -> tuple[float, float | None, float | None]:
    """'3.5 1/8 4/1' -> (3.5, 1.125, 5.0); '4+ 2/5' -> (3.5, 1.4, None)."""
    parts = text.split()
    if parts[0].endswith("+"):
        return int(parts[0][:-1]) - 0.5, parse_price(parts[1]), None
    return float(parts[0]), parse_price(parts[1]), parse_price(parts[2]) if len(parts) > 2 else None


def parse_file(text: str) -> list[dict]:
    blocks, cur = [], None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if m := HEADER.match(line):
            when = datetime.strptime(m["when"], "%Y-%m-%d %H:%M").replace(tzinfo=UK).astimezone(timezone.utc)
            cur = dict(when=when, home=m["home"].strip(), away=m["away"].strip(), market=m["market"].lower(), rows=[],
                       book=(m["book"] or "").strip() or None)
            blocks.append(cur)
        elif cur is None:
            raise SystemExit(f"Price row before any '@ ...' header: {raw!r}")
        else:
            cur["rows"].append(parse_row(line))
    return blocks


def market_id(con: sqlite3.Connection, kind: str) -> int:
    m = MARKETS[kind]
    con.execute("INSERT OR IGNORE INTO markets (code, unit, scope, period) VALUES (?, ?, ?, ?)",
                (m["code"], m["unit"], m["scope"], m["period"]))
    return con.execute("SELECT market_id FROM markets WHERE code = ?", (m["code"],)).fetchone()[0]


def log_blocks(db: DB, blocks: list[dict], book: str) -> int:
    con = db.con
    ids: dict[str, int] = {}

    def book_id(name: str) -> int:
        if name not in ids:
            row = con.execute("SELECT bookmaker_id, name FROM bookmakers WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            ids[name] = row[0] if row else con.execute("INSERT INTO bookmakers (name) VALUES (?)", (name,)).lastrowid
            if not con.execute("SELECT 1 FROM settlement_rules WHERE bookmaker_id = ?", (ids[name],)).fetchone():
                print(f"NOTE: no card-counting rules for {name} yet: its prices are stored but check_prices won't "
                      f"price them until they're added (agent/add_book_rules.py)")
        return ids[name]

    added = 0
    for b in blocks:
        try:  # one block that can't be placed (a typo, a postponed match) mustn't stop the rest of the file
            fx = find_match(db, dict(home=b["home"], away=b["away"]), b["when"], DAYS_AHEAD, b["when"])
        except SystemExit as exc:
            print(f"SKIPPED {b['home']} v {b['away']} {b['market']} at {b['when']:%Y-%m-%d %H:%M} UTC: {exc}")
            continue
        if fx["kickoff_utc"] <= b["when"].strftime(ISO):
            print(f"SKIPPED {fx['home']} v {fx['away']}: kicked off before {b['when']:%Y-%m-%d %H:%M} UTC")
            continue
        mid = market_id(con, b["market"])
        for line, over, under in b["rows"]:
            if over is None and under is None:
                continue
            cur = con.execute("""INSERT OR IGNORE INTO odds_snapshots
                                 (match_id, bookmaker_id, market_id, line, over_odds, under_odds, captured_at)
                                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
                              (fx["match_id"], book_id(b.get("book") or book), mid, line, over, under,
                               b["when"].strftime(ISO)))
            added += cur.rowcount
        print(f"{fx['home']} v {fx['away']} {b['market']}: {len(b['rows'])} lines at {b['when']:%Y-%m-%d %H:%M} UTC")
    con.commit()
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--book", default="Sky Bet")
    args = ap.parse_args()
    db = DB(args.db, None)
    db.con.row_factory = sqlite3.Row
    added = log_blocks(db, parse_file(open(args.file).read()), args.book)
    print(f"{added} new price rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
