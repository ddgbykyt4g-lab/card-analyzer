#!/usr/bin/env python3
"""Post-freeze test 1 (agent/POST_FREEZE.md): a curved underdog term.

  python review/test1_curved_underdog.py [cards.db]

The 1X2 context enters as gap = p_own_win - p_opp_win and |gap| (plus p_draw), so cards rise linearly with
how big an underdog a team is. The candidate adds gap*|gap| (fixed in advance); gap**3 is reported as a
sensitivity check, not used to choose. Walk-forward, refit weekly, against the unchanged model on the same
matches. Also reports team-level fit and the bias for heavy underdogs (own win probability under 20%), the
case the shortlist caution exists for. Pass bar (user, 11 Nov): +0.004 log-loss per match on 2024-27. Read-only.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wf import SKY, TUNED, UNTUNED, boot_ci, connect, total_pmf  # noqa: E402
from glm_model import CardGLM, Data, GLMConfig  # noqa: E402

BAR = 0.004


def extend(d: Data, cols: np.ndarray) -> Data:
    """Append feature columns (one value per team row) and shift the parameter layout."""
    k = cols.shape[1]
    d.feat = np.column_stack([d.feat, cols])
    d.n_feat += k
    d.o_disc += k
    d.o_prov += k
    d.o_ref += k
    d.n_par += k
    return d


def walk(d: Data, seasons, cfg=GLMConfig()):
    model = CardGLM(d, cfg)
    test = np.where(d.finished & np.isin(d.season, seasons))[0]
    weeks = (d.day[test] + 3) // 7
    out = {}
    for wk in sorted(set(weeks.tolist())):
        idx = test[weeks == wk]
        start = wk * 7 - 3
        model.fit(np.where(d.finished & (d.day < start))[0], start)
        mh, ma, sizes = model.predict(idx)
        for j, i in enumerate(idx):
            out[int(d.match_id[i])] = dict(season=d.season[i], mu_h=float(mh[j]), mu_a=float(ma[j]), size=sizes[j],
                                           y_h=float(d.y[2 * i]), y_a=float(d.y[2 * i + 1]), i=int(i))
    return out


def ll_total(r):
    return math.log(max(total_pmf(r)(int(r["y_h"] + r["y_a"])), 1e-300))


def ll_pois(mu, y):
    return y * math.log(mu) - mu - math.lgamma(y + 1)


def main(db):
    cfg = GLMConfig()
    con = connect(db)
    mk = lambda: Data(con, weights=SKY, fouls=cfg.fouls, fouls_prior=cfg.fouls_prior, half_life=cfg.half_life_days)
    base_d = mk()
    gap = base_d.feat[:, 0]
    seasons = TUNED + UNTUNED
    base = walk(base_d, seasons)
    cands = {"gap*|gap| (primary)": walk(extend(mk(), (gap * np.abs(gap))[:, None]), seasons),
             "gap**3 (sensitivity)": walk(extend(mk(), (gap ** 3)[:, None]), seasons)}
    # own win probability per team row, to find heavy underdogs
    rows = con.execute("""SELECT m.match_id, p.p_home, p.p_away FROM matches m JOIN match_prices p USING (match_id)""").fetchall()
    pwin = {m: (h, a) for m, h, a in rows}

    print("Test 1, curved underdog term. Candidate minus current, per match (positive = candidate better), 95% CI")
    for name, cand in cands.items():
        print(f"  {name}")
        for label, ss in (("tuned 2022-24", TUNED), ("untuned 2024-27", UNTUNED)) + tuple((s, [s]) for s in UNTUNED):
            ids = [m for m, r in base.items() if r["season"] in ss]
            g = np.array([ll_total(cands[name][m]) - ll_total(base[m]) for m in ids])
            gt = np.array([sum(ll_pois(cand[m][s], cand[m][y]) - ll_pois(base[m][s], base[m][y])
                               for s, y in (("mu_h", "y_h"), ("mu_a", "y_a"))) for m in ids])
            lo, hi = boot_ci(g)
            print(f"    {label:16s} match total {g.mean():+.4f} ({lo:+.4f} to {hi:+.4f})   both team rows {gt.mean():+.4f}")
        un = [m for m, r in base.items() if r["season"] in UNTUNED]
        g = np.mean([ll_total(cand[m]) - ll_total(base[m]) for m in un])
        print(f"    -> {'PASS' if g >= BAR else 'FAIL'} against the +{BAR} bar")

    print("\n  Team rows by own win probability, untuned 2024-27: actual minus projected cards (current / primary)")
    prim = cands["gap*|gap| (primary)"]
    bands = ((0, .2), (.2, .35), (.35, .5), (.5, .65), (.65, 1.01))
    for lo_p, hi_p in bands:
        cur, new, n = [], [], 0
        for m, r in base.items():
            if r["season"] not in UNTUNED or m not in pwin:
                continue
            for side, s, y in ((0, "mu_h", "y_h"), (1, "mu_a", "y_a")):
                p = pwin[m][side]
                if p is not None and lo_p <= p < hi_p:
                    cur.append(r[y] - r[s]); new.append(prim[m][y] - prim[m][s]); n += 1
        print(f"    win p {lo_p:.2f}-{min(hi_p, 1):.2f}: n={n:5d}  {np.mean(cur):+.3f}  ->  {np.mean(new):+.3f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cards.db")
