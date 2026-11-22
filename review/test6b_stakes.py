#!/usr/bin/env python3
"""Post-freeze test 6b (agent/POST_FREEZE.md): match stakes from league tables built from stored results.

  python review/test6b_stakes.py [cards.db]

Fixed before running (22 Nov 2026). For each team in each match, the table is points (3/1/0) from that
season's finished matches on earlier days. Lines: title (1st/2nd), Europe (EUROPE[league]/next) and relegation
(last safe place/first relegation or play-off place, RELEGATED[league, season] places). Three features per
team row:
  late   the team has at most LATE games left (including this one);
  fight  late, and within FIGHT points of a line: of the team at the line's upper place if it is below the line,
         of the team at the lower place if it is on or above it;
  dead   late, and for every line the gap is more than 3 points x games left, so it can't cross any.
Walk-forward, refit weekly, against the unchanged model. Only 2024-25 and 2025-26 have final rounds among the
untuned seasons. Primary: the +0.004 bar over all matches, 2024-27. Stated before running: the gain on matches
where either team is late. Read-only.
"""
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test1_curved_underdog import extend, ll_total, walk  # noqa: E402
from wf import SKY, TUNED, UNTUNED, boot_ci, connect  # noqa: E402
from glm_model import Data, GLMConfig  # noqa: E402

BAR = 0.004
LATE = 8
FIGHT = 3
EUROPE = {"EPL": 4, "LALIGA": 4, "SERIEA": 4, "BUNDES": 4, "LIGUE1": 4, "SPL": 3}
RELEGATED = {"EPL": 3, "LALIGA": 3, "SERIEA": 3, "BUNDES": 3, "LIGUE1": 3, "SPL": 2}   # play-off place included
RELEGATED_SEASON = {("LIGUE1", "2022-23"): 4}                                          # 20 -> 18 teams


def stake_features(con, d: Data) -> np.ndarray:
    rows = con.execute("""SELECT m.match_id, l.code, s.label, date(m.kickoff_utc), m.home_team_id, m.away_team_id,
                                 m.home_goals, m.away_goals, m.status
                          FROM matches m JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
                          ORDER BY m.kickoff_utc""").fetchall()
    by_season = defaultdict(list)
    for r in rows:
        by_season[(r[1], r[2])].append(r)
    feats = {}
    for (lg, season), ms in by_season.items():
        teams = {t for r in ms for t in (r[4], r[5])}
        n = len(teams)
        games = 38 if lg == "SPL" else 2 * (n - 1)
        rel = RELEGATED_SEASON.get((lg, season), RELEGATED[lg])
        lines = [(1, 2), (EUROPE[lg], EUROPE[lg] + 1), (n - rel, n - rel + 1)]
        pts, played = defaultdict(int), defaultdict(int)
        days = sorted({r[3] for r in ms})
        by_day = defaultdict(list)
        for r in ms:
            by_day[r[3]].append(r)
        for day in days:
            table = sorted(teams, key=lambda t: -pts[t])
            at = {i + 1: pts[t] for i, t in enumerate(table)}
            pos = {t: i + 1 for i, t in enumerate(table)}
            for r in by_day[day]:
                out = []
                for t in (r[4], r[5]):
                    left = games - played[t]
                    late = left <= LATE
                    fight, dead = False, late
                    for up, low in lines:
                        ref = at[up] if pos[t] > up else at[low]
                        gap = abs(pts[t] - ref)
                        if late and gap <= FIGHT:
                            fight = True
                        if gap <= 3 * left:
                            dead = False
                    out.append([float(late), float(fight), float(dead)])
                feats[r[0]] = out
            for r in by_day[day]:
                if r[8] == "finished" and r[6] is not None:
                    h, a = r[4], r[5]
                    played[h] += 1
                    played[a] += 1
                    pts[h] += 3 if r[6] > r[7] else 1 if r[6] == r[7] else 0
                    pts[a] += 3 if r[7] > r[6] else 1 if r[6] == r[7] else 0
    return np.array([f for m in d.match_id for f in feats.get(int(m), [[0.0] * 3, [0.0] * 3])])


def main(db):
    cfg = GLMConfig()
    con = connect(db)
    mk = lambda: Data(con, weights=SKY, fouls=cfg.fouls, fouls_prior=cfg.fouls_prior, half_life=cfg.half_life_days)
    seasons = TUNED + UNTUNED
    base_d = mk()
    F = stake_features(con, base_d)
    base = walk(base_d, seasons)
    cand = walk(extend(mk(), F), seasons)
    late = {int(m): bool(F[2 * i, 0] or F[2 * i + 1, 0]) for i, m in enumerate(base_d.match_id)}
    un_rows = [i for i, m in enumerate(base_d.match_id) if int(m) in base and base[int(m)]["season"] in UNTUNED]
    share = F[np.ravel([[2 * i, 2 * i + 1] for i in un_rows])].mean(0)
    print(f"Test 6b, match stakes. Share of team rows 2024-27: late {share[0]:.0%}, fight {share[1]:.0%}, dead {share[2]:.0%}")
    for label, ss in (("tuned 2022-24", TUNED), ("untuned 2024-27", UNTUNED)) + tuple((s, [s]) for s in UNTUNED):
        ids = [m for m, r in base.items() if r["season"] in ss]
        g = np.array([ll_total(cand[m]) - ll_total(base[m]) for m in ids])
        lids = [m for m in ids if late[m]]
        gl = np.array([ll_total(cand[m]) - ll_total(base[m]) for m in lids])
        lo, hi = boot_ci(g)
        llo, lhi = boot_ci(gl) if len(gl) > 10 else (float("nan"), float("nan"))
        print(f"  {label:16s} all {g.mean():+.4f} ({lo:+.4f} to {hi:+.4f})   late matches (n={len(lids)}) "
              f"{(gl.mean() if len(gl) else float('nan')):+.4f} ({llo:+.4f} to {lhi:+.4f})")
    un = [m for m, r in base.items() if r["season"] in UNTUNED]
    g = np.mean([ll_total(cand[m]) - ll_total(base[m]) for m in un])
    print(f"  -> primary: {'PASS' if g >= BAR else 'FAIL'} against the +{BAR} bar over all matches")

    print("\n  Late matches 2024-27, actual minus projected cards per team row (current / candidate):")
    groups = {"fight": 1, "dead": 2}
    for name, j in groups.items():
        for val in (1.0, 0.0):
            cur, new = [], []
            for i, m in enumerate(base_d.match_id):
                m = int(m)
                if m not in base or base[m]["season"] not in UNTUNED:
                    continue
                for side, s, y in ((0, "mu_h", "y_h"), (1, "mu_a", "y_a")):
                    if F[2 * i + side, 0] and F[2 * i + side, j] == val:
                        cur.append(base[m][y] - base[m][s])
                        new.append(cand[m][y] - cand[m][s])
            if cur:
                lo, hi = boot_ci(cur)
                print(f"    {name}={'yes' if val else 'no ':3s} n={len(cur):4d}  {np.mean(cur):+.3f} (CI {lo:+.3f} to {hi:+.3f})"
                      f"  ->  {np.mean(new):+.3f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cards.db")
