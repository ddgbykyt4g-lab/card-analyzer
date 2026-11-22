#!/usr/bin/env python3
"""Post-freeze test 5 (agent/POST_FREEZE.md): the referee's card-per-foul conversion, interacting with the
teams' foul rates.

  python review/test5_ref_conversion.py [cards.db]

Fixed before running (22 Nov 2026). conv = log(referee's cards per foul / league's), from every finished match
on earlier days, the referee shrunk toward the league by PRIOR matches' worth of league-average fouls and
cards; 0 with no referee. Two features per team row: conv x the team's fouls-committed term and conv x the
opponent's fouls-drawn term (the model's existing pre-match foul features). The referee's flat card level is
already in the model; this asks whether a strict referee costs foul-prone teams more than that.
(Distinct from the rejected referee foul-rate term, which was about how many fouls a referee gives.)
Walk-forward, refit weekly, against the unchanged model. Pass bar: +0.004 log-loss a match on 2024-27. Read-only.
"""
import math
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test1_curved_underdog import extend, ll_total, walk  # noqa: E402
from wf import SKY, TUNED, UNTUNED, boot_ci, connect  # noqa: E402
from glm_model import BASE_FEAT, Data, GLMConfig  # noqa: E402

BAR = 0.004
PRIOR = 20.0


def conversion(con) -> dict[int, float]:
    rows = con.execute("""
        SELECT m.match_id, date(m.kickoff_utc), l.code, m.referee_id, m.status,
               SUM(c.yellows + c.second_yellows + c.straight_reds), SUM(c.fouls)
        FROM matches m JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
        LEFT JOIN match_team_cards c USING (match_id)
        GROUP BY m.match_id ORDER BY m.kickoff_utc""").fetchall()
    lc, lf, ln = defaultdict(float), defaultdict(float), defaultdict(int)       # league cards, fouls, matches
    rc, rf, rn = defaultdict(float), defaultdict(float), defaultdict(int)       # referee
    out, by_day = {}, defaultdict(list)
    for r in rows:
        by_day[r[1]].append(r)
    for day in sorted(by_day):
        for m, _, lg, ref, *_ in by_day[day]:
            if ref is None or ln[lg] < 50 or lf[lg] <= 0:
                out[m] = 0.0
                continue
            league_rate = lc[lg] / lf[lg]
            fbar = lf[lg] / ln[lg]
            rate = (rc[ref] + PRIOR * fbar * league_rate) / (rf[ref] + PRIOR * fbar)
            out[m] = math.log(rate / league_rate)
        for m, _, lg, ref, status, cards, fouls in by_day[day]:
            if status == "finished" and cards is not None and fouls:   # fouls missing or 0: no information
                lc[lg] += cards; lf[lg] += fouls; ln[lg] += 1
                if ref is not None:
                    rc[ref] += cards; rf[ref] += fouls; rn[ref] += 1
    return out


def main(db):
    cfg = GLMConfig()
    con = connect(db)
    mk = lambda: Data(con, weights=SKY, fouls=cfg.fouls, fouls_prior=cfg.fouls_prior, half_life=cfg.half_life_days)
    seasons = TUNED + UNTUNED
    base_d = mk()
    conv = conversion(con)
    c = np.repeat([conv.get(int(m), 0.0) for m in base_d.match_id], 2)
    fc, fd = base_d.feat[:, BASE_FEAT], base_d.feat[:, BASE_FEAT + 1]   # own fouls committed, opponent fouls drawn
    F = np.column_stack([c * fc, c * fd])
    base = walk(base_d, seasons)
    cand_d = extend(mk(), F)
    cand = walk(cand_d, seasons)
    vals = np.array([v for v in conv.values() if v])
    print(f"Test 5, referee conversion x team fouls. Referee conv (log ratio to league): sd {vals.std():.3f}, "
          f"10th-90th pct {np.percentile(vals, 10):+.3f} to {np.percentile(vals, 90):+.3f}")
    for label, ss in (("tuned 2022-24", TUNED), ("untuned 2024-27", UNTUNED)) + tuple((s, [s]) for s in UNTUNED):
        ids = [m for m, r in base.items() if r["season"] in ss]
        g = np.array([ll_total(cand[m]) - ll_total(base[m]) for m in ids])
        lo, hi = boot_ci(g)
        print(f"  {label:16s} {g.mean():+.4f} ({lo:+.4f} to {hi:+.4f})")
    un = [m for m, r in base.items() if r["season"] in UNTUNED]
    g = np.mean([ll_total(cand[m]) - ll_total(base[m]) for m in un])
    print(f"  -> {'PASS' if g >= BAR else 'FAIL'} against the +{BAR} bar")
    from glm_model import CardGLM
    m = CardGLM(cand_d, cfg).fit(np.where(cand_d.finished)[0], int(cand_d.day[cand_d.finished].max()) + 1)
    k = cand_d.o_feat + cand_d.n_feat - 2
    print(f"  fitted interactions at the last refit: conv x own fouls {m.beta[k]:+.3f}, conv x opponent drawn {m.beta[k + 1]:+.3f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cards.db")
