#!/usr/bin/env python3
"""
price_sheet: model prices for total-cards lines on upcoming fixtures, and manual bet logging.

For each scheduled match in the next --days, projects match cards with the model from
value_finder, counting cards the way --book (default Sky Bet) settles them, and prices Over/Under
lines (default 2.5 to 6.5 in 0.5 steps). Whole-number lines can push; prices account for that:
    fair odds      = (1 - p_push) / p_win             (EV = 0)
    min odds +EV   = (1 + min_ev - p_push) / p_win    (EV = min_ev)
Matches with no appointed referee use the league average for the referee term and are
marked "provisional". Each match shows the referee's past matches with card data; under
MIN_REF_MATCHES it is flagged "thin referee history".

Usage
    python price_sheet.py --db cards.db                     # table per match
    python price_sheet.py --db cards.db --json
    python price_sheet.py --db cards.db --log-bet "Brighton v Man Utd over 4.5 @ 2.20 bet365 £10"
        (stake optional, defaults to 1 unit; records a bet the user placed, never places one)
"""
from __future__ import annotations

import argparse
import json
import math
import logging
import re
import sqlite3
import sys
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collect_cards import DB, TeamResolver
from settle import ESPN_CARDS, read_espn_cards, settle
from value_finder import (DEFAULT_WEIGHTS, ISO, Config, Projector, Weights, is_quarter_line, side_probs,
                          weights_for)

log = logging.getLogger("price_sheet")
UK = ZoneInfo("Europe/London")
MARKET = dict(code="TOTAL_CARDS", unit="cards", scope="match", period="FT")
DEFAULT_LINES = [x / 2 for x in range(5, 14)]  # 2.5 .. 6.5
TEAM_LINES = (0.5, 1.5, 2.5, 3.5)
MIN_REF_MATCHES = 10  # below this the referee's average is mostly noise


def fixtures(con: sqlite3.Connection, now: datetime, days: float) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT m.match_id, m.kickoff_utc, m.home_team_id, m.away_team_id, m.referee_id,
                  s.league_id, l.code AS league, h.name AS home, a.name AS away, r.name AS referee,
                  (SELECT COUNT(*) FROM matches p
                   WHERE p.referee_id = m.referee_id AND p.status = 'finished' AND p.kickoff_utc < m.kickoff_utc
                     AND EXISTS (SELECT 1 FROM match_team_cards c WHERE c.match_id = p.match_id)) AS ref_matches
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
def book_weights(con: sqlite3.Connection, book: str) -> Weights:
    """The book's TOTAL_CARDS settlement rule, or the default card weights if it has none."""
    row = con.execute("""SELECT r.w_yellow, r.w_second_yellow, r.w_straight_red FROM settlement_rules r
                         JOIN bookmakers b USING (bookmaker_id) JOIN markets m USING (market_id)
                         WHERE b.name = ? COLLATE NOCASE AND m.code = 'TOTAL_CARDS'""", (book,)).fetchone()
    if row is None:
        log.warning("No TOTAL_CARDS settlement rule for %r; pricing with default card weights", book)
        return DEFAULT_WEIGHTS["cards"]
    return Weights(*row)


