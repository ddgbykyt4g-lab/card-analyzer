#!/usr/bin/env python3
"""
price_sheet: model prices for total-cards lines on upcoming fixtures, and manual bet logging.

For each scheduled match in the next --days, projects match cards with the PCR model from
value_finder and prices Over/Under lines (default 2.5 to 6.5 in 0.5 steps). Whole-number
lines can push; prices account for that:
    fair odds      = (1 - p_push) / p_win             (EV = 0)
    min odds +EV   = (1 + min_ev - p_push) / p_win    (EV = min_ev)
Matches with no appointed referee use the league average for the referee term and are
marked "provisional".

Usage
    python price_sheet.py --db cards.db                     # table per match
    python price_sheet.py --db cards.db --json
    python price_sheet.py --db cards.db --log-bet "Brighton v Man Utd over 4.5 @ 2.20 bet365 £10"
        (stake optional, defaults to 1 unit; records a bet the user placed, never places one)
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sqlite3
import sys
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collect_cards import DB, TeamResolver
from value_finder import (DEFAULT_WEIGHTS, ISO, MODEL_VERSION, Config, Stats, fit_pmf,
                          is_quarter_line, project, side_probs, weights_for)

log = logging.getLogger("price_sheet")
UK = ZoneInfo("Europe/London")
MARKET = dict(code="TOTAL_CARDS", unit="cards", scope="match", period="FT")
DEFAULT_LINES = [x / 2 for x in range(5, 14)]  # 2.5 .. 6.5


def fixtures(con: sqlite3.Connection, now: datetime, days: float) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT m.match_id, m.kickoff_utc, m.home_team_id, m.away_team_id, m.referee_id,
                  s.league_id, l.code AS league, h.name AS home, a.name AS away, r.name AS referee
           FROM matches m
           JOIN seasons s USING (season_id)
           JOIN leagues l USING (league_id)
           JOIN teams h ON h.team_id = m.home_team_id
           JOIN teams a ON a.team_id = m.away_team_id
           LEFT JOIN referees r USING (referee_id)
           WHERE m.status = 'scheduled' AND m.kickoff_utc > ? AND m.kickoff_utc <= ?
           ORDER BY m.kickoff_utc""",
        (now.strftime(ISO), (now + timedelta(days=days)).strftime(ISO))).fetchall()


def odds_for(p_win: float, p_push: float, ev: float) -> float | None:
    return (1 + ev - p_push) / p_win if p_win > 1e-9 else None


# ───────────────────────────── Price sheet ─────────────────────────────
def price_match(stats: Stats, cfg: Config, fx: sqlite3.Row, lines: list[float], min_ev: float) -> dict:
    w = DEFAULT_WEIGHTS["cards"]
    out = dict(match_id=fx["match_id"], kickoff_utc=fx["kickoff_utc"], league=fx["league"],
               fixture=f"{fx['home']} v {fx['away']}", referee=fx["referee"],
               provisional=fx["referee_id"] is None, projection=None, lines=[])
    proj = project(stats, cfg, fx, w)
    if proj is None:
        out["skipped"] = "not enough team or referee history"
        return out
    pmf, size, _ = fit_pmf(stats, cfg, fx["league_id"], w, fx["kickoff_utc"], proj.rating)
    out["projection"] = dict(cards=round(proj.rating, 3),
                             home_avg=round(proj.home_avg, 3), home_n=proj.home_n,
                             away_avg=round(proj.away_avg, 3), away_n=proj.away_n,
                             ref_avg=round(proj.ref_avg, 3), ref_n=proj.ref_n, ref_source=proj.ref_source,
                             trend=round(proj.trend, 4),
                             distribution=f"NB r={size:.1f}" if size else "Poisson")
    for line in lines:
        p_over, push = side_probs(pmf, line, "over")
        p_under, _ = side_probs(pmf, line, "under")
        out["lines"].append(dict(
            line=line, p_over=p_over, p_under=p_under, p_push=push,
            fair_over=odds_for(p_over, push, 0), fair_under=odds_for(p_under, push, 0),
            min_over=odds_for(p_over, push, min_ev), min_under=odds_for(p_under, push, min_ev)))
    return out


def _odds(x: float | None) -> str:
    return f"{x:7.2f}" if x is not None and x < 1000 else "      -"


