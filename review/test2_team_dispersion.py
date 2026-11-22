#!/usr/bin/env python3
"""Post-freeze test 2 (agent/POST_FREEZE.md): team-level dispersion.

  python review/test2_team_dispersion.py [cards.db]

Team and each-team lines are priced with independent Poisson counts per team. Two candidates, fixed in advance:
  A  shared match effect: both teams' rates are multiplied by one gamma factor (mean 1, shape = the league's
     match-total NB size the model already fits). No new parameters, and the match-total distribution is
     exactly the current one; it widens each team's count and makes the two teams' counts move together.
  B  independent negative binomial per team, shape fitted per league by method of moments on team rows
     (same recency weights), no link between the teams.
Scored walk-forward (weekly refit) on 2024-27 against the current pricing: log-loss of the joint (home, away)
outcome, which is what the each-team market settles on, of single team rows, and calibration on the lines
bet. Pass bar (user, 11 Nov): +0.004 log-loss per match. Read-only.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wf import SKY, TUNED, UNTUNED, boot_ci, connect  # noqa: E402
from glm_model import CardGLM, Data, GLMConfig  # noqa: E402

BAR = 0.004


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
        team_size = {}
        for i in set(lg_t.tolist()):
            m = lg_t == i
            mu = np.concatenate([th[m], ta[m]])
            y = np.concatenate([d.y[2 * train[m]], d.y[2 * train[m] + 1]])
            ww = np.concatenate([w[m], w[m]])
            excess = np.sum(ww * ((y - mu) ** 2 - mu))
            team_size[i] = float(np.sum(ww * mu ** 2) / excess) if excess > 0 else None
        mh, ma, sizes = model.predict(idx)
        for j, i in enumerate(idx):
            out.append(dict(season=d.season[i], mu_h=float(mh[j]), mu_a=float(ma[j]), size=sizes[j],
                            tsize=team_size.get(int(d.lg[2 * i])), y_h=int(d.y[2 * i]), y_a=int(d.y[2 * i + 1])))
    return out


def lpois(k, mu):
    return k * math.log(mu) - mu - math.lgamma(k + 1)


def lnb(k, mu, s):
    if s is None:
        return lpois(k, mu)
    return (math.lgamma(s + k) - math.lgamma(s) - math.lgamma(k + 1)
            + s * math.log(s / (s + mu)) + k * math.log(mu / (s + mu)))


def ljoint_shared(h, a, mh, ma, s):
    """Negative multinomial: Poisson(mh*G), Poisson(ma*G) with G ~ Gamma(shape s, mean 1)."""
    if s is None:
        return lpois(h, mh) + lpois(a, ma)
    mt = mh + ma
    return (math.lgamma(s + h + a) - math.lgamma(s) - math.lgamma(h + 1) - math.lgamma(a + 1)
            + s * math.log(s / (s + mt)) + h * math.log(mh / (s + mt)) + a * math.log(ma / (s + mt)))


def joint(r, kind, K=16):
    """Matrix P[h, a] for h, a < K."""
    f = {"base": lambda h, a: lpois(h, r["mu_h"]) + lpois(a, r["mu_a"]),
         "A": lambda h, a: ljoint_shared(h, a, r["mu_h"], r["mu_a"], r["size"]),
         "B": lambda h, a: lnb(h, r["mu_h"], r["tsize"]) + lnb(a, r["mu_a"], r["tsize"])}[kind]
    return np.exp(np.array([[f(h, a) for a in range(K)] for h in range(K)]))


def scores(rows, kind):
    lj, lt = [], []
    for r in rows:
        P = joint(r, kind)
        lj.append(math.log(max(P[r["y_h"], r["y_a"]], 1e-300)) if r["y_h"] < 16 and r["y_a"] < 16 else -30)
        lt.append(math.log(max(P.sum(1)[min(r["y_h"], 15)], 1e-300)) + math.log(max(P.sum(0)[min(r["y_a"], 15)], 1e-300)))
    return np.array(lj), np.array(lt)


def main(db):
    rows = walk(connect(db), TUNED + UNTUNED)
    un = [r for r in rows if r["season"] in UNTUNED]
    sizes = [r["tsize"] for r in un if r["tsize"]]
    print(f"Test 2, team-level dispersion. {len(un)} matches 2024-27; fitted team-row NB shape: median "
          f"{np.median(sizes):.1f} (Poisson = infinite); {sum(r['tsize'] is None for r in un)} matches with none")
    print("  candidate minus current log-loss per match (positive = candidate better), 95% CI")
    bj, bt = scores(un, "base")
    res = {}
    for kind, name in (("A", "A shared match effect"), ("B", "B NB per team")):
        cj, ct = scores(un, kind)
        res[kind] = (cj - bj).mean()
        lo, hi = boot_ci(cj - bj)
        lo2, hi2 = boot_ci(ct - bt)
        print(f"    {name:22s} joint (each-team) {np.mean(cj - bj):+.4f} ({lo:+.4f} to {hi:+.4f})   "
              f"team rows {np.mean(ct - bt):+.4f} ({lo2:+.4f} to {hi2:+.4f})")
        for s in UNTUNED:
            ss = [r for r in un if r["season"] == s]
            a, b = scores(ss, kind)[0], scores(ss, "base")[0]
            print(f"      {s}: joint {np.mean(a - b):+.4f}")
        print(f"      -> {'PASS' if res[kind] >= BAR else 'FAIL'} against the +{BAR} bar (joint outcome)")
    tj, tt = scores([r for r in rows if r["season"] in TUNED], "A")
    bj2, bt2 = scores([r for r in rows if r["season"] in TUNED], "base")
    print(f"    (A on tuned 2022-24: joint {np.mean(tj - bj2):+.4f})")

    print("\n  Calibration 2024-27, projected v actual hit rate")
    mats = {k: [joint(r, k) for r in un] for k in ("base", "A", "B")}
    yh = np.array([r["y_h"] for r in un]); ya = np.array([r["y_a"] for r in un])
    print("    line                    actual   current      A        B")
    for k in (2, 3, 4):
        act = np.mean(np.concatenate([yh >= k, ya >= k]))
        p = {n: np.mean([*[P.sum(1)[k:].sum() for P in m], *[P.sum(0)[k:].sum() for P in m]]) for n, m in mats.items()}
        print(f"    one team {k}+ (o{k - 0.5})       {act:.3f}    {p['base']:.3f}    {p['A']:.3f}    {p['B']:.3f}")
    for k in (1, 2, 3):
        act = np.mean((yh >= k) & (ya >= k))
        p = {n: np.mean([P[k:, k:].sum() for P in m]) for n, m in mats.items()}
        print(f"    each team {k}+               {act:.3f}    {p['base']:.3f}    {p['A']:.3f}    {p['B']:.3f}")
    # where bets come from: high-probability each-team 2+ and one-team 3+
    for k, label in ((2, "each team 2+"),):
        pb = np.array([P[k:, k:].sum() for P in mats["base"]])
        act = ((yh >= k) & (ya >= k)).astype(float)
        top = pb >= np.quantile(pb, 0.8)
        print(f"    {label}, top fifth by current p: actual {act[top].mean():.3f}  current {pb[top].mean():.3f}  "
              f"A {np.mean([mats['A'][i][k:, k:].sum() for i in np.where(top)[0]]):.3f}  "
              f"B {np.mean([mats['B'][i][k:, k:].sum() for i in np.where(top)[0]]):.3f}")
    print(f"\n  correlation of home and away residuals 2024-27: "
          f"{np.corrcoef(yh - [r['mu_h'] for r in un], ya - [r['mu_a'] for r in un])[0, 1]:+.3f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cards.db")