def price_match(projector: Projector, fx: sqlite3.Row, lines: list[float], min_ev: float,
                w: Weights = DEFAULT_WEIGHTS["cards"]) -> dict:
    out = dict(match_id=fx["match_id"], kickoff_utc=fx["kickoff_utc"], league=fx["league"],
               fixture=f"{fx['home']} v {fx['away']}", referee=fx["referee"],
               provisional=fx["referee_id"] is None,
               ref_matches=fx["ref_matches"] if fx["referee_id"] is not None else None,
               thin_ref=fx["referee_id"] is not None and fx["ref_matches"] < MIN_REF_MATCHES,
               projection=None, lines=[])
    priced = projector.price(fx, w)
    if priced is None:
        out["skipped"] = "not enough team or referee history"
        return out
    pmf = priced.pmf
    out["projection"] = dict(cards=round(priced.rating, 3), **priced.detail)
    if priced.detail.get("model") == "glm":  # per-team lines (Poisson on each team's expected cards)
        out["team_lines"] = {
            side: [dict(line=tl, p_over=(po := poisson_tail(priced.detail[f"{side}_mu"], tl, "over")[0]),
                        fair_over=odds_for(po, 0, 0), min_over=odds_for(po, 0, min_ev),
                        fair_under=odds_for(1 - po, 0, 0), min_under=odds_for(1 - po, 0, min_ev))
                   for tl in TEAM_LINES]
            for side in ("home", "away")}
    for line in lines:  # the pmf is in count units, like value_finder.evaluate and log_bet
        p_over, push = side_probs(pmf, line / priced.scale, "over")
        p_under, _ = side_probs(pmf, line / priced.scale, "under")
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
        ref = f"{m['referee']}, {m['ref_matches']} past" if m["referee"] else "-"
        thin = f"  THIN REFEREE HISTORY (<{MIN_REF_MATCHES})" if m["thin_ref"] else ""
        print(f"\n{ko:%a %d %b %H:%M} {m['league']}  {m['fixture']}  [ref: {ref}]{status}{thin}")
        p = m["projection"]
        if p is None:
            print(f"  skipped: {m['skipped']}")
            continue
        if p["model"] == "glm":
            print(f"  Projected cards {p['cards']:.2f}   GLM: home {p['home_mu']:.2f}  away {p['away_mu']:.2f}  "
                  + ("" if p["has_prices"] else "(no 1X2 prices)  ") + p["distribution"])
        else:
            print(f"  Projected cards {p['cards']:.2f}   PCR: H {p['home_avg']:.2f}/{p['home_n']}  "
                  f"A {p['away_avg']:.2f}/{p['away_n']}  R {p['ref_avg']:.2f}/{p['ref_n']} ({p['ref_source']})  "
                  + (f"trend x{p['trend']:.3f}  " if p["trend"] != 1 else "") + p["distribution"])
        print(f"  {'Line':>4}  {'P(O)':>6} {'P(U)':>6} {'Push':>6}  {'FairO':>7}{'FairU':>7}"
              f"  {'MinO':>7}{'MinU':>7}   (Min = lowest odds for {tag} EV)")
        for r in m["lines"]:
            push = f"{r['p_push']:6.1%}" if r["p_push"] else "     -"
            print(f"  {r['line']:4.1f}  {r['p_over']:6.1%} {r['p_under']:6.1%} {push}  "
                  f"{_odds(r['fair_over'])}{_odds(r['fair_under'])}  "
                  f"{_odds(r['min_over'])}{_odds(r['min_under'])}")
        home, away = m["fixture"].split(" v ", 1)
        for side, name in (("home", home), ("away", away)):
            if side in m.get("team_lines", {}):
                cells = "  ".join(f"o{t['line']:.1f} {t['p_over']:4.0%} min {_odds(t['min_over']).strip()}"
                                  for t in m["team_lines"][side])
                print(f"  {name[:16]:16} {cells}")


# ───────────────────────────── Manual bet logging ─────────────────────────────
# Markets: match total (no team), one team's cards ("Rangers over 2.5"), each team ("each team 2+").
MARKETS = {
    "total": dict(code="TOTAL_CARDS", unit="cards", scope="match", period="FT"),
    "home": dict(code="HOME_CARDS", unit="cards", scope="home", period="FT"),
    "away": dict(code="AWAY_CARDS", unit="cards", scope="away", period="FT"),
    "each": dict(code="EACH_TEAM_CARDS", unit="cards", scope="match", period="FT"),  # settles on min/max team
}
MARKET = MARKETS["total"]
ODDS = r"(?P<odds>\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)"
SEL = r"(?P<sel>over|under|o|u)\s*(?P<line>\d+(?:\.\d+)?)|(?P<plus>\d+)\s*\+"
BET_RE = re.compile(  # "Home v Away[: market] over 4.5 @ 2.20 book [£10]"
    rf"^\s*(?P<home>.+?)\s+v(?:s\.?)?\s+(?P<away>[^:]+?)\s*(?::\s*(?P<target>.+?)\s+|\s+)(?:{SEL})"
    rf"\s*@\s*{ODDS}\s+(?P<book>.+?)(?:\s+(?P<stake>[£$€]?\s*\d+(?:\.\d+)?\s*(?:u|units?)?))?\s*$", re.I)