def print_sheet(sheet: list[dict], min_ev: float) -> None:
    if not sheet:
        print("No scheduled matches in the window.")
        return
    tag = f"+{min_ev:.0%}"
    for m in sheet:
        ko = datetime.strptime(m["kickoff_utc"], ISO).replace(tzinfo=timezone.utc).astimezone(UK)
        status = "  PROVISIONAL (no referee, league avg)" if m["provisional"] else ""
        print(f"\n{ko:%a %d %b %H:%M} {m['league']}  {m['fixture']}  [ref: {m['referee'] or '-'}]{status}")
        p = m["projection"]
        if p is None:
            print(f"  skipped: {m['skipped']}")
            continue
        print(f"  Projected cards {p['cards']:.2f}   H {p['home_avg']:.2f}/{p['home_n']}  "
              f"A {p['away_avg']:.2f}/{p['away_n']}  R {p['ref_avg']:.2f}/{p['ref_n']} ({p['ref_source']})  "
              + (f"trend x{p['trend']:.3f}  " if p["trend"] != 1 else "") + f"{p['distribution']}")
        print(f"  {'Line':>4}  {'P(O)':>6} {'P(U)':>6} {'Push':>6}  {'FairO':>7}{'FairU':>7}"
              f"  {'MinO':>7}{'MinU':>7}   (Min = lowest odds for {tag} EV)")
        for r in m["lines"]:
            push = f"{r['p_push']:6.1%}" if r["p_push"] else "     -"
            print(f"  {r['line']:4.1f}  {r['p_over']:6.1%} {r['p_under']:6.1%} {push}  "
                  f"{_odds(r['fair_over'])}{_odds(r['fair_under'])}  "
                  f"{_odds(r['min_over'])}{_odds(r['min_under'])}")


# ───────────────────────────── Manual bet logging ─────────────────────────────
BET_RE = re.compile(
    r"^\s*(?P<home>.+?)\s+v(?:s\.?)?\s+(?P<away>.+?)\s+(?P<sel>over|under|o|u)\s*(?P<line>\d+(?:\.\d+)?)"
    r"\s*@\s*(?P<odds>\d+(?:\.\d+)?)\s+(?P<book>.+?)"
    r"(?:\s+(?P<stake>[£$€]?\s*\d+(?:\.\d+)?\s*(?:u|units?)?))?\s*$", re.I)
DEFAULT_STAKE = 1.0  # units, when no stake is given


def parse_bet(text: str) -> dict:
    m = BET_RE.match(text)
    if not m:
        raise SystemExit(f'Could not parse {text!r}; expected "Home v Away over 4.5 @ 2.20 bookmaker"')
    sel = m["sel"].lower()
    stake_text = (m["stake"] or "").strip()
    stake = float(re.sub(r"[^\d.]", "", stake_text)) if stake_text else DEFAULT_STAKE
    if stake <= 0:
        raise SystemExit("Stake must be positive")
    return dict(home=m["home"], away=m["away"], selection="over" if sel.startswith("o") else "under",
                line=float(m["line"]), odds=float(m["odds"]), bookmaker=m["book"],
                stake=stake, stake_text=stake_text or f"{DEFAULT_STAKE:g} unit")


