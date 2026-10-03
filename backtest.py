#!/usr/bin/env python3
"""
backtest: walk-forward check of the PCR card model on finished matches.

Each match is projected with value_finder's own code using only data from before its
kickoff, then compared with what happened. Reports:
  - accuracy of projected vs actual match cards (bias, MAE, RMSE, correlation), per league
  - the same for a baseline that ignores teams/referee (league average only)
  - log-likelihood of actual totals under negative binomial vs Poisson
  - calibration and Brier score of P(Over) on common lines
  - fitted weights for the home/away/referee terms vs the equal thirds PCR uses,
    fitted on the earlier season(s) and scored out of sample on the latest

Usage
    python backtest.py --db cards.db                       # test on 2025-26 and 2026-27
    python backtest.py --db cards.db --seasons 2026-27 --csv preds.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import sys
from dataclasses import replace

import numpy as np

from value_finder import DEFAULT_WEIGHTS, Config, Stats, fit_pmf, make_pmf, project, side_probs

W = DEFAULT_WEIGHTS["cards"]
LINES = (3.5, 4.5, 5.5)


def matches(con: sqlite3.Connection, seasons: list[str]) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(seasons))
    return con.execute(
        f"""SELECT m.match_id, m.kickoff_utc, m.home_team_id, m.away_team_id, m.referee_id,
                   s.league_id, s.label AS season, l.code AS league,
                   h.name AS home, a.name AS away,
                   SUM(:wy * c.yellows + :w2 * c.second_yellows + :wr * c.straight_reds) AS actual
            FROM matches m
            JOIN seasons s USING (season_id)
            JOIN leagues l USING (league_id)
            JOIN teams h ON h.team_id = m.home_team_id
            JOIN teams a ON a.team_id = m.away_team_id
            JOIN match_team_cards c USING (match_id)
            WHERE m.status = 'finished' AND s.label IN ({marks})
            GROUP BY m.match_id HAVING COUNT(*) = 2
            ORDER BY m.kickoff_utc""".replace(":wy", str(W.yellow)).replace(":w2", str(W.second_yellow))
        .replace(":wr", str(W.straight_red)), seasons).fetchall()


def run(con: sqlite3.Connection, cfg: Config, seasons: list[str]) -> tuple[list[dict], int]:
    stats = Stats(con, cfg)
    poisson_cfg = replace(cfg, distribution="poisson")
    rows, skipped = [], 0
    for fx in matches(con, seasons):
        proj = project(stats, cfg, fx, W)
        l_mean, _, l_n = stats.league_profile(fx["league_id"], W, fx["kickoff_utc"])
        if proj is None or not l_mean:
            skipped += 1
            continue
        nb, size, _ = fit_pmf(stats, replace(cfg, distribution="negbin"), fx["league_id"], W,
                              fx["kickoff_utc"], proj.rating)
        po, _, _ = fit_pmf(stats, poisson_cfg, fx["league_id"], W, fx["kickoff_utc"], proj.rating)
        base = make_pmf(l_mean, size)  # same dispersion, no team/referee information
        k = int(round(fx["actual"]))
        r = dict(match_id=fx["match_id"], kickoff=fx["kickoff_utc"][:10], league=fx["league"],
                 season=fx["season"], fixture=f"{fx['home']} v {fx['away']}",
                 actual=fx["actual"], pcr=proj.rating, base=l_mean,
                 home_avg=proj.home_avg, away_avg=proj.away_avg, ref_avg=proj.ref_avg,
                 ref_source=proj.ref_source, nb_size=size,
                 ll_nb=math.log(max(nb(k), 1e-300)), ll_po=math.log(max(po(k), 1e-300)),
                 ll_base=math.log(max(base(k), 1e-300)))
        for line in LINES:
            r[f"p_over_{line}"] = side_probs(nb, line, "over")[0]
            r[f"base_over_{line}"] = side_probs(base, line, "over")[0]
        rows.append(r)
    return rows, skipped


# ───────────────────────────── Reporting ─────────────────────────────
def accuracy(rows: list[dict], key: str) -> str:
    a = np.array([r["actual"] for r in rows])
    p = np.array([r[key] for r in rows])
    err = p - a
    corr = np.corrcoef(p, a)[0, 1] if p.std() > 0 else float("nan")
    return (f"bias {err.mean():+.2f}  MAE {np.abs(err).mean():.2f}  RMSE {np.sqrt((err ** 2).mean()):.2f}  "
            f"corr {corr:.3f}")


def brier(rows: list[dict], key: str, line: float) -> float:
    return float(np.mean([(r[key] - (r["actual"] > line)) ** 2 for r in rows]))


def calibration(rows: list[dict], line: float) -> list[str]:
    out = []
    for lo in np.arange(0, 1, 0.1):
        b = [r for r in rows if lo <= r[f"p_over_{line}"] < lo + 0.1]
        if len(b) >= 10:
            pred = np.mean([r[f"p_over_{line}"] for r in b])
            act = np.mean([r["actual"] > line for r in b])
            out.append(f"    {lo:.1f}-{lo + 0.1:.1f}  n={len(b):4d}  predicted {pred:5.1%}  actual {act:5.1%}")
    return out


def fit_weights(train: list[dict]) -> np.ndarray:
    """OLS of actual cards on [1, home_avg, away_avg, ref_avg]."""
    X = np.array([[1, r["home_avg"], r["away_avg"], r["ref_avg"]] for r in train])
    y = np.array([r["actual"] for r in train])
    return np.linalg.lstsq(X, y, rcond=None)[0]


def report(rows: list[dict], skipped: int, seasons: list[str]) -> None:
    print(f"Walk-forward backtest on {', '.join(seasons)}: {len(rows)} matches "
          f"({skipped} skipped for thin history)\n")
    groups = [("All", rows)] + [(lg, [r for r in rows if r["league"] == lg])
                                for lg in sorted({r["league"] for r in rows})]

    print("Projected vs actual match cards (yellow 1, red 2)")
    for name, g in groups:
        print(f"  {name:4} n={len(g):4d}  actual mean {np.mean([r['actual'] for r in g]):.2f}")
        print(f"       PCR      {accuracy(g, 'pcr')}")
        print(f"       baseline {accuracy(g, 'base')}")

    print("\nLog-likelihood per match (higher is better)")
    for name, g in groups:
        nb_r = [r["nb_size"] for r in g if r["nb_size"]]
        print(f"  {name:4} PCR+NB {np.mean([r['ll_nb'] for r in g]):.4f}   PCR+Poisson "
              f"{np.mean([r['ll_po'] for r in g]):.4f}   baseline+NB {np.mean([r['ll_base'] for r in g]):.4f}"
              + (f"   (NB r median {np.median(nb_r):.1f}, NB used in {len(nb_r)}/{len(g)})" if nb_r else
                 "   (no overdispersion: NB fell back to Poisson)"))

    print("\nP(Over) Brier score (lower is better; PCR vs baseline)")
    for name, g in groups:
        cells = "  ".join(f"{ln}: {brier(g, f'p_over_{ln}', ln):.4f} vs {brier(g, f'base_over_{ln}', ln):.4f}"
                          for ln in LINES)
        print(f"  {name:4} {cells}")

    for line in (4.5,):
        print(f"\nCalibration, Over {line} (all matches)")
        print("\n".join(calibration(rows, line)) or "    not enough matches per bin")

    src = {}
    for r in rows:
        src[r["ref_source"]] = src.get(r["ref_source"], 0) + 1
    print(f"\nReferee term source: {src}")

    latest = seasons[-1]
    train = [r for r in rows if r["season"] != latest]
    test = [r for r in rows if r["season"] == latest]
    print(f"\nTerm weights (OLS; fitted on {', '.join(s for s in seasons if s != latest) or '-'}, "
          f"scored on {latest})")
    if len(train) < 50 or len(test) < 20:
        print("  not enough matches to split; pass at least two seasons")
        return
    b = fit_weights(train)
    print(f"  fitted: intercept {b[0]:+.2f}  home {b[1]:.3f}  away {b[2]:.3f}  referee {b[3]:.3f}"
          f"   (PCR: 0, 0.333, 0.333, 0.333)")
    fitted = [dict(r, fit=b[0] + b[1] * r["home_avg"] + b[2] * r["away_avg"] + b[3] * r["ref_avg"])
              for r in test]
    print(f"  {latest} out of sample  PCR    {accuracy(test, 'pcr')}")
    print(f"  {latest} out of sample  fitted {accuracy(fitted, 'fit')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Walk-forward backtest of the PCR card model")
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--seasons", nargs="+", default=["2025-26", "2026-27"])
    ap.add_argument("--team-lookback", type=int, default=Config.team_lookback)
    ap.add_argument("--ref-lookback", type=int, default=Config.ref_lookback)
    ap.add_argument("--no-venue-split", dest="venue_split", action="store_false")
    ap.add_argument("--team-prior", type=float, default=Config.team_prior)
    ap.add_argument("--ref-prior", type=float, default=Config.ref_prior)
    ap.add_argument("--trend-recent", type=int, default=Config.trend_recent)
    ap.add_argument("--trend-base", type=int, default=Config.trend_base)
    ap.add_argument("--trend-prior", type=float, default=Config.trend_prior)
    ap.add_argument("--csv", metavar="FILE", help="write per-match predictions")
    args = ap.parse_args(argv)

    cfg = Config(team_lookback=args.team_lookback, ref_lookback=args.ref_lookback,
                 venue_split=args.venue_split, ref_fallback_league_avg=True,
                 team_prior=args.team_prior, ref_prior=args.ref_prior, trend_recent=args.trend_recent,
                 trend_base=args.trend_base, trend_prior=args.trend_prior)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows, skipped = run(con, cfg, args.seasons)
    con.close()
    if not rows:
        print("No matches to test.")
        return 1
    report(rows, skipped, args.seasons)
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