DEFAULT_STAKE = 1.0  # units, when no stake is given


def parse_odds(text: str) -> float:
    if "/" in text:
        num, den = text.split("/")
        return 1 + float(num) / float(den)
    return float(text)


def parse_prob(text: str | None) -> float | None:
    if not text or not text.strip() or text.strip() in ("-", "n/a"):
        return None
    v = float(text.strip().rstrip("%"))
    return v / 100 if v > 1 else v


def parse_bet(text: str) -> dict:
    """Free text ("Aberdeen v Rangers: Rangers over 2.5 @ 5/2 Sky Bet £10") or the prompt's
    pipe line ("Home v Away | market | over 2.5 @ 3.50 | bookmaker | model p | adjusted p | reason")."""
    extra = {}
    if "|" in text:
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 4:
            raise SystemExit("Pipe format: Home v Away | market | over 2.5 @ 3.50 | bookmaker | model p | adjusted p | reason")
        fixture, market, selection, book = parts[:4]
        extra = dict(adjusted_p=parse_prob(parts[5]) if len(parts) > 5 else None,
                     reason=" | ".join(parts[6:]) or None)
        target = "" if market.lower() in ("", "total", "match", "total cards", "match cards") else f": {market}"
        text = f"{fixture}{target} {selection} {book}"
    m = BET_RE.match(text)
    if not m:
        raise SystemExit(f'Could not parse {text!r}; expected e.g. "Home v Away over 4.5 @ 2.20 bet365 £10", '
                         '"Home v Away: Away Team over 2.5 @ 5/2 Sky Bet" or "Home v Away: each team 2+ @ 15/8 Sky Bet"')
    if m["plus"]:
        selection, line = "over", int(m["plus"]) - 0.5
    else:
        selection, line = ("over" if m["sel"].lower().startswith("o") else "under"), float(m["line"])
    target = (m["target"] or "").strip()
    home, away = m["home"].strip(), m["away"].strip()
    if not target:  # "Genoa v Fiorentina Fiorentina over 2.5": the team was named without the colon
        words = away.split()
        for k in range(1, len(words)):
            head, tail = " ".join(words[:k]), " ".join(words[k:])
            if tail.lower() in (home.lower(), head.lower()):
                away, target = head, tail
                break
    kind = "total" if not target or target.lower() in ("match", "total") else \
        "each" if target.lower().startswith("each") else "team"
    stake_text = (m["stake"] or "").strip()
    stake = float(re.sub(r"[^\d.]", "", stake_text)) if stake_text else DEFAULT_STAKE
    if stake <= 0:
        raise SystemExit("Stake must be positive")
    return dict(home=home, away=away, kind=kind, team=target if kind == "team" else None,
                selection=selection, line=line, odds=parse_odds(m["odds"]), bookmaker=m["book"].strip(),
                stake=stake, stake_text=stake_text or f"{DEFAULT_STAKE:g} unit", **extra)


