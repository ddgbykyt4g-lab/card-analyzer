#!/usr/bin/env python3
"""Walk-forward backtest of the card model on the Tier 1 candidate leagues (tier1.db), per league.

  python review/tier1_backtest.py tier1.db

Same model and settings as live (GLMConfig defaults, Sky Bet card units), fitted jointly on the five leagues
(as the live model is on its six), refit weekly on earlier matches only. These leagues have no referees in the
data (football-data has none for them and ESPN lists no officials from Oct 2026), which is also how they would
be priced live, so every match uses the league-average referee. Nothing was tuned on them; 2022-23 is the warm-up.

Gate, fixed before running (24 Nov 2026): a league passes if the model beats the league-average baseline of
review/audit_model.py by at least +0.02 log-loss a match on 2023-27, with the 95% interval above 0. For scale:
the six live leagues gain +0.036 together (EPL +0.015 to Serie A +0.061); the Championship gained +0.008.
Read-only.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wf import boot_ci, cached_walk, climatology, metrics, p_over_total, total_pmf  # noqa: E402

SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26", "2026-27"]
SCORED = SEASONS[1:]
GATE = 0.02


def ll(r: dict) -> float:
    return math.log(max(total_pmf(r)(int(r["y_h"] + r["y_a"])), 1e-300))


def main(db: str) -> None:
    base = cached_walk(db, SEASONS, tag="tier1")
    clim = {r["match_id"]: r for r in climatology(base, base)}
    rows = [r for r in base if r["season"] in SCORED and r["match_id"] in clim]
    print(f"{db}: {len(rows)} matches scored walk-forward on 2023-27 (no referees, as live)")
    print(f"  {'league':9s} {'n':>5}  {'gain':>7}  {'95% CI':>17}  {'by season':28s} {'proj sd':>7}  {'o3.5 proj/act':>13}  verdict")
    for lg in sorted({r["league"] for r in rows}) + ["ALL"]:
        rr = [r for r in rows if lg == "ALL" or r["league"] == lg]
        g = np.array([ll(r) - ll(clim[r["match_id"]]) for r in rr])
        lo, hi = boot_ci(g)
        by = " ".join(f"{np.mean([ll(r) - ll(clim[r['match_id']]) for r in rr if r['season'] == s]):+.3f}"
                      for s in SCORED if any(r["season"] == s for r in rr))
        mu = np.array([r["mu_h"] + r["mu_a"] for r in rr])
        p = np.mean([p_over_total(r, 3.5) for r in rr])
        hit = np.mean([r["y_h"] + r["y_a"] > 3.5 for r in rr])
        verdict = "" if lg == "ALL" else ("PASS" if g.mean() >= GATE and lo > 0 else "fail")
        print(f"  {lg:9s} {len(rr):5d}  {g.mean():+.4f}  ({lo:+.4f} to {hi:+.4f})  {by:28s} {mu.std():7.2f}  "
              f"{p:6.1%}/{hit:5.1%}  {verdict}")
    m = metrics(rows)
    print(f"\n  all: projected {m['mu']:.2f} v actual {m['y']:.2f} cards a match; "
          f"live six leagues for comparison: gain +0.036, projected-total sd 0.48-0.69")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tier1.db")
