#!/usr/bin/env python3
"""Post-freeze test 3 (agent/POST_FREEZE.md): a fast league level.

  python review/test3_league_level.py [cards.db]

The live model's league intercepts share the 120-day half-life of the team and referee effects, so a
league-wide shift this season takes months to show. The candidate keeps the fitted model as it is and
multiplies each league's projections by a short-memory correction: the league's actual over fitted cards in
its training matches, weighted with a 30-day half-life and shrunk toward 1 by PRIOR matches' worth of
evidence. Both settings are fixed here in advance (not tuned); the sensitivity table is reported, not
used to choose.

Walk-forward, refit weekly, compared with the unchanged model on the same matches. Pass bar (decided
11 Nov): +0.004 log-loss per match on 2024-27, then log-only until a confirmation read. Read-only.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wf import SKY, TUNED, UNTUNED, boot_ci, connect, total_pmf  # noqa: E402
from glm_model import CardGLM, Data, GLMConfig  # noqa: E402

FAST_HL = 30.0
PRIOR = 30.0
BAR = 0.004


def walk(con, seasons, settings, cfg=GLMConfig()):
    """Rows per match: base mu and, for each (half-life, prior) in settings, the corrected mu."""
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
        fit_t, y_t = th + ta, d.y[2 * train] + d.y[2 * train + 1]
        lg_t, age = d.lg[2 * train], start - d.day[train]
        mh, ma, sizes = model.predict(idx)
        factors = {}
        for hl, prior in settings:
            w = 0.5 ** (age / hl)
            f = {}
            for i in set(lg_t.tolist()):
                m = lg_t == i
                n_eff = w[m].sum()
                ratio = (w[m] * y_t[m]).sum() / (w[m] * fit_t[m]).sum()
                f[i] = (n_eff * ratio + prior) / (n_eff + prior)
            factors[(hl, prior)] = f
        for j, i in enumerate(idx):
            lg = int(d.lg[2 * i])
            r = dict(match_id=int(d.match_id[i]), day=int(d.day[i]), league=d.league[i], season=d.season[i],
                     mu_h=float(mh[j]), mu_a=float(ma[j]), size=sizes[j], y_h=float(d.y[2 * i]), y_a=float(d.y[2 * i + 1]))
            r["fac"] = {k: v.get(lg, 1.0) for k, v in factors.items()}
            out.append(r)
    return out


def ll(mu, size, y):
    return math.log(max(total_pmf(dict(mu_h=mu / 2, mu_a=mu / 2, size=size))(int(y)), 1e-300))


def report(rows, key, label):
    base = np.array([ll(r["mu_h"] + r["mu_a"], r["size"], r["y_h"] + r["y_a"]) for r in rows])
    cand = np.array([ll((r["mu_h"] + r["mu_a"]) * r["fac"][key], r["size"], r["y_h"] + r["y_a"]) for r in rows])
    g = cand - base
    lo, hi = boot_ci(g)
    return g.mean(), lo, hi


def main(db):
    settings = [(FAST_HL, PRIOR)] + [(hl, p) for hl in (15, 30, 60) for p in (10, 30, 100) if (hl, p) != (FAST_HL, PRIOR)]
    rows = walk(connect(db), TUNED + UNTUNED, settings)
    key = (FAST_HL, PRIOR)
    print(f"Test 3, fast league level: half-life {FAST_HL:.0f} days, prior {PRIOR:.0f} matches (fixed in advance)")
    print("  candidate minus current log-loss per match (positive = candidate better), 95% CI")
    for name, ss in (("tuned seasons 2022-24", TUNED), ("untuned 2024-27", UNTUNED)):
        g, lo, hi = report([r for r in rows if r["season"] in ss], key, name)
        print(f"    {name}: {g:+.4f} ({lo:+.4f} to {hi:+.4f})")
    for s in UNTUNED:
        g, lo, hi = report([r for r in rows if r["season"] == s], key, s)
        print(f"      {s}: {g:+.4f} ({lo:+.4f} to {hi:+.4f})")
    un = [r for r in rows if r["season"] in UNTUNED]
    g, _, _ = report(un, key, "")
    print(f"  -> {'PASS' if g >= BAR else 'FAIL'} against the +{BAR} bar")

    print("\n  2026-27 mean bias by league (actual minus projected), current v candidate:")
    cur = [r for r in rows if r["season"] == "2026-27"]
    for lg in sorted({r["league"] for r in cur}):
        rr = [r for r in cur if r["league"] == lg]
        y = np.mean([r["y_h"] + r["y_a"] for r in rr])
        b = np.mean([r["mu_h"] + r["mu_a"] for r in rr])
        c = np.mean([(r["mu_h"] + r["mu_a"]) * r["fac"][key] for r in rr])
        print(f"    {lg:7s} n={len(rr):3d}  {y - b:+.2f}  ->  {y - c:+.2f}")

    print("\n  Sensitivity, untuned 2024-27 (reported, not used to choose):")
    for k in settings:
        g, lo, hi = report(un, k, "")
        print(f"    half-life {k[0]:3.0f}, prior {k[1]:3.0f}: {g:+.4f} ({lo:+.4f} to {hi:+.4f})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cards.db")