def find_match(db: DB, bet: dict, now: datetime, days: float, placed: datetime | None) -> sqlite3.Row:
    """Upcoming fixture, or for a past bet the first match on/after the placement time."""
    con = db.con
    lo = (placed or now) - timedelta(hours=3)
    hi = (placed or now) + timedelta(days=days)
    rows = con.execute(
        """SELECT m.match_id, m.kickoff_utc, m.status, m.home_team_id, m.away_team_id, m.referee_id,
                  s.league_id, l.code AS league, h.name AS home, a.name AS away, r.name AS referee
           FROM matches m JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
           JOIN teams h ON h.team_id = m.home_team_id JOIN teams a ON a.team_id = m.away_team_id
           LEFT JOIN referees r USING (referee_id)
           WHERE m.status IN ('scheduled', 'finished') AND m.kickoff_utc BETWEEN ? AND ?
           ORDER BY m.kickoff_utc""", (lo.strftime(ISO), hi.strftime(ISO))).fetchall()
    if placed is None:
        rows = [r for r in rows if r["status"] == "scheduled" and r["kickoff_utc"] > now.strftime(ISO)]
    resolver = TeamResolver(db, {t for r in rows for t in (r["home_team_id"], r["away_team_id"])})
    h, a = resolver.resolve(bet["home"], "manual"), resolver.resolve(bet["away"], "manual")
    fx = next((r for r in rows if (r["home_team_id"], r["away_team_id"]) == (h, a)), None)
    if fx is None:
        when = f"within {days:g} days of {placed:%Y-%m-%d %H:%M}" if placed else f"in the next {days:g} days"
        raise SystemExit(f"No match {bet['home']} v {bet['away']} {when}")
    return fx


def poisson_tail(mu: float, line: float, selection: str) -> tuple[float, float]:
    pmf = lambda k: math.exp(k * math.log(mu) - mu - math.lgamma(k + 1)) if mu > 0 else float(k == 0)
    return side_probs(pmf, line, selection)


