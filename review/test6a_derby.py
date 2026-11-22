#!/usr/bin/env python3
"""Post-freeze test 6a (agent/POST_FREEZE.md): a derby flag.

  python review/test6a_derby.py [cards.db]

DERBIES is fixed here from general football knowledge (city, regional and traditional rivalries), written
before any card data was looked at; it is not to be edited on the results. The candidate adds one feature,
1 for both team rows of a derby (either venue), 0 otherwise, penalised like the other features.

Walk-forward, refit weekly, against the unchanged model on the same matches. Primary: the pass bar (user,
11 Nov) of +0.004 log-loss per match over all matches, 2024-27. Stated before running, because derbies are
about 4% of matches: also the gain on derby matches alone (95% CI) and the projected v actual cards on them.
If those disagree, the user decides. Read-only.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test1_curved_underdog import extend, ll_total, walk  # noqa: E402
from wf import SKY, TUNED, UNTUNED, boot_ci, connect  # noqa: E402
from glm_model import Data, GLMConfig  # noqa: E402

BAR = 0.004
DERBIES = {
    "EPL": [("Arsenal", "Tottenham"), ("Liverpool", "Everton"), ("Man United", "Man City"), ("Liverpool", "Man United"),
            ("Chelsea", "Tottenham"), ("Chelsea", "Arsenal"), ("West Ham", "Tottenham"), ("Newcastle", "Sunderland"),
            ("Crystal Palace", "Brighton"), ("Leeds", "Man United")],
    "LALIGA": [("Real Madrid", "Ath Madrid"), ("Real Madrid", "Barcelona"), ("Barcelona", "Espanol"),
               ("Sevilla", "Betis"), ("Ath Bilbao", "Sociedad"), ("Valencia", "Villarreal"), ("Valencia", "Levante"),
               ("Celta", "La Coruna")],
    "SERIEA": [("Inter", "Milan"), ("Roma", "Lazio"), ("Juventus", "Torino"), ("Genoa", "Sampdoria"),
               ("Juventus", "Inter"), ("Napoli", "Roma")],
    "BUNDES": [("Dortmund", "Schalke 04"), ("Dortmund", "Bayern Munich"), ("Hamburg", "Werder Bremen"),
               ("FC Koln", "M'gladbach"), ("FC Koln", "Leverkusen"), ("Hamburg", "St Pauli"),
               ("Union Berlin", "Hertha"), ("Ein Frankfurt", "Darmstadt")],
    "LIGUE1": [("Paris SG", "Marseille"), ("Lyon", "St Etienne"), ("Nice", "Monaco"), ("Lens", "Lille"),
               ("Rennes", "Nantes"), ("Rennes", "Brest"), ("Rennes", "Lorient"), ("Brest", "Lorient"),
               ("Marseille", "Lyon"), ("Paris SG", "Paris FC")],
    "SPL": [("Celtic", "Rangers"), ("Hearts", "Hibernian"), ("Dundee", "Dundee United"), ("Aberdeen", "Rangers")],
}


def derby_column(con, d: Data) -> np.ndarray:
    names = dict(con.execute("SELECT team_id, name FROM teams"))
    pairs = {(lg, frozenset(p)) for lg, ps in DERBIES.items() for p in ps}
    home_away = dict((m, (h, a)) for m, h, a in con.execute("SELECT match_id, home_team_id, away_team_id FROM matches"))
    flag = np.array([float((d.league[i], frozenset(names[t] for t in home_away[int(m)])) in pairs)
                     for i, m in enumerate(d.match_id)])
    return np.repeat(flag, 2)[:, None]


def main(db):
    cfg = GLMConfig()
    con = connect(db)
    mk = lambda: Data(con, weights=SKY, fouls=cfg.fouls, fouls_prior=cfg.fouls_prior, half_life=cfg.half_life_days)
    seasons = TUNED + UNTUNED
    base_d = mk()
    col = derby_column(con, base_d)
    names_found = {n for lg, ps in DERBIES.items() for p in ps for n in p}
    missing = names_found - {n for (n,) in con.execute("SELECT name FROM teams")}
    if missing:
        raise SystemExit(f"derby names not in the database: {sorted(missing)}")
    base = walk(base_d, seasons)
    cand_d = extend(mk(), col)
    cand = walk(cand_d, seasons)
    is_derby = {int(m): bool(col[2 * i, 0]) for i, m in enumerate(base_d.match_id)}

    print(f"Test 6a, derby flag: {sum(len(v) for v in DERBIES.values())} pairs fixed in advance")
    for label, ss in (("tuned 2022-24", TUNED), ("untuned 2024-27", UNTUNED)) + tuple((s, [s]) for s in UNTUNED):
        ids = [m for m, r in base.items() if r["season"] in ss]
        g = np.array([ll_total(cand[m]) - ll_total(base[m]) for m in ids])
        dids = [m for m in ids if is_derby[m]]
        gd = np.array([ll_total(cand[m]) - ll_total(base[m]) for m in dids])
        lo, hi = boot_ci(g)
        dlo, dhi = boot_ci(gd) if len(gd) > 10 else (float("nan"), float("nan"))
        print(f"  {label:16s} all matches {g.mean():+.4f} ({lo:+.4f} to {hi:+.4f})   derbies only (n={len(dids)}) "
              f"{gd.mean():+.4f} ({dlo:+.4f} to {dhi:+.4f})")
    un = [m for m, r in base.items() if r["season"] in UNTUNED]
    g = np.mean([ll_total(cand[m]) - ll_total(base[m]) for m in un])
    print(f"  -> primary: {'PASS' if g >= BAR else 'FAIL'} against the +{BAR} bar over all matches")

    print("\n  Derby matches 2024-27, actual v projected cards (current, candidate):")
    for lg in DERBIES:
        dm = [m for m in un if is_derby[m] and base[m].get("league", lg) == lg]
    rows = [(m, base[m]) for m in un if is_derby[m]]
    y = np.array([r["y_h"] + r["y_a"] for _, r in rows])
    mb = np.array([r["mu_h"] + r["mu_a"] for _, r in rows])
    mc = np.array([cand[m]["mu_h"] + cand[m]["mu_a"] for m, _ in rows])
    lo, hi = boot_ci(y - mb)
    print(f"    n={len(rows)}  actual {y.mean():.2f}  current {mb.mean():.2f} (gap {np.mean(y - mb):+.2f}, CI {lo:+.2f} to {hi:+.2f})"
          f"  candidate {mc.mean():.2f} (gap {np.mean(y - mc):+.2f})")
    nd = [r for m, r in base.items() if r["season"] in UNTUNED and not is_derby[m]]
    print(f"    non-derbies: actual {np.mean([r['y_h'] + r['y_a'] for r in nd]):.2f}  current "
          f"{np.mean([r['mu_h'] + r['mu_a'] for r in nd]):.2f}")
    k = cand_d.o_feat + cand_d.n_feat - 1
    print(f"  (fitted derby effect at the last refit: x{math.exp(walk_last_beta(cand_d, k)):.3f} per team)")


def walk_last_beta(d, k):
    from glm_model import CardGLM
    m = CardGLM(d, GLMConfig())
    last = int(d.day[d.finished].max()) + 1
    m.fit(np.where(d.finished)[0], last)
    return float(m.beta[k])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cards.db")