def log_bet(db: DB, stats: Stats, cfg: Config, text: str, now: datetime, days: float, min_ev: float) -> dict:
    bet = parse_bet(text)
    if is_quarter_line(bet["line"]):
        raise SystemExit("Quarter lines (x.25/x.75) are not supported")
    if bet["odds"] <= 1:
        raise SystemExit("Odds must be decimal and above 1.00")
    con = db.con
    upcoming = fixtures(con, now, days)
    resolver = TeamResolver(db, {t for fx in upcoming for t in (fx["home_team_id"], fx["away_team_id"])})
    h = resolver.resolve(bet["home"], "manual")
    a = resolver.resolve(bet["away"], "manual")
    fx = next((f for f in upcoming if (f["home_team_id"], f["away_team_id"]) == (h, a)), None)
    if fx is None:
        raise SystemExit(f"No scheduled match {bet['home']} v {bet['away']} in the next {days:g} days")

    book_id = db.one("SELECT bookmaker_id FROM bookmakers WHERE name = ? COLLATE NOCASE", bet["bookmaker"])
    if book_id is None:
        book_id = con.execute("INSERT INTO bookmakers (name) VALUES (?)", (bet["bookmaker"],)).lastrowid
    con.execute("INSERT OR IGNORE INTO markets (code, unit, scope, period) VALUES (?,?,?,?)",
                tuple(MARKET.values()))
    market_id = db.one("SELECT market_id FROM markets WHERE code = ?", MARKET["code"])

    w = weights_for(con, book_id, market_id, MARKET["unit"])
    proj = project(stats, cfg, fx, w)
    if proj is None:
        con.rollback()
        raise SystemExit("Not enough history to model this match; bet not logged")
    pmf, size, scale = fit_pmf(stats, cfg, fx["league_id"], w, fx["kickoff_utc"], proj.rating)
    p_win, p_push = side_probs(pmf, bet["line"] / scale, bet["selection"])
    ev = p_win * bet["odds"] + p_push - 1
    b = bet["odds"] - 1
    kelly = max(0.0, (b * p_win - (1 - p_win - p_push)) / b)

    captured = now.strftime(ISO)
    side = "over_odds" if bet["selection"] == "over" else "under_odds"
    snap_id = con.execute(
        f"INSERT INTO odds_snapshots (match_id, bookmaker_id, market_id, line, {side}, captured_at) "
        "VALUES (?,?,?,?,?,?)", (fx["match_id"], book_id, market_id, bet["line"], bet["odds"], captured)).lastrowid
    provisional = fx["referee_id"] is None
    notes = (f"manual entry: {text.strip()} | stake {bet['stake_text']} (reported by user) | H {proj.home_avg:.2f}/{proj.home_n} A {proj.away_avg:.2f}/{proj.away_n} "
             f"R {proj.ref_avg:.2f}/{proj.ref_n} ({proj.ref_source}) "
             f"dist={'NB r=%.1f' % size if size else 'Poisson'}" + (" | provisional" if provisional else ""))
    bet_id = con.execute(
        """INSERT INTO value_bets (snapshot_id, selection, model_version, expected_mean, model_prob,
               model_push_prob, taken_odds, stake, kelly_fraction, placed_at, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (snap_id, bet["selection"], MODEL_VERSION, proj.rating, p_win, p_push, bet["odds"], bet["stake"],
         kelly, captured, notes)).lastrowid
    con.commit()
    return dict(bet_id=bet_id, snapshot_id=snap_id, fixture=f"{fx['home']} v {fx['away']}",
                kickoff_utc=fx["kickoff_utc"], referee=fx["referee"], provisional=provisional,
                bookmaker=bet["bookmaker"], selection=bet["selection"], line=bet["line"], odds=bet["odds"],
                stake=bet["stake"], stake_text=bet["stake_text"],
                projected_cards=proj.rating, model_prob=p_win, push_prob=p_push,
                fair_odds=odds_for(p_win, p_push, 0), min_odds=odds_for(p_win, p_push, min_ev),
                ev=ev, kelly=kelly, meets_min_ev=ev >= min_ev)


def print_logged(r: dict) -> None:
    print(f"Logged bet #{r['bet_id']} (stake {r['stake_text']}): {r['fixture']} {r['selection']} {r['line']} "
          f"@ {r['odds']:.2f} {r['bookmaker']}" + ("  [provisional]" if r["provisional"] else ""))
    print(f"  projected {r['projected_cards']:.2f}  model {r['model_prob']:.1%}"
          + (f" (push {r['push_prob']:.1%})" if r["push_prob"] else "")
          + f"  fair {_odds(r['fair_odds']).strip()}  EV {r['ev']:+.1%}  Kelly {r['kelly']:.1%}"
          + ("" if r["meets_min_ev"] else "  -- below the EV threshold"))


# ───────────────────────────── CLI ─────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Total-cards price sheet and manual bet logging")
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--lines", type=float, nargs="+", default=DEFAULT_LINES)
    ap.add_argument("--min-ev", type=float, default=Config.min_ev, help="0.05 = 5%%")
    ap.add_argument("--team-lookback", type=int, default=Config.team_lookback)
    ap.add_argument("--ref-lookback", type=int, default=Config.ref_lookback)
    ap.add_argument("--min-team-matches", type=int, default=Config.min_team_matches)
    ap.add_argument("--min-ref-matches", type=int, default=Config.min_ref_matches)
    ap.add_argument("--no-venue-split", dest="venue_split", action="store_false")
    ap.add_argument("--distribution", choices=["negbin", "poisson"], default=Config.distribution)
    ap.add_argument("--team-prior", type=float, default=Config.team_prior)
    ap.add_argument("--ref-prior", type=float, default=Config.ref_prior)
    ap.add_argument("--trend-recent", type=int, default=Config.trend_recent)
    ap.add_argument("--trend-base", type=int, default=Config.trend_base)
    ap.add_argument("--trend-prior", type=float, default=Config.trend_prior)
    ap.add_argument("--log-bet", metavar="TEXT", help='e.g. "Brighton v Man Utd over 4.5 @ 2.20 bet365 £10" (stake defaults to 1 unit)')
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s", stream=sys.stderr)

    bad = [x for x in args.lines if is_quarter_line(x)]
    if bad:
        ap.error(f"quarter lines not supported: {bad}")
    opts = {f.name: getattr(args, f.name) for f in fields(Config) if hasattr(args, f.name)}
    cfg = Config(**opts, ref_fallback_league_avg=True)  # no referee yet -> league average

    db = DB(args.db, None)
    db.con.row_factory = sqlite3.Row
    stats = Stats(db.con, cfg)
    now = datetime.now(timezone.utc)
    try:
        if args.log_bet:
            r = log_bet(db, stats, cfg, args.log_bet, now, args.days, args.min_ev)
            if args.json:
                print(json.dumps(r, indent=2))
            else:
                print_logged(r)
            return 0
        sheet = [price_match(stats, cfg, fx, sorted(args.lines), args.min_ev)
                 for fx in fixtures(db.con, now, args.days)]
        if args.json:
            print(json.dumps({"generated_at": now.strftime(ISO), "min_ev": args.min_ev,
                              "matches": sheet}, indent=2))
        else:
            print_sheet(sheet, args.min_ev)
    finally:
        db.con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
