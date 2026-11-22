#!/usr/bin/env python3
"""Walk-forward backtest of the frozen card model on a shadow league's own database (the Championship, efl.db).

  python review/efl_backtest.py efl.db

Same model and settings as live (glm-v4.1, GLMConfig defaults, Sky Bet card units), fitted only on that
database, refit weekly on earlier matches. Nothing was tuned on these leagues, so every season is out-of-sample;
2022-23 is reported apart because the first months have little history. Compared with the league-average
baseline of review/audit_model.py (each league's mean total over its previous 380 matches, same dispersion).
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


def ll(r: dict) -> float:
    return math.log(max(total_pmf(r)(int(r["y_h"] + r["y_a"])), 1e-300))


def main(db: str) -> None:
    base = cached_walk(db, SEASONS, tag=os.path.basename(db).split(".")[0])
    clim = {r["match_id"]: r for r in climatology(base, base)}
    print(f"{db}: {len(base)} matches projected walk-forward; baseline needs 50 earlier matches")
    for s in SEASONS:
        rows = [r for r in base if r["season"] == s and r["match_id"] in clim]
        if not rows:
            continue
        m, c = metrics(rows), metrics([clim[r["match_id"]] for r in rows])
        g = np.array([ll(r) - ll(clim[r["match_id"]]) for r in rows])
        lo, hi = boot_ci(g)
        print(f"  {s}: n={m['n']:3d}  log-loss {m['ll']:+.4f} v baseline {c['ll']:+.4f}  gain {g.mean():+.4f} "
              f"({lo:+.4f} to {hi:+.4f})  RMSE {m['rmse']:.3f} v {c['rmse']:.3f}  projected {m['mu']:.2f} v actual {m['y']:.2f}")
    rows = [r for r in base if r["season"] in SCORED and r["match_id"] in clim]
    g = np.array([ll(r) - ll(clim[r["match_id"]]) for r in rows])
    lo, hi = boot_ci(g)
    m, c = metrics(rows), metrics([clim[r["match_id"]] for r in rows])
    print(f"\n  {SCORED[0]} to {SCORED[-1]}: n={len(rows)}  gain per match {g.mean():+.4f} (95% CI {lo:+.4f} to {hi:+.4f})"
          f"  Brier {m['brier']:.4f} v {c['brier']:.4f}")

    print("\n  Calibration, over 3.5 and over 4.5 (projected v actual, by fifth of projected probability):")
    for line in (3.5, 4.5):
        p = np.array([p_over_total(r, line) for r in rows])
        hit = np.array([r["y_h"] + r["y_a"] > line for r in rows], dtype=float)
        bins = [(p[ix].mean(), hit[ix].mean(), len(ix)) for ix in np.array_split(np.argsort(p), 5)]
        print(f"    over {line}: " + "  ".join(f"{a:.2f}->{b:.2f}" for a, b, _ in bins)
              + f"   all {p.mean():.3f} v {hit.mean():.3f}")
    hc = np.array([r["has_ref"] for r in rows]).mean()
    hp = np.array([r["has_prices"] for r in rows]).mean()
    print(f"\n  matches with a referee: {hc:.0%}, with 1X2 prices: {hp:.0%}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "efl.db")
