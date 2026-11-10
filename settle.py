#!/usr/bin/env python3
"""
settle: settle pending value_bets once their match's cards are loaded.

settled_value = the market's total under the bookmaker's settlement_rules row (or the
DEFAULT_WEIGHTS for the market unit), counting staff cards only if the rule says so.
Over wins above the line, Under below it, and a whole-number line that lands exactly pushes.
profit = stake * (odds - 1) won, -stake lost, 0 push/void. Paper bets (stake 0) get a result
and settled_value but zero profit.

Bets on abandoned matches are voided, and so are bets on matches not played within
POSTPONE_VOID_DAYS calendar days (UK) of the original kickoff date (Sky Bet's rule): still postponed
after that, or rescheduled past it (original kickoff = "kickoff=" in the bet's notes). First-/second-half markets are
left pending: the season CSVs have no per-half cards.

Card counts come from football-data, and either it or ESPN can be wrong for a match, so bets with a
stake are checked against ESPN's card events (data/espn_cards.csv, written by collect_cards.py):
  - both sources give the same result: settled as before;
  - they give different results: left pending and listed, until you say what the bookmaker paid with
    --confirm BET_ID won|lost|push|void;
  - only ESPN has the match yet (football-data lags a day or two): settled on ESPN's count, and listed
    later if football-data turns out to disagree.

Runs automatically at the end of collect_cards.py; can also be run on its own:
    python settle.py --db cards.db
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from value_finder import DEFAULT_WEIGHTS, ISO, Weights, is_quarter_line

log = logging.getLogger("settle")
POSTPONE_VOID_DAYS = 3
UK = ZoneInfo("Europe/London")
KICKOFF_TAG = re.compile(r"kickoff=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")
ESPN_CARDS = Path(__file__).resolve().parent / "data" / "espn_cards.csv"
CONFIRMED = "confirmed by user"
RESULTS = ("won", "lost", "push", "void")

BETS_SQL = """
    SELECT vb.bet_id, vb.selection, vb.taken_odds, vb.stake, vb.result, vb.notes,
           o.match_id, o.bookmaker_id, o.market_id, o.line,
           mk.code AS market, mk.unit, mk.scope, mk.period,
           m.status, m.home_team_id, m.away_team_id, m.external_id, h.name AS home, a.name AS away
    FROM value_bets vb
    JOIN odds_snapshots o USING (snapshot_id)
    JOIN markets mk ON mk.market_id = o.market_id
    JOIN matches m ON m.match_id = o.match_id
    JOIN teams h ON h.team_id = m.home_team_id
    JOIN teams a ON a.team_id = m.away_team_id
    WHERE """
PENDING = "vb.result = 'pending' AND m.status IN ('finished', 'abandoned', 'scheduled')"


def read_espn_cards(path: str | Path = ESPN_CARDS) -> dict[str, dict]:
    """ESPN's card counts by ESPN event id (collect_cards.load_espn_cards); empty without the file."""
    path = Path(path)
    if not path.exists():
        return {}
    with path.open() as f:
        return {r["espn_id"]: r for r in csv.DictReader(f)}


