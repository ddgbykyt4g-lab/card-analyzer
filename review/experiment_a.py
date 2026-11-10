"""Experiment A: does blending the model with the bookmaker's own de-vigged price beat the bookmaker?
Plan, fixed parameters and pass marks: review/EXPERIMENT_A.md. Read-only on cards.db.

  pick   Lists the benchmark matches of the next 7 days. A fixture is a benchmark match if a hash of its ESPN
         id falls in one bucket of four: about a quarter of all fixtures, chosen without looking at the model,
         fixed for good and the same whenever it is run. So the sample isn't tilted toward matches the model
         likes. Screenshot Sky Bet's Total cards over/under tab for each; log them with agent/log_prices.py.
  score  Scores every two-sided Sky Bet line captured since the start date: log-loss of the model, the
         bookmaker (margin removed) and the 50/50 blend, the blend's margin over the bookmaker with an interval
         that resamples whole matches, closing-line value where closing prices exist, and both betting rules
         as paper bets. Ends with where the experiment stands against its pass marks.
Usage: python review/experiment_a.py pick  [--db cards.db]
       python review/experiment_a.py score [--db cards.db] [--since 2026-11-10]
"""
import argparse
import csv
import hashlib
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from wf import ROOT, connect, pois_over  # noqa: E402

from check_prices import BLEND_W, blend, market_prob, verdicts  # noqa: E402

START = "2026-11-10"   # prices captured from this day on count; everything earlier was used to design the test
ONE_IN = 4             # a fixture is a benchmark match if its hash falls in one bucket of this many
NEED, GIVE_UP = 150, 300   # priced benchmark matches: first verdict, and where "no usable edge" is concluded
ISO = "%Y-%m-%dT%H:%M:%SZ"
SIDE = {"TOTAL_CARDS": "total", "HOME_CARDS": "home", "AWAY_CARDS": "away"}


def event_id(external_id: str | None) -> str:
    return (external_id or "").rsplit(":", 1)[-1]


def is_benchmark(external_id: str | None) -> bool:
    """Model-blind, reproducible and stateless: decided by a hash of the fixture's ESPN event id alone."""
    eid = event_id(external_id)
    return bool(eid) and int(hashlib.sha256(f"experiment-a:{eid}".encode()).hexdigest(), 16) % ONE_IN == 0


def cmd_pick(args) -> int:
    con = connect(args.db)
    now = datetime.now(timezone.utc)
    rows = [r for r in con.execute(
        """SELECT m.external_id, m.kickoff_utc, l.code, h.name, a.name
           FROM matches m JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
           JOIN teams h ON h.team_id = m.home_team_id JOIN teams a ON a.team_id = m.away_team_id
           WHERE m.status = 'scheduled' AND m.kickoff_utc > ? AND m.kickoff_utc <= ? ORDER BY m.kickoff_utc""",
        (now.strftime(ISO), (now + timedelta(days=7)).strftime(ISO))) if is_benchmark(r[0])]
    if not rows:
        print("No benchmark matches in the next 7 days.")
        return 0
    print("Benchmark matches (chosen without the model). Screenshot Sky Bet's Total cards over/under tab for each:")
    for _, ko, league, home, away in rows:
        print(f"  {ko[:16].replace('T', ' ')} UTC  {league:7} {home} v {away}")
    return 0


# ───────────────────────────── scoring ─────────────────────────────
def ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return y * np.log(p) + (1 - y) * np.log(1 - p)


