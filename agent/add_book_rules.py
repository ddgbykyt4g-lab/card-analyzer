#!/usr/bin/env python3
"""Enters a bookmaker's card-counting rules, once, so its prices can be checked against the model.

Bookmakers count cards differently, and the model is refitted in each book's units, so a wrong rule misprices
every line. Take the figures from the bookmaker's own rules page for card markets, never from memory:
  --yellow          what a yellow card counts (1 for "cards" markets)
  --second-yellow   what a second yellow (the sending-off itself) adds on top of the player's first yellow
  --red             what a straight red counts
  --staff           if cards shown to managers and coaches count (Sky Bet: they don't)
Sky Bet, for comparison: yellow 1, second yellow 1, straight red 1, no staff cards.
The same rule is stored for the match-total, home, away and each-team card markets. Re-running replaces it.
Usage: python agent/add_book_rules.py --db cards.db "bet365" --yellow 1 --second-yellow 1 --red 2
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from price_sheet import MARKETS  # noqa: E402


def add_rules(con: sqlite3.Connection, book: str, yellow: float, second_yellow: float, red: float,
              staff: bool = False) -> int:
    if min(yellow, second_yellow, red) < 0 or yellow == 0:
        raise SystemExit("Card values must be positive (a yellow at least 1)")
    row = con.execute("SELECT bookmaker_id FROM bookmakers WHERE name = ? COLLATE NOCASE", (book,)).fetchone()
    book_id = row[0] if row else con.execute("INSERT INTO bookmakers (name) VALUES (?)", (book,)).lastrowid
    for m in MARKETS.values():
        con.execute("INSERT OR IGNORE INTO markets (code, unit, scope, period) VALUES (?, ?, ?, ?)",
                    (m["code"], m["unit"], m["scope"], m["period"]))
        con.execute("""INSERT OR REPLACE INTO settlement_rules
                           (bookmaker_id, market_id, w_yellow, w_second_yellow, w_straight_red, counts_staff)
                       SELECT ?, market_id, ?, ?, ?, ? FROM markets WHERE code = ?""",
                    (book_id, yellow, second_yellow, red, int(staff), m["code"]))
    con.commit()
    return book_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("book")
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--yellow", type=float, required=True)
    ap.add_argument("--second-yellow", type=float, required=True)
    ap.add_argument("--red", type=float, required=True)
    ap.add_argument("--staff", action="store_true", help="managers' and coaches' cards count")
    args = ap.parse_args()
    con = sqlite3.connect(args.db)
    con.execute("PRAGMA foreign_keys = ON")
    add_rules(con, args.book, args.yellow, args.second_yellow, args.red, args.staff)
    print(f"{args.book}: yellow {args.yellow:g}, second yellow {args.second_yellow:g}, straight red {args.red:g}, "
          f"staff cards {'count' if args.staff else 'do not count'} (total, home, away and each-team markets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
