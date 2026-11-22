#!/usr/bin/env python3
"""Pre-kickoff projection log: appends the model's projection for every upcoming league match to a CSV,
bet on or not, so the live model can be scored on matches it had never seen (agent/score_projections.py).

Run before kickoff (daily workflow, and the Saturday run). Each run appends one row per match kicking off
in the next --days; the scorer keeps the latest row logged before kickoff. The pmf column holds
P(0..MAX_K cards) in the book's card units, so any line can be scored later whatever the model was.
Usage: python agent/log_projections.py --db cards.db [--days 2] [--out data/projections.csv] [--book "Sky Bet"]
"""
import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collect_cards import DB  # noqa: E402
from price_sheet import book_weights, fixtures  # noqa: E402
from value_finder import ISO, Config, Projector  # noqa: E402

MAX_K = 20
FIELDS = ["logged_at", "match_id", "external_id", "league", "kickoff_utc", "home", "away", "referee",
          "ref_matches", "has_prices", "model_version", "book", "w_yellow", "w_second_yellow", "w_straight_red",
          "mu_home", "mu_away", "mu_total", "distribution", "pmf", "rho"]
# rho: the league's home/away card link (post-freeze test 2b, logged in shadow from 22 Nov 2026), so each-team
# lines can be scored later with the dependence model as well as the live independent one. Blank before then.


def rows(con: sqlite3.Connection, now: datetime, days: float, book: str) -> list[dict]:
    projector = Projector(con, Config(ref_fallback_league_avg=True), now)
    w = book_weights(con, book)
    ext = dict(con.execute("SELECT match_id, external_id FROM matches WHERE status = 'scheduled'").fetchall())
    out = []
    for fx in fixtures(con, now, days):
        priced = projector.price(fx, w)
        if priced is None:
            continue
        d = priced.detail
        out.append(dict(
            logged_at=now.strftime(ISO), match_id=fx["match_id"], external_id=ext.get(fx["match_id"]) or "",
            league=fx["league"], kickoff_utc=fx["kickoff_utc"], home=fx["home"], away=fx["away"],
            referee=fx["referee"] or "", ref_matches=fx["ref_matches"] if fx["referee_id"] is not None else "",
            has_prices=int(bool(d.get("has_prices"))) if "has_prices" in d else "",
            model_version=projector.version if d.get("model") == "glm" else "pcr-v1", book=book,
            w_yellow=w.yellow, w_second_yellow=w.second_yellow, w_straight_red=w.straight_red,
            mu_home=d.get("home_mu", ""), mu_away=d.get("away_mu", ""), mu_total=round(priced.rating, 3),
            distribution=d.get("distribution", ""),
            pmf=" ".join(f"{priced.pmf(k):.5f}" for k in range(MAX_K + 1)), rho=d.get("rho", "")))
    return out


def upgrade_header(path: str) -> None:
    """A log written before a column was added gets the new header, its old rows that column blank."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == FIELDS:
            return
        old = list(reader)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerows({k: r.get(k, "") or "" for k in FIELDS} for r in old)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--days", type=float, default=2)
    ap.add_argument("--out", default="data/projections.csv")
    ap.add_argument("--book", default="Sky Bet")
    args = ap.parse_args()
    db = DB(args.db, None)
    db.con.row_factory = sqlite3.Row
    try:
        new = rows(db.con, datetime.now(timezone.utc), args.days, args.book)
    finally:
        db.con.close()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    upgrade_header(args.out)
    fresh = not os.path.exists(args.out) or os.path.getsize(args.out) == 0
    with open(args.out, "a", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        if fresh:
            w.writeheader()
        w.writerows(new)
    print(f"logged {len(new)} projections to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