def cluster_ci(values: np.ndarray, groups: np.ndarray, rng, n: int = 4000) -> tuple[float, float]:
    ids = np.unique(groups)
    by = {g: values[groups == g] for g in ids}
    draws = [np.mean(np.concatenate([by[g] for g in rng.choice(ids, len(ids))])) for _ in range(n)]
    return float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def load_lines(con: sqlite3.Connection, log: str, espn_path: str, since: str) -> list[dict]:
    """The latest pre-kickoff Sky Bet price of each match/market/line captured since `since`, settled, with
    the model probability logged before the price was captured. Outcomes prefer ESPN's count (CLAUDE.md)."""
    espn = {r["espn_id"]: r for r in csv.DictReader(open(espn_path))} if os.path.exists(espn_path) else {}
    actual = {}
    for mid, ext, h, a in con.execute("""SELECT m.match_id, m.external_id,
            (SELECT yellows + second_yellows + straight_reds FROM match_team_cards c WHERE c.match_id = m.match_id AND c.team_id = m.home_team_id),
            (SELECT yellows + second_yellows + straight_reds FROM match_team_cards c WHERE c.match_id = m.match_id AND c.team_id = m.away_team_id)
            FROM matches m WHERE m.status = 'finished'"""):
        e = espn.get(event_id(ext))
        if e:
            h, a = int(e["home_yellows"]) + int(e["home_reds"]), int(e["away_yellows"]) + int(e["away_reds"])
        if h is not None and a is not None:
            actual[mid] = dict(home=h, away=a, total=h + a)
    by_ext = {event_id(e): m for m, e in con.execute("SELECT match_id, external_id FROM matches") if e}
    logged = defaultdict(list)
    for r in csv.DictReader(open(log)):
        mid = by_ext.get(event_id(r["external_id"]))
        if mid is not None and r["mu_home"]:
            logged[mid].append(r)
    latest = {}
    for s in con.execute("""
            SELECT o.match_id, o.line, o.over_odds, o.under_odds, o.captured_at, mk.code, m.external_id,
                   h.name || ' v ' || a.name AS fixture, p.p_home, p.p_away
            FROM odds_snapshots o JOIN markets mk USING (market_id) JOIN bookmakers b USING (bookmaker_id)
            JOIN matches m ON m.match_id = o.match_id JOIN teams h ON h.team_id = m.home_team_id
            JOIN teams a ON a.team_id = m.away_team_id LEFT JOIN match_prices p ON p.match_id = m.match_id
            WHERE b.name = 'Sky Bet' AND mk.code IN ('TOTAL_CARDS', 'HOME_CARDS', 'AWAY_CARDS')
              AND o.captured_at < m.kickoff_utc AND o.captured_at >= ? ORDER BY o.captured_at""", (since,)):
        s = dict(s)
        latest[(s["match_id"], s["code"], s["line"])] = s
    out = []
    for s in latest.values():
        side = SIDE[s["code"]]
        proj = [r for r in logged.get(s["match_id"], []) if r["logged_at"] <= s["captured_at"]]
        if s["match_id"] not in actual or not proj or not s["over_odds"]:
            continue
        r = max(proj, key=lambda r: r["logged_at"])
        if side == "total":
            pmf = [float(x) for x in r["pmf"].split()]
            p = 1 - sum(pmf[:math.floor(s["line"]) + 1])
        else:
            p = pois_over(float(r[f"mu_{side}"]), s["line"])
        mkt, assumed = market_prob(s["over_odds"], s["under_odds"])
        p_win = None if side == "total" else s["p_home"] if side == "home" else s["p_away"]
        out.append(dict(s, side=side, p=p, mkt=mkt, assumed=assumed, mix=blend(p, mkt),
                        y=float(actual[s["match_id"]][side] > s["line"]), event=event_id(s["external_id"]),
                        underdog=p_win is not None and p_win < 0.20))
    return out


def summarise(lines: list[dict], label: str, rng) -> dict | None:
    sub = [r for r in lines if not r["assumed"] and 0.10 <= r["mkt"] <= 0.90]
    matches = len({r["match_id"] for r in sub})
    if matches < 5:
        print(f"  {label}: {len(sub)} lines on {matches} matches: too few to score")
        return dict(matches=matches)
    y, g = np.array([r["y"] for r in sub]), np.array([r["match_id"] for r in sub])
    pm, pk, pb = (np.array([r[k] for r in sub]) for k in ("p", "mkt", "mix"))
    d = ll(pb, y) - ll(pk, y)
    lo, hi = cluster_ci(d, g, rng)
    dm = ll(pm, y) - ll(pk, y)
    lom, him = cluster_ci(dm, g, rng)
    lm, lk = np.log(pm / (1 - pm)), np.log(pk / (1 - pk))
    grid = np.linspace(0, 1, 21)
    best = grid[int(np.argmax([ll(1 / (1 + np.exp(-(w * lm + (1 - w) * lk))), y).mean() for w in grid]))]
    print(f"  {label}: {len(sub)} lines on {matches} matches")
    print(f"     log-loss: model {ll(pm, y).mean():+.4f}, Sky Bet {ll(pk, y).mean():+.4f}, blend {ll(pb, y).mean():+.4f}")
    print(f"     blend minus Sky Bet {d.mean():+.4f} (95% CI {lo:+.4f} to {hi:+.4f}); model minus Sky Bet {dm.mean():+.4f} ({lom:+.4f} to {him:+.4f})")
    print(f"     weight on the model that would have scored best: {best:.2f} (reported only; the test uses {BLEND_W})")
    return dict(matches=matches, diff=float(d.mean()), lo=lo, hi=hi)


