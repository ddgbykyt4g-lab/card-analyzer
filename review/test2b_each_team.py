#!/usr/bin/env python3
"""Post-freeze test 2b (agent/POST_FREEZE.md): dependence between the two teams' cards, marginals unchanged.

  python review/test2b_each_team.py [cards.db]

Test 2 found team rows are Poisson around the model (no overdispersion) but home and away residuals correlate
+0.17, so each-team lines are under-projected. Candidate, fixed before running:
  common-shock bivariate Poisson: home = X1 + X3, away = X2 + X3, X3 ~ Poisson(l3), X1 ~ Poisson(mu_h - l3),
  X2 ~ Poisson(mu_a - l3), so each team's count stays Poisson(mu) exactly. l3 = rho * sqrt(mu_h * mu_a), rho per
  league by method of moments on the training rows (recency-weighted covariance of the residuals over the
  weighted mean of sqrt(mu_h * mu_a)), clipped to [0, 0.9 * min(mu_h, mu_a) / sqrt(mu_h * mu_a)] per match.
Match totals keep the model's negative binomial; this changes team and each-team prices only.
Pass bar (user, 11 Nov): +0.004 joint log-loss per match on 2024-27. Second check, stated before running: does
the each-team 2+ calibration gap (0.363 v 0.396 in test 2) at least halve. Read-only.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wf import SKY, TUNED, UNTUNED, boot_ci, connect  # noqa: E402
from glm_model import CardGLM, Data, GLMConfig  # noqa: E402

BAR = 0.004
K = 16


def walk(con, seasons, cfg=GLMConfig()):
    d = Data(con, weights=SKY, fouls=cfg.fouls, fouls_prior=cfg.fouls_prior, half_life=cfg.half_life_days)
    model = CardGLM(d, cfg)
    test = np.where(d.finished & np.isin(d.season, seasons))[0]
    weeks = (d.day[test] + 3) // 7
    out = []
    for wk in sorted(set(weeks.tolist())):
        idx = test[weeks == wk]
        start = wk * 7 - 3
        train = np.where(d.finished & (d.day < start))[0]
        model.fit(train, start)
        th, ta, _ = model.predict(train)
        w = 0.5 ** ((start - d.day[train]) / cfg.half_life_days)
        lg_t = d.lg[2 * train]
        rho = {}
        for i in set(lg_t.tolist()):
            m = lg_t == i
            cov = np.sum(w[m] * (d.y[2 * train[m]] - th[m]) * (d.y[2 * train[m] + 1] - ta[m])) / w[m].sum()
            scale = np.sum(w[m] * np.sqrt(th[m] * ta[m])) / w[m].sum()
            rho[i] = max(cov / scale, 0.0)
        mh, ma, _ = model.predict(idx)
        for j, i in enumerate(idx):
            r = rho.get(int(d.lg[2 * i]), 0.0)
            g = math.sqrt(mh[j] * ma[j])
            l3 = min(r * g, 0.9 * min(mh[j], ma[j]))
            out.append(dict(season=d.season[i], league=d.league[i], mu_h=float(mh[j]), mu_a=float(ma[j]), l3=l3,
                            rho=r, y_h=int(d.y[2 * i]), y_a=int(d.y[2 * i + 1])))
    return out


def pois(mu):
    k = np.arange(K)
    return np.exp(k * math.log(mu) - mu - np.array([math.lgamma(x + 1) for x in k]))


def joint(r, dep: bool):
    if not dep or r["l3"] <= 0:
        return np.outer(pois(r["mu_h"]), pois(r["mu_a"]))
    p1, p2, p3 = pois(r["mu_h"] - r["l3"]), pois(r["mu_a"] - r["l3"]), pois(r["l3"])
    P = np.zeros((K, K))
    for s in range(K):
        P[s:, s:] += p3[s] * np.outer(p1[:K - s], p2[:K - s])
    return P


def main(db):
    rows = walk(connect(db), TUNED + UNTUNED)
    print("Test 2b, common-shock bivariate Poisson (marginals unchanged)")
    for lg in sorted({r["league"] for r in rows}):
        rr = [r["rho"] for r in rows if r["league"] == lg and r["season"] in UNTUNED]
        print(f"  {lg:7s} fitted rho 2024-27: mean {np.mean(rr):.3f} (range {min(rr):.3f}-{max(rr):.3f})")
    for label, ss in (("tuned 2022-24", TUNED), ("untuned 2024-27", UNTUNED)) + tuple((s, [s]) for s in UNTUNED):
        sub = [r for r in rows if r["season"] in ss]
        g = np.array([math.log(max(joint(r, True)[min(r["y_h"], K - 1), min(r["y_a"], K - 1)], 1e-300))
                      - math.log(max(joint(r, False)[min(r["y_h"], K - 1), min(r["y_a"], K - 1)], 1e-300)) for r in sub])
        lo, hi = boot_ci(g)
        print(f"  {label:16s} joint log-loss gain {g.mean():+.4f} ({lo:+.4f} to {hi:+.4f})")
        if label == "untuned 2024-27":
            verdict = g.mean()
    print(f"  -> {'PASS' if verdict >= BAR else 'FAIL'} against the +{BAR} bar")

    un = [r for r in rows if r["season"] in UNTUNED]
    cur = [joint(r, False) for r in un]
    new = [joint(r, True) for r in un]
    yh = np.array([r["y_h"] for r in un]); ya = np.array([r["y_a"] for r in un])
    print("\n  Calibration 2024-27            actual  current  candidate")
    gaps = {}
    for k in (1, 2, 3):
        act = np.mean((yh >= k) & (ya >= k))
        pc, pn = np.mean([P[k:, k:].sum() for P in cur]), np.mean([P[k:, k:].sum() for P in new])
        gaps[k] = (act - pc, act - pn)
        print(f"    each team {k}+                 {act:.3f}   {pc:.3f}    {pn:.3f}")
    for k in (2, 3, 4):
        act = np.mean(np.concatenate([yh >= k, ya >= k]))
        pc = np.mean([*[P.sum(1)[k:].sum() for P in cur], *[P.sum(0)[k:].sum() for P in cur]])
        pn = np.mean([*[P.sum(1)[k:].sum() for P in new], *[P.sum(0)[k:].sum() for P in new]])
        print(f"    one team {k}+                  {act:.3f}   {pc:.3f}    {pn:.3f}")
    pc2 = np.array([P[2:, 2:].sum() for P in cur]); pn2 = np.array([P[2:, 2:].sum() for P in new])
    act2 = ((yh >= 2) & (ya >= 2)).astype(float)
    for q, name in ((0.8, "top fifth"), (0.5, "top half")):
        top = pc2 >= np.quantile(pc2, q)
        print(f"    each team 2+, {name} by current p: actual {act2[top].mean():.3f}  current {pc2[top].mean():.3f}  "
              f"candidate {pn2[top].mean():.3f}")
    print(f"  -> each team 2+ gap {gaps[2][0]:+.3f} -> {gaps[2][1]:+.3f}: "
          f"{'halved or better' if abs(gaps[2][1]) <= abs(gaps[2][0]) / 2 else 'not halved'}")
    mo = np.mean(pn2 - pc2)
    print(f"  each team 2+ probability, candidate minus current: mean {mo:+.3f}, "
          f"10th-90th pct {np.percentile(pn2 - pc2, 10):+.3f} to {np.percentile(pn2 - pc2, 90):+.3f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cards.db")