def log_bet(db: DB, projector: Projector, text: str, now: datetime, days: float, min_ev: float,
            placed: datetime | None = None, adjusted_p: float | None = None, reason: str | None = None,
            espn_cards=ESPN_CARDS) -> dict:
    bet = parse_bet(text)
    adjusted_p = adjusted_p if adjusted_p is not None else bet.get("adjusted_p")
    reason = reason or bet.get("reason")
    if is_quarter_line(bet["line"]):
        raise SystemExit("Quarter lines (x.25/x.75) are not supported")
    if bet["odds"] <= 1:
        raise SystemExit("Odds must be above 1.00 (decimal) or a fraction like 5/2")
    con = db.con
    fx = find_match(db, bet, now, days, placed)
    kind = bet["kind"]
    if kind == "team":
        side = TeamResolver(db, {fx["home_team_id"], fx["away_team_id"]}).resolve(bet["team"], "manual")
        if side not in (fx["home_team_id"], fx["away_team_id"]):
            raise SystemExit(f"{bet['team']!r} isn't {fx['home']} or {fx['away']}")
        kind = "home" if side == fx["home_team_id"] else "away"
    market = MARKETS[kind]

    book_id = db.one("SELECT bookmaker_id FROM bookmakers WHERE name = ? COLLATE NOCASE", bet["bookmaker"])
    if book_id is None:
        book_id = con.execute("INSERT INTO bookmakers (name) VALUES (?)", (bet["bookmaker"],)).lastrowid
    con.execute("INSERT OR IGNORE INTO markets (code, unit, scope, period) VALUES (?,?,?,?)", tuple(market.values()))
    market_id = db.one("SELECT market_id FROM markets WHERE code = ?", market["code"])

    w = weights_for(con, book_id, market_id, market["unit"])
    priced = projector.price(fx, w)
    if priced is None:
        con.rollback()
        raise SystemExit("Not enough history to model this match; bet not logged")
    if kind == "total":
        p_win, p_push = side_probs(priced.pmf, bet["line"] / priced.scale, bet["selection"])
        projected = priced.rating
    else:
        if priced.detail.get("model") != "glm":
            con.rollback()
            raise SystemExit("Team and each-team markets need the GLM (--model glm) for per-team projections")
        mu_h, mu_a = priced.detail["home_mu"], priced.detail["away_mu"]
        if kind == "each":  # both teams must clear the line (over) / stay under it (under); teams independent
            ph, push_h = poisson_tail(mu_h, bet["line"], bet["selection"])
            pa, push_a = poisson_tail(mu_a, bet["line"], bet["selection"])
            p_win, p_push, projected = ph * pa, 0.0, mu_h + mu_a
            if push_h or push_a:
                con.rollback()
                raise SystemExit("Each-team markets need a half line (use 2+ or over 1.5)")
        else:
            mu = mu_h if kind == "home" else mu_a
            p_win, p_push = poisson_tail(mu, bet["line"], bet["selection"])
            projected = mu
    if adjusted_p is not None and adjusted_p + p_push > 1:
        con.rollback()
        raise SystemExit(f"Adjusted p {adjusted_p:.1%} plus the {p_push:.1%} push chance exceeds 100%")
    ev = p_win * bet["odds"] + p_push - 1
    b = bet["odds"] - 1
    kelly = max(0.0, (b * p_win - (1 - p_win - p_push)) / b)

    captured = (placed or now).strftime(ISO)
    side_col = "over_odds" if bet["selection"] == "over" else "under_odds"
    # a price logged from a screenshot in the same minute is the same snapshot: reuse it, filling this side if blank
    con.execute(
        f"INSERT INTO odds_snapshots (match_id, bookmaker_id, market_id, line, {side_col}, captured_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT (match_id, bookmaker_id, market_id, line, captured_at) "
        f"DO UPDATE SET {side_col} = COALESCE({side_col}, excluded.{side_col})",
        (fx["match_id"], book_id, market_id, bet["line"], bet["odds"], captured))
    snap_id = db.one("""SELECT snapshot_id FROM odds_snapshots WHERE match_id = ? AND bookmaker_id = ? AND market_id = ?
                        AND line = ? AND captured_at = ?""", fx["match_id"], book_id, market_id, bet["line"], captured)
    provisional = fx["referee_id"] is None
    notes = " | ".join(x for x in (
        f"manual entry: {text.strip()}",
        f"stake {bet['stake_text']} (reported by user)",
        f"kickoff={fx['kickoff_utc']}",
        f"adj_p={adjusted_p:.4f}" if adjusted_p is not None else "",
        f"reason: {reason}" if reason else "",
        f"{market['code']} projected {projected:.2f}",
        priced.components,
        "provisional" if provisional else "",
    ) if x)
    bet_id = con.execute(
        """INSERT INTO value_bets (snapshot_id, selection, model_version, expected_mean, model_prob,
               model_push_prob, taken_odds, stake, kelly_fraction, placed_at, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (snap_id, bet["selection"], projector.version, projected, p_win, p_push, bet["odds"], bet["stake"],
         kelly, captured, notes)).lastrowid
    con.commit()
    # with ESPN's counts, as every other settle run: a bet the two sources dispute stays pending
    settled = settle(con, read_espn_cards(espn_cards)) if fx["status"] == "finished" else []
    result = next((s for s in settled if s["bet_id"] == bet_id), None)
    return dict(bet_id=bet_id, snapshot_id=snap_id, fixture=f"{fx['home']} v {fx['away']}",
                kickoff_utc=fx["kickoff_utc"], referee=fx["referee"], provisional=provisional,
                market=market["code"], target=bet.get("team") or ("each team" if kind == "each" else "match"),
                bookmaker=bet["bookmaker"], selection=bet["selection"], line=bet["line"], odds=bet["odds"],
                stake=bet["stake"], stake_text=bet["stake_text"],
                projected_cards=projected, model=projector.version, model_prob=p_win, push_prob=p_push,
                adjusted_prob=adjusted_p,
                adjusted_ev=adjusted_p * bet["odds"] + p_push - 1 if adjusted_p is not None else None,
                fair_odds=odds_for(p_win, p_push, 0), min_odds=odds_for(p_win, p_push, min_ev),
                ev=ev, kelly=kelly, meets_min_ev=ev >= min_ev,
                result=result["result"] if result else "pending",
                profit=result["profit"] if result else None)


def print_logged(r: dict) -> None:
    what = r["target"] if r["target"] in ("match", "each team") else r["target"]
    print(f"Logged bet #{r['bet_id']} (stake {r['stake_text']}): {r['fixture']} [{what}] {r['selection']} "
          f"{r['line']} @ {r['odds']:.2f} {r['bookmaker']}" + ("  [provisional]" if r["provisional"] else ""))
    print(f"  projected {r['projected_cards']:.2f}  model {r['model_prob']:.1%}"
          + (f" (push {r['push_prob']:.1%})" if r["push_prob"] else "")
          + f"  fair {_odds(r['fair_odds']).strip()}  model EV {r['ev']:+.1%}"
          + (f"  | adjusted {r['adjusted_prob']:.1%}, EV {r['adjusted_ev']:+.1%}" if r["adjusted_prob"] is not None else "")
          + ("" if r["meets_min_ev"] else "  -- model below the EV threshold"))
    if r["result"] != "pending":
        print(f"  settled: {r['result']} ({r['profit']:+.2f})")


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
    ap.add_argument("--model", choices=["glm", "pcr"], default=Config.model,
                    help="glm = team-level regression (default), pcr = three-way average")
    ap.add_argument("--distribution", choices=["negbin", "poisson"], default=Config.distribution,
                    help="PCR model only")
    ap.add_argument("--team-prior", type=float, default=Config.team_prior)
    ap.add_argument("--ref-prior", type=float, default=Config.ref_prior)
    ap.add_argument("--trend-recent", type=int, default=Config.trend_recent)
    ap.add_argument("--trend-base", type=int, default=Config.trend_base)
    ap.add_argument("--trend-prior", type=float, default=Config.trend_prior)
    ap.add_argument("--log-bet", metavar="TEXT", help='e.g. "Brighton v Man Utd over 4.5 @ 2.20 bet365 £10" (stake defaults to 1 unit)')
    ap.add_argument("--placed", metavar="'YYYY-MM-DD HH:MM'",
                    help="log a bet placed in the past (UK time); model priced as of then, settles if finished")
    ap.add_argument("--adjusted-p", type=float, help="your research-adjusted probability (0.45 or 45)")
    ap.add_argument("--reason", help="why you adjusted the model probability")
    ap.add_argument("--book", default="Sky Bet", help="price under this bookmaker's card settlement rules")
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
    now = datetime.now(timezone.utc)
    placed = (datetime.strptime(args.placed, "%Y-%m-%d %H:%M").replace(tzinfo=UK).astimezone(timezone.utc)
              if args.placed else None)
    projector = Projector(db.con, cfg, placed or now)  # past bets: model sees only data before placement
    try:
        if args.log_bet:
            r = log_bet(db, projector, args.log_bet, now, args.days, args.min_ev, placed,
                        parse_prob(str(args.adjusted_p)) if args.adjusted_p is not None else None, args.reason)
            if args.json:
                print(json.dumps(r, indent=2))
            else:
                print_logged(r)
            return 0
        w = book_weights(db.con, args.book)
        sheet = [price_match(projector, fx, sorted(args.lines), args.min_ev, w)
                 for fx in fixtures(db.con, now, args.days)]
        if args.json:
            print(json.dumps({"generated_at": now.strftime(ISO), "min_ev": args.min_ev, "book": args.book,
                              "card_weights": dict(yellow=w.yellow, second_yellow=w.second_yellow,
                                                   straight_red=w.straight_red),
                              "matches": sheet}, indent=2))
        else:
            print(f"Priced under {args.book} card rules: yellow {w.yellow:g}, second yellow "
                  f"{w.second_yellow:g}, straight red {w.straight_red:g}")
            print_sheet(sheet, args.min_ev)
    finally:
        db.con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