def paper(lines: list[dict], key: str, label: str, rng) -> None:
    blocks = defaultdict(list)
    for r in lines:
        blocks[(r["match_id"], r["side"])].append(r)
    pr, g = [], []
    for (mid, side), rs in blocks.items():
        v = verdicts([dict(line=r["line"], p_over=r["p"], p_push=0.0, over=r["over_odds"], under=r["under_odds"], _r=r) for r in rs],
                     team_line=side != "total", underdog=rs[0]["underdog"])
        for x in v:
            if x[key].startswith("BET"):
                pr.append(x["over"] - 1 if x["_r"]["y"] else -1.0)
                g.append(mid)
    if len(pr) < 3:
        print(f"  {label}: {len(pr)} bets")
        return
    pr, g = np.array(pr), np.array(g)
    lo, hi = cluster_ci(pr, g, rng)
    print(f"  {label}: {len(pr)} bets, {int((pr > 0).sum())} won, ROI {pr.mean():+.1%} (95% CI {lo:+.1%} to {hi:+.1%})")


def cmd_score(args) -> int:
    rng = np.random.default_rng(5)
    con = connect(args.db)
    con.row_factory = sqlite3.Row
    lines = load_lines(con, args.log, args.espn, args.since)
    bench = [r for r in lines if is_benchmark(r["event"])]
    print(f"Experiment A: Sky Bet prices captured since {args.since}, settled, with a projection logged before the price")
    print("\n1. Probability scoring (two-sided lines the bookmaker puts between 10% and 90%; above 0 = better than Sky Bet)")
    primary = summarise(bench, "benchmark matches (chosen without the model): the test", rng)
    summarise(lines, "every priced match (includes ones screenshotted because the model liked them)", rng)
    print("\n2. Closing-line value on real bets")
    clv = [r[0] for r in con.execute("SELECT clv FROM value_bets WHERE stake > 0 AND clv IS NOT NULL AND placed_at >= ?", (args.since,))]
    if clv:
        lo, hi = (np.percentile([np.mean(rng.choice(clv, len(clv))) for _ in range(4000)], q) for q in (2.5, 97.5))
        print(f"  {len(clv)} bets with a Betfair closing price: mean CLV {np.mean(clv):+.1%} (95% CI {lo:+.1%} to {hi:+.1%})")
    else:
        print("  no closing prices yet (they arrive once the Betfair logger, PR #1, is live)")
    print("\n3. Paper bets on every priced line, 1 unit each")
    paper(lines, "verdict", "current rule (model value +8%)", rng)
    paper(lines, "blend_verdict", "blend rule (blended value +2%)", rng)
    print("\n4. Where the experiment stands")
    n = primary["matches"] if primary else 0
    if n < NEED:
        print(f"  {n} of {NEED} benchmark matches priced and settled: keep collecting, no conclusion yet.")
    elif primary["lo"] > 0 and clv and np.mean(clv) > 0:
        print("  PASS: the blend beats Sky Bet's prices with the interval above zero, and closing-line value is positive.")
    elif primary["lo"] > 0:
        print("  The blend beats Sky Bet's prices, but closing-line value is missing or not positive: not a pass yet.")
    elif n >= GIVE_UP:
        print(f"  {n} matches and the interval still includes zero: no usable edge at Sky Bet's margin.")
    else:
        print(f"  {n} matches, interval still includes zero: keep collecting to {GIVE_UP}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["pick", "score"])
    ap.add_argument("--db", default=os.path.join(ROOT, "cards.db"))
    ap.add_argument("--since", default=START)
    ap.add_argument("--log", default=os.path.join(ROOT, "data", "projections.csv"))
    ap.add_argument("--espn", default=os.path.join(ROOT, "data", "espn_cards.csv"))
    args = ap.parse_args()
    return cmd_pick(args) if args.command == "pick" else cmd_score(args)


if __name__ == "__main__":
    sys.exit(main())
