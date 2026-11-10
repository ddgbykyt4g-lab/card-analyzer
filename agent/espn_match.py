#!/usr/bin/env python3
"""Line-ups and the running card count for one match, from ESPN's public JSON: for checking a team
sheet before a bet, and following a bet in play. Read-only; talks to ESPN only, never a bookmaker.

Cards are counted the way Sky Bet settles: player cards only (a booked manager or coach is listed
apart and doesn't count), a second yellow as one more card, a straight red as one.
Usage: python agent/espn_match.py "Leeds v Tottenham" [--db cards.db] [--lineups]
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collect_cards import DB, ESPN_BASE, HOST_INTERVAL, LEAGUES, espn_card_counts  # noqa: E402
from price_sheet import find_match  # noqa: E402

GAP = HOST_INTERVAL["site.api.espn.com"]  # same pause between requests as the collector


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def card_lines(comp: dict, names: dict[str, str]) -> list[str]:
    """names: {'home': ..., 'away': ...}. One line per team for player cards, then staff cards, then the total."""
    counts = espn_card_counts(comp)
    if counts is None:
        return ["ESPN has no card events for this match."]
    side = {c["team"]["id"]: c["homeAway"] for c in comp["competitors"]}
    players = {ha: [] for ha in names}
    staff = {ha: [] for ha in names}
    for d in comp.get("details") or []:
        ha = side.get((d.get("team") or {}).get("id"))
        if ha is None or not (d.get("yellowCard") or d.get("redCard")):
            continue
        minute = (d.get("clock") or {}).get("displayValue", "?")
        who = ", ".join(a.get("displayName", "?") for a in d.get("athletesInvolved") or [])
        (players if who else staff)[ha].append(f"{minute} {who}{' (red)' if d.get('redCard') else ''}".strip())
    out = [f"{names[ha]}: {counts[ha]['yellows'] + counts[ha]['reds']} player cards"
           + (f" ({'; '.join(players[ha])})" if players[ha] else "") for ha in ("home", "away")]
    out += [f"{names[ha]} staff card, doesn't count: {'; '.join(staff[ha])}" for ha in ("home", "away") if staff[ha]]
    out.append(f"Total that counts: {sum(c['yellows'] + c['reds'] for c in counts.values())}")
    return out


def lineup_lines(summary: dict) -> list[str]:
    out = []
    for r in summary.get("rosters") or []:
        name = lambda p: (p.get("athlete") or {}).get("displayName", "?")
        start = [name(p) for p in r.get("roster") or [] if p.get("starter")]
        bench = [name(p) for p in r.get("roster") or [] if not p.get("starter")]
        if start:
            out += [f"{(r.get('team') or {}).get('displayName', '?')} ({r.get('formation', '?')}): {', '.join(start)}",
                    f"  bench: {', '.join(bench)}"]
    return out or ["Line-ups aren't out yet (usually an hour before kickoff)."]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fixture", help='"Home v Away", a match within two days either side of now')
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--lineups", action="store_true", help="also print the starting elevens and benches")
    args = ap.parse_args()
    home, _, away = args.fixture.partition(" v ")
    db = DB(args.db, None)
    db.con.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    fx = find_match(db, dict(home=home.strip(), away=away.strip()), now, 4, now - timedelta(days=2))
    ext = db.con.execute("SELECT external_id FROM matches WHERE match_id = ?", (fx["match_id"],)).fetchone()[0]
    if not (ext or "").startswith("espn"):
        raise SystemExit(f"{fx['home']} v {fx['away']} has no ESPN id in the database yet")
    base, eid = f"{ESPN_BASE}/{LEAGUES[fx['league']]['espn_slug']}", ext.rsplit(":", 1)[-1]
    board = fetch(f"{base}/scoreboard?dates={fx['kickoff_utc'][:10].replace('-', '')}")
    comp = next((ev["competitions"][0] for ev in board.get("events", []) if str(ev["id"]) == eid), None)
    if comp is None:
        raise SystemExit(f"ESPN doesn't list {fx['home']} v {fx['away']} on {fx['kickoff_utc'][:10]}")
    score = {c["homeAway"]: c.get("score", "0") for c in comp["competitors"]}
    print(f"{fx['home']} {score['home']}-{score['away']} {fx['away']} · {comp['status']['type']['shortDetail']}")
    print("\n".join(card_lines(comp, dict(home=fx["home"], away=fx["away"]))))
    time.sleep(GAP)
    summary = fetch(f"{base}/summary?event={eid}")
    fouls = {t["team"]["id"]: next((s["displayValue"] for s in t.get("statistics", []) if s["name"] == "foulsCommitted"), "?")
             for t in (summary.get("boxscore") or {}).get("teams", [])}
    if fouls:
        ids = {c["homeAway"]: c["team"]["id"] for c in comp["competitors"]}
        print(f"Fouls: {fx['home']} {fouls.get(ids['home'], '?')}, {fx['away']} {fouls.get(ids['away'], '?')}")
    if args.lineups:
        print("\n".join(lineup_lines(summary)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
