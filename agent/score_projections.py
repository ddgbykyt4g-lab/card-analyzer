#!/usr/bin/env python3
"""Scores the pre-kickoff projection log (data/projections.csv) on finished matches: out-of-sample by
construction, since every row was written before its match kicked off.

Per match it uses the latest projection logged before the actual kickoff, and counts cards with the weights
stored in that row (Sky Bet: yellow 1, second yellow 1, straight red 1). Reports log-loss, Brier over lines
2.5-6.5, RMSE and over-rate calibration, per model version, next to the walk-forward backtest figures.
Usage: python agent/score_projections.py [cards.db] [data/projections.csv]
"""
import csv
import math
import sqlite3
import sys
from collections import defaultdict

LINES = (2.5, 3.5, 4.5, 5.5, 6.5)
# glm-v4 walk-forward backtest, untuned seasons 2024-27, all leagues, Sky Bet card count
BACKTEST = {"log-loss": -2.161, "rmse": 2.203}


def latest_before_kickoff(path: str, kickoff: dict[int, str], by_ext: dict[str, int]) -> dict[int, dict]:
    best: dict[int, dict] = {}
    for r in csv.DictReader(open(path)):
        mid = int(r["match_id"])
        if mid not in kickoff and r["external_id"]:  # row merged away since: follow the ESPN event id
            mid = by_ext.get(r["external_id"].split(":", 1)[-1], mid)
        ko = kickoff.get(mid)
        if ko is None or r["logged_at"] >= ko:
            continue
        if mid not in best or r["logged_at"] > best[mid]["logged_at"]:
            best[mid] = r
    return best


def score(db: str, path: str) -> dict[str, dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    kickoff = dict(con.execute("SELECT match_id, kickoff_utc FROM matches").fetchall())
    by_ext = {e.split(":", 1)[-1]: m for m, e in
              con.execute("SELECT match_id, external_id FROM matches WHERE external_id LIKE 'espn%'")}
    cards = {m: (y, sy, sr) for m, y, sy, sr in con.execute(
        """SELECT m.match_id, SUM(c.yellows), SUM(c.second_yellows), SUM(c.straight_reds)
           FROM matches m JOIN match_team_cards c USING (match_id)
           WHERE m.status = 'finished' GROUP BY m.match_id""")}
    con.close()
    acc: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for mid, r in latest_before_kickoff(path, kickoff, by_ext).items():
        if mid not in cards:
            continue
        y, sy, sr = cards[mid]
        actual = (y * float(r["w_yellow"]) + sy * float(r["w_second_yellow"])
                  + sr * float(r["w_straight_red"])) / float(r["w_yellow"])  # count units, as the pmf
        pmf = [float(x) for x in r["pmf"].split()]
        k = int(round(actual))
        p_k = pmf[k] if k < len(pmf) else max(1 - sum(pmf), 1e-6)
        for key in (r["model_version"], "all"):
            a = acc[key]
            a["n"] += 1
            a["ll"] += math.log(max(p_k, 1e-12))
            a["se"] += (actual - float(r["mu_total"]) / float(r["w_yellow"])) ** 2
            a["mu"] += float(r["mu_total"]) / float(r["w_yellow"])
            a["y"] += actual
            for line in LINES:
                p_over = 1 - sum(pmf[:math.floor(line) + 1])
                a["brier"] += (p_over - (actual > line)) ** 2 / len(LINES)
                a[f"p{line}"] += p_over
                a[f"o{line}"] += actual > line
    return acc


def report(acc: dict[str, dict]) -> str:
    if not acc:
        return "No scored matches yet: the log needs finished matches that were projected before kickoff."
    out = []
    for key in sorted(acc, key=lambda k: (k == "all", k)):
        a, n = acc[key], acc[key]["n"]
        out += [f"{key}: {n:.0f} matches",
                f"  log-loss {a['ll'] / n:+.3f} (backtest {BACKTEST['log-loss']:+.3f})   "
                f"RMSE {math.sqrt(a['se'] / n):.3f} (backtest {BACKTEST['rmse']:.3f})   "
                f"Brier {a['brier'] / n:.4f}",
                f"  mean cards: projected {a['mu'] / n:.2f}, actual {a['y'] / n:.2f}",
                "  over rate, projected vs actual: "
                + "  ".join(f"o{L:g} {a[f'p{L}'] / n:.0%}/{a[f'o{L}'] / n:.0%}" for L in LINES)]
    out.append("Under a few hundred matches these figures are noisy; compare with the backtest, not with zero.")
    return "\n".join(out)


if __name__ == "__main__":
    print(report(score(sys.argv[1] if len(sys.argv) > 1 else "cards.db",
                       sys.argv[2] if len(sys.argv) > 2 else "data/projections.csv")))