def bets(con: sqlite3.Connection, where: str, *args) -> list[dict]:
    cur = con.execute(BETS_SQL + where, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def rule_for(con: sqlite3.Connection, bookmaker_id: int, market_id: int, unit: str) -> tuple[Weights, bool]:
    row = con.execute("""SELECT w_yellow, w_second_yellow, w_straight_red, counts_staff FROM settlement_rules
                         WHERE bookmaker_id = ? AND market_id = ?""", (bookmaker_id, market_id)).fetchone()
    if row:
        return Weights(row[0], row[1], row[2]), bool(row[3])
    return DEFAULT_WEIGHTS[unit], False


def scope_total(bet: dict, per_team: dict[str, float]) -> float:
    """per_team: {'home': x, 'away': y} in the bookmaker's card units."""
    if bet["market"] == "EACH_TEAM_CARDS":  # over: both teams above the line; under: both below
        return min(per_team.values()) if bet["selection"] == "over" else max(per_team.values())
    return per_team[bet["scope"]] if bet["scope"] in per_team else sum(per_team.values())


def market_total(con: sqlite3.Connection, bet: dict, w: Weights, counts_staff: bool) -> float | None:
    """Total for the market's scope, or None if the match's cards aren't loaded."""
    rows = con.execute("""SELECT team_id, yellows, second_yellows, straight_reds, staff_cards
                          FROM match_team_cards WHERE match_id = ?""", (bet["match_id"],)).fetchall()
    if len(rows) != 2 or bet["status"] != "finished":
        return None
    side = {bet["home_team_id"]: "home", bet["away_team_id"]: "away"}
    return scope_total(bet, {
        side[tid]: w.yellow * y + w.second_yellow * y2 + w.straight_red * r + (w.yellow * staff if counts_staff else 0)
        for tid, y, y2, r, staff in rows})


def espn_total(bet: dict, w: Weights, counts_staff: bool, row: dict | None) -> float | None:
    """The same total from ESPN's card events, or None when it can't say: no row for the match, or a
    rule that scores a second yellow and a straight red differently (the file doesn't split reds)."""
    if row is None or w.second_yellow != w.straight_red:
        return None
    return scope_total(bet, {
        ha: w.yellow * int(row[f"{ha}_yellows"]) + w.straight_red * int(row[f"{ha}_reds"])
            + (w.yellow * int(row[f"{ha}_staff"]) if counts_staff else 0)
        for ha in ("home", "away")})


def both_totals(con: sqlite3.Connection, bet: dict, espn: dict[str, dict]) -> tuple[float | None, float | None]:
    """(football-data's total, ESPN's total) for a full-time bet; None where a source has nothing."""
    w, staff = rule_for(con, bet["bookmaker_id"], bet["market_id"], bet["unit"])
    row = espn.get((bet["external_id"] or "").rsplit(":", 1)[-1])
    return market_total(con, bet, w, staff), espn_total(bet, w, staff, row)


def outcome(selection: str, line: float, value: float) -> str:
    if abs(value - line) < 1e-9:
        return "push"
    return "won" if (value > line) == (selection == "over") else "lost"


def void_postponed(con: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Void pending bets on matches not played within POSTPONE_VOID_DAYS of the original kickoff date."""
    def uk_date(iso: str) -> date:
        return datetime.strptime(iso, ISO).replace(tzinfo=timezone.utc).astimezone(UK).date()
    today = (now or datetime.now(timezone.utc)).astimezone(UK).date()
    rows = con.execute("""SELECT vb.bet_id, vb.notes, m.status, m.kickoff_utc FROM value_bets vb
                          JOIN odds_snapshots o USING (snapshot_id) JOIN matches m ON m.match_id = o.match_id
                          WHERE vb.result = 'pending' AND m.status IN ('scheduled', 'postponed')""").fetchall()
    done = []
    for bet_id, notes, status, kickoff in rows:
        tag = KICKOFF_TAG.search(notes or "")
        original = uk_date(tag[1]) if tag else uk_date(kickoff)
        moved = (uk_date(kickoff) - original).days > POSTPONE_VOID_DAYS
        if moved or (status == "postponed" and (today - original).days > POSTPONE_VOID_DAYS):
            con.execute("UPDATE value_bets SET result = 'void', settled_value = NULL, profit = 0 WHERE bet_id = ?",
                        (bet_id,))
            done.append(dict(bet_id=bet_id, result="void", settled_value=None, profit=0.0))
    return done


def profit_for(bet: dict, result: str) -> float:
    stake = bet["stake"] or 0.0
    return round(stake * (bet["taken_odds"] - 1) if result == "won" else -stake if result == "lost" else 0.0, 2)


def settle(con: sqlite3.Connection, espn: dict[str, dict] | None = None) -> list[dict]:
    """Settle every pending bet whose match is done. Returns the bets settled. `espn` (read_espn_cards)
    turns on the cross-check for bets with a stake; paper bets settle on football-data alone."""
    espn = espn or {}
    done = void_postponed(con)
    for bet in bets(con, PENDING):
        note = None
        if bet["status"] == "abandoned":
            result, value = "void", None
        else:
            if bet["period"] != "FT" or is_quarter_line(bet["line"]):
                log.debug("bet %d: %s %s line %s not settleable here", bet["bet_id"], bet["market"],
                          bet["period"], bet["line"])
                continue
            ours, theirs = both_totals(con, bet, espn)
            if not bet["stake"]:
                theirs = None
            value = ours if ours is not None else theirs
            if value is None:
                continue  # cards not loaded yet
            result = outcome(bet["selection"], bet["line"], value)
            if ours is None:
                note = "settled on ESPN's card events (football-data not loaded yet)"
            elif theirs is not None and outcome(bet["selection"], bet["line"], theirs) != result:
                log.warning("bet %d left pending: football-data counts %g, ESPN %g", bet["bet_id"], ours, theirs)
                continue
        profit = profit_for(bet, result)
        con.execute("""UPDATE value_bets SET result = ?, settled_value = ?, profit = ?,
                           notes = CASE WHEN ? IS NULL THEN notes ELSE COALESCE(notes || ' | ', '') || ? END
                       WHERE bet_id = ?""", (result, value, profit, note, note, bet["bet_id"]))
        done.append(dict(bet_id=bet["bet_id"], result=result, settled_value=value, profit=profit))
    con.commit()
    if done:
        log.info("Settled %d bets: %s", len(done),
                 ", ".join(f"#{d['bet_id']} {d['result']} ({d['profit']:+.2f})" for d in done))
    return done


def disputes(con: sqlite3.Connection, espn: dict[str, dict]) -> list[dict]:
    """Staked bets where football-data and ESPN give different results (left pending), or where the
    recorded result differs from what either source now says. A confirmed bet is never listed."""
    out = []
    for bet in bets(con, "vb.stake > 0 AND vb.result != 'void' AND mk.period = 'FT' "
                         "AND m.status IN ('finished', 'scheduled')"):
        if CONFIRMED in (bet["notes"] or "") or is_quarter_line(bet["line"]):
            continue
        ours, theirs = both_totals(con, bet, espn)
        results = {outcome(bet["selection"], bet["line"], v) for v in (ours, theirs) if v is not None}
        if len(results) > 1 or (bet["result"] != "pending" and results - {bet["result"]}):
            out.append(dict(bet, football_data=ours, espn=theirs))
    return out


def describe(d: dict) -> str:
    count = lambda v: "no count yet" if v is None else f"{v:g}"
    team = {"home": f": {d['home']}", "away": f": {d['away']}"}.get(d["scope"], "")
    return (f"Bet #{d['bet_id']} {d['home']} v {d['away']}{team} {d['selection']} {d['line']:g} "
            f"(recorded {d['result']}): football-data counts {count(d['football_data'])}, ESPN {count(d['espn'])}.")


def confirm(con: sqlite3.Connection, bet_id: int, result: str, espn: dict[str, dict]) -> dict:
    """Record what the bookmaker paid on a bet the two sources dispute (or any bet settled wrongly)."""
    found = bets(con, "vb.bet_id = ?", bet_id)
    if not found:
        raise SystemExit(f"No bet #{bet_id}")
    bet = found[0]
    ours, theirs = (None, None) if bet["period"] != "FT" else both_totals(con, bet, espn)
    value = next((v for v in (ours, theirs)
                  if v is not None and outcome(bet["selection"], bet["line"], v) == result), None)
    count = lambda v: "?" if v is None else f"{v:g}"
    note = f"{CONFIRMED}: {result} (football-data {count(ours)}, ESPN {count(theirs)})"
    profit = profit_for(bet, result)
    con.execute("""UPDATE value_bets SET result = ?, settled_value = ?, profit = ?,
                       notes = COALESCE(notes || ' | ', '') || ? WHERE bet_id = ?""",
                (result, value, profit, note, bet_id))
    con.commit()
    return dict(bet_id=bet_id, result=result, settled_value=value, profit=profit)


def report(con: sqlite3.Connection) -> None:
    """Track record: your logged bets (and what the model thought of them) vs value_finder's paper bets."""
    rows = con.execute("""
        SELECT vb.bet_id, vb.result, vb.stake, vb.taken_odds, vb.model_prob, vb.model_push_prob, vb.profit,
               vb.notes, vb.model_version, mk.code
        FROM value_bets vb JOIN odds_snapshots o USING (snapshot_id) JOIN markets mk USING (market_id)""").fetchall()
    if not rows:
        print("No bets recorded yet.")
        return

    def line(name: str, bets: list) -> None:
        done = [b for b in bets if b[1] not in ("pending", "void")]
        if not bets:
            return
        won = sum(b[1] == "won" for b in done)
        staked = sum(b[2] for b in done)
        profit = sum(b[6] or 0 for b in done)
        flat = sum((b[3] - 1) if b[1] == "won" else -1 if b[1] == "lost" else 0 for b in done)
        exp_wins = sum(b[4] for b in done)
        roi = f"{profit / staked:+6.1%}" if staked else "     -"
        print(f"  {name:34} {len(bets):4d} {len(done):4d}  {won:3d}W  exp {exp_wins:5.1f}W  "
              f"staked {staked:8.2f}  profit {profit:+8.2f}  ROI {roi}  flat-1u {flat:+6.2f}")

    manual = [r for r in rows if (r[7] or "").startswith("manual entry")]
    paper = [r for r in rows if r not in manual]
    ev = lambda r: r[4] * r[3] + r[5] - 1
    print(f"  {'':34} {'bets':>4} {'sett':>4}  {'won':>4} {'model expects':>13}")
    line("Your bets", manual)
    line("  ...that the model rated +5% EV", [r for r in manual if ev(r) >= 0.05])
    line("  ...that the model rated below +5%", [r for r in manual if ev(r) < 0.05])
    adj = [r for r in manual if "adj_p=" in (r[7] or "")]
    line("  ...with a research-adjusted prob", adj)
    line("Model paper bets (value_finder)", paper)
    print("\n'exp W' = wins the model expected; 'flat-1u' = profit staking 1 unit on each settled bet.")
    print("Hundreds of settled bets are needed before any of these numbers mean much.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Settle pending value_bets from loaded card data")
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--report", action="store_true", help="print the track record after settling")
    ap.add_argument("--confirm", nargs=2, metavar=("BET_ID", "RESULT"),
                    help="record what the bookmaker paid on a disputed bet: won, lost, push or void")
    ap.add_argument("--espn-cards", default=str(ESPN_CARDS), metavar="CSV",
                    help="ESPN's card counts for recent matches (written by collect_cards.py)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    con = sqlite3.connect(args.db)
    con.execute("PRAGMA foreign_keys = ON")
    espn = read_espn_cards(args.espn_cards)
    if args.confirm:
        if not args.confirm[0].isdigit() or args.confirm[1] not in RESULTS:
            ap.error("--confirm takes a bet number and one of: " + ", ".join(RESULTS))
        c = confirm(con, int(args.confirm[0]), args.confirm[1], espn)
        print(f"Bet #{c['bet_id']} recorded as {c['result']} ({c['profit']:+.2f})")
    done = settle(con, espn)
    pending = con.execute("SELECT COUNT(*) FROM value_bets WHERE result = 'pending'").fetchone()[0]
    print(f"{len(done)} bets settled, {pending} still pending")
    for d in disputes(con, espn):
        print(f"CHECK: {describe(d)} See what the bookmaker paid, then: "
              f"python settle.py --db {args.db} --confirm {d['bet_id']} won|lost")
    if args.report:
        print()
        report(con)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
