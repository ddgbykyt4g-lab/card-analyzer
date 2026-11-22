"""Audit items 1-4: look-ahead, validation, model specification, calibration. Read-only.
Usage: python review/audit_model.py cards.db [data/projections.csv]   -> prints a report, saves charts in review/out/
"""
import csv
import math
import sqlite3
import sys
from dataclasses import replace

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from wf import (LINES, OUT, ROOT, SKY, TUNED, UNTUNED, boot_ci, cached_walk, climatology, connect, logloss_total,  # noqa: E402
                metrics, p_over_total, pois_over, total_pmf, walk)

from glm_model import CardGLM, Data, GLMConfig  # noqa: E402
from score_projections import latest_before_kickoff  # noqa: E402

DB = sys.argv[1] if len(sys.argv) > 1 else "cards.db"
LOG = sys.argv[2] if len(sys.argv) > 2 else f"{ROOT}/data/projections.csv"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def reliability(p: np.ndarray, hit: np.ndarray, bins: int = 10) -> list[tuple[float, float, int]]:
    order = np.argsort(p)
    return [(float(p[ix].mean()), float(hit[ix].mean()), len(ix)) for ix in np.array_split(order, bins) if len(ix)]


def logistic_slope(p: np.ndarray, hit: np.ndarray) -> tuple[float, float]:
    """hit ~ a + b*logit(p): b = 1 and a = 0 is perfect calibration; b < 1 means over-confident."""
    x = np.log(p / (1 - p))
    a = b = 0.0
    b = 1.0
    for _ in range(50):
        q = 1 / (1 + np.exp(-(a + b * x)))
        g = np.array([np.sum(hit - q), np.sum((hit - q) * x)])
        w = q * (1 - q)
        H = np.array([[w.sum(), (w * x).sum()], [(w * x).sum(), (w * x * x).sum()]])
        da, db = np.linalg.solve(H, g)
        a, b = a + da, b + db
    return float(a), float(b)


# ───────────────────────────── 1. Look-ahead ─────────────────────────────
section("1a. Look-ahead test: scramble the future, predictions must not move")
src = connect(DB)
base = cached_walk(DB, TUNED + UNTUNED)
weeks = sorted({r["week"] for r in base if r["season"] in UNTUNED})
rng = np.random.default_rng(7)
SCRAMBLE = """UPDATE match_team_cards SET {cols} WHERE match_id IN
              (SELECT match_id FROM matches WHERE date(kickoff_utc) >= date(?, ?))"""
CARDS = "yellows = abs(random()) % 9, second_yellows = 0, straight_reds = abs(random()) % 2"
FOULS = "fouls = abs(random()) % 30"
worst = {"control": 0.0, "future": 0.0, "own": 0.0}
for wk in rng.choice(weeks, 4, replace=False):
    start = int(wk) * 7 - 3
    cut = str(np.datetime64(start, "D"))
    week = [r for r in base if r["week"] == wk]
    first_day = min(r["day"] for r in week)
    res = {}
    for name in worst:
        mem = sqlite3.connect(":memory:")
        src.backup(mem)
        if name == "future":
            # cards of every match from the test week on, and fouls and 1X2 prices of every later week, become
            # noise. (Fouls from earlier in the test week stay: a Saturday bet may use Tuesday's match.)
            mem.execute(SCRAMBLE.format(cols=CARDS), (cut, "+0 day"))
            mem.execute(SCRAMBLE.format(cols=FOULS), (cut, "+7 day"))
            mem.execute("""UPDATE match_prices SET p_home = 0.2, p_draw = 0.2, p_away = 0.6 WHERE match_id IN
                           (SELECT match_id FROM matches WHERE date(kickoff_utc) >= date(?, '+7 day'))""", (cut,))
        elif name == "own":
            # cards AND fouls of the test week itself become noise: matches on its first day must not move
            mem.execute(SCRAMBLE.format(cols=CARDS + ", " + FOULS), (cut, "+0 day"))
        mem.commit()
        alt = {r["match_id"]: r for r in walk(mem, TUNED + UNTUNED, only_weeks={int(wk)})}
        rows = week if name != "own" else [r for r in week if r["day"] == first_day]
        res[name] = max(abs(alt[r["match_id"]]["mu_h"] + alt[r["match_id"]]["mu_a"] - r["mu_h"] - r["mu_a"]) for r in rows)
        worst[name] = max(worst[name], res[name])
    print(f"  week starting {cut} ({len(week)} matches): largest change in projected cards: "
          f"refit only {res['control']:.1e}, future scrambled {res['future']:.1e}, own day scrambled {res['own']:.1e}")
tol = max(1e-6, 10 * worst["control"])
print(f"  -> {'PASS' if max(worst['future'], worst['own']) <= tol else 'FAIL'}: predictions "
      f"{'do not' if max(worst['future'], worst['own']) <= tol else 'DO'} depend on results, cards, fouls or prices from the future "
      f"(tolerance {tol:.0e}, the refit-only difference)")

section("1b. Which 1X2 odds feed the model")
print("  sources in match_prices:", src.execute("SELECT source, COUNT(*) FROM match_prices GROUP BY 1").fetchall())
print("  code: collect_cards.upsert_prices stores football-data's AvgH/AvgD/AvgA (pre-match market average),")
print("        not AvgCH/AvgCD/AvgCA (closing). Live projections made before football-data posts a match use")
print("        ESPN's bookmaker line instead (store_prices, overwrite=False).")

section("1c. Live log against the backtest for the same matches (a leak would make the backtest look better)")
kick = dict(src.execute("SELECT match_id, kickoff_utc FROM matches"))
by_ext = {e.split(":", 1)[-1]: m for m, e in src.execute("SELECT match_id, external_id FROM matches WHERE external_id LIKE 'espn%'")}
teams = {m: (h, a) for m, h, a in src.execute("""SELECT m.match_id, h.name, a.name FROM matches m
    JOIN teams h ON h.team_id = m.home_team_id JOIN teams a ON a.team_id = m.away_team_id""")}
live = latest_before_kickoff(LOG, kick, by_ext, teams)
bt = {r["match_id"]: r for r in base}
pairs = [(float(live[m]["mu_total"]), bt[m]["mu_h"] + bt[m]["mu_a"], bt[m]["y_h"] + bt[m]["y_a"], live[m], bt[m])
         for m in live if m in bt]
lv, bk, y = (np.array([p[i] for p in pairs]) for i in range(3))
ll_live = np.array([math.log(max([float(x) for x in p[3]["pmf"].split()][int(p[2])], 1e-12)) for p in pairs])
ll_bt = np.array([math.log(max(total_pmf(p[4])(int(p[2])), 1e-300)) for p in pairs])
print(f"  {len(pairs)} matches logged before kickoff and since finished")
print(f"  mean projection: live {lv.mean():.3f}, backtest {bk.mean():.3f}, actual {y.mean():.3f}; correlation live/backtest {np.corrcoef(lv, bk)[0, 1]:.3f}")
print(f"  RMSE: live {np.sqrt(np.mean((lv - y) ** 2)):.3f}, backtest {np.sqrt(np.mean((bk - y) ** 2)):.3f}")
lo, hi = boot_ci(ll_bt - ll_live)
print(f"  log-loss: live {ll_live.mean():+.4f}, backtest {ll_bt.mean():+.4f}; backtest minus live {np.mean(ll_bt - ll_live):+.4f} (95% CI {lo:+.4f} to {hi:+.4f})")

# ───────────────────────────── 2. Validation ─────────────────────────────
section("2. Walk-forward results by period (weekly refit on earlier matches only)")
clim = climatology(base, base)
for name, seasons in (("tuned on (2022-24)", TUNED), ("reported as out-of-sample (2024-27)", UNTUNED)):
    rows = [r for r in base if r["season"] in seasons]
    m, c = metrics(rows), metrics([r for r in clim if r["season"] in seasons])
    print(f"  {name}: n={m['n']}  log-loss {m['ll']:+.4f}  RMSE {m['rmse']:.3f}  Brier {m['brier']:.4f}  projected {m['mu']:.2f} v actual {m['y']:.2f}")
    print(f"      league-average baseline: log-loss {c['ll']:+.4f}  RMSE {c['rmse']:.3f}  Brier {c['brier']:.4f}")
for s in UNTUNED:
    rows = [r for r in base if r["season"] == s]
    crow = [r for r in clim if r["season"] == s]
    ids = {r["match_id"] for r in crow}
    m, c = metrics([r for r in rows if r["match_id"] in ids]), metrics(crow)
    print(f"  {s}: n={m['n']}  model log-loss {m['ll']:+.4f} v baseline {c['ll']:+.4f} (gain {m['ll'] - c['ll']:+.4f})  projected {m['mu']:.2f} v actual {m['y']:.2f}")
ids = {r["match_id"] for r in clim if r["season"] in UNTUNED}
gain = np.array([math.log(max(total_pmf(r)(int(r["y_h"] + r["y_a"])), 1e-300)) for r in base if r["match_id"] in ids]) \
    - np.array([math.log(max(total_pmf(r)(int(r["y_h"] + r["y_a"])), 1e-300)) for r in clim if r["match_id"] in ids])
lo, hi = boot_ci(gain)
print(f"  model minus baseline log-loss per match, 2024-27: {gain.mean():+.4f} (95% CI {lo:+.4f} to {hi:+.4f})")

section("2b. Half-life sensitivity (was the tuned value lucky on the later seasons?)")
for hl in (60, 120, 240, 480):
    rows = base if hl == 120 else cached_walk(DB, TUNED + UNTUNED, replace(GLMConfig(), half_life_days=hl), tag=f"hl{hl}")
    a, b = metrics([r for r in rows if r["season"] in TUNED]), metrics([r for r in rows if r["season"] in UNTUNED])
    print(f"  half-life {hl:3d} days: 2022-24 log-loss {a['ll']:+.4f}   2024-27 log-loss {b['ll']:+.4f}")

# ───────────────────────────── 3. Specification ─────────────────────────────
section("3a. Dispersion, out-of-sample 2024-27 (1.00 = Poisson)")
oos = [r for r in base if r["season"] in UNTUNED]
yh, ya = np.array([r["y_h"] for r in oos]), np.array([r["y_a"] for r in oos])
mh, ma = np.array([r["mu_h"] for r in oos]), np.array([r["mu_a"] for r in oos])
team_disp = np.mean(np.concatenate([(yh - mh) ** 2 / mh, (ya - ma) ** 2 / ma]))
tot_disp = np.mean((yh + ya - mh - ma) ** 2 / (mh + ma))
rho = np.corrcoef((yh - mh) / np.sqrt(mh), (ya - ma) / np.sqrt(ma))[0, 1]
lo, hi = boot_ci(np.arange(len(oos)), stat=lambda ix: np.corrcoef(((yh - mh) / np.sqrt(mh))[ix.astype(int)], ((ya - ma) / np.sqrt(ma))[ix.astype(int)])[0, 1])
print(f"  per team: {team_disp:.3f}   match total: {tot_disp:.3f}")
print(f"  correlation between the two teams' residuals in a match: {rho:+.3f} (95% CI {lo:+.3f} to {hi:+.3f})")
print("  NB size used for totals, by league (None = Poisson):",
      {lg: (None if s is None else round(s, 1)) for lg, s in sorted({r['league']: r['size'] for r in oos[-400:]}.items())})
print("  observed v predicted share of match totals, 2024-27:")
tot = (yh + ya).astype(int)
pred = np.zeros(13)
for r in oos:
    pmf = total_pmf(r)
    pred += [pmf(k) for k in range(12)] + [1 - sum(pmf(k) for k in range(12))]
print("   cards:    " + " ".join(f"{k:>5}" for k in list(range(12)) + ["12+"]))
print("   observed: " + " ".join(f"{np.mean(tot == k) if k < 12 else np.mean(tot >= 12):5.3f}" for k in range(13)))
print("   model:    " + " ".join(f"{p / len(oos):5.3f}" for p in pred))

section("3b. Team lines priced as independent Poisson, out-of-sample 2024-27")
for L in (0.5, 1.5, 2.5, 3.5):
    p = np.concatenate([[pois_over(m, L) for m in mh], [pois_over(m, L) for m in ma]])
    hit = np.concatenate([yh > L, ya > L]).astype(float)
    a, b = logistic_slope(np.clip(p, 1e-6, 1 - 1e-6), hit)
    print(f"  team over {L}: predicted {p.mean():.3f}, observed {hit.mean():.3f}, Brier {np.mean((p - hit) ** 2):.4f}, calibration slope {b:.2f}")
both_p = np.array([pois_over(a, 1.5) * pois_over(b, 1.5) for a, b in zip(mh, ma)])
both = ((yh > 1.5) & (ya > 1.5)).astype(float)
print(f"  each team 2+ (priced as a product): predicted {both_p.mean():.3f}, observed {both.mean():.3f}")

section("3c. Coefficients of the current fit, and collinearity of the continuous features")
d = Data(src, weights=SKY, fouls=True)
fin = np.where(d.finished)[0]
model = CardGLM(d, GLMConfig()).fit(fin, int(d.day.max()) + 1)
names = ["win-prob gap (own - opp)", "|gap|", "p(draw)", "log own fouls committed", "log opponent fouls drawn"]
beta = model.beta
print(f"  home team effect: x{math.exp(beta[d.o_home]):.3f} cards")
for n, b in zip(names, beta[d.o_feat:d.o_feat + d.n_feat]):
    print(f"  {n:28} {b:+.3f}")
F = d.feat[np.column_stack([2 * fin, 2 * fin + 1]).ravel()]
corr = np.corrcoef(F.T)
vif = np.diag(np.linalg.inv(corr))
print("  VIF: " + ", ".join(f"{n} {v:.1f}" for n, v in zip(names, vif)))
print(f"  correlation |gap| with p(draw): {corr[1, 2]:+.2f}")
for blk, a, b in (("team discipline", d.o_disc, d.o_prov), ("opponent provocation", d.o_prov, d.o_ref), ("referee", d.o_ref, d.n_par)):
    e = beta[a:b]
    print(f"  {blk}: {b - a} effects, sd {e.std():.3f} (multiplier range x{math.exp(np.percentile(e, 5)):.2f} to x{math.exp(np.percentile(e, 95)):.2f}, 5th-95th pct)")
print("  league intercepts (cards per team at neutral values): " + ", ".join(
    f"{lg} {math.exp(beta[i]):.2f}" for lg, i in sorted(((src.execute('SELECT code FROM leagues WHERE league_id=?', (k,)).fetchone()[0], i) for k, i in d.l_ix.items()))))

# ───────────────────────────── 4. Calibration ─────────────────────────────
section("4. Calibration of match-total overs")
fig, axes = plt.subplots(1, 2, figsize=(9.6, 4.4), dpi=170)
SURF, T2, GRID, BLUE, ORANGE = "#fcfcfb", "#52514e", "#e4e3df", "#2a78d6", "#eb6834"
fig.patch.set_facecolor(SURF)
logged = [(float(x), None) for x in ()]
live_rows = []
for m, r in live.items():
    if m in bt:
        pmf = [float(x) for x in r["pmf"].split()]
        live_rows.append((pmf, bt[m]["y_h"] + bt[m]["y_a"]))
for ax, (title, getp, ys) in zip(axes, (
        ("Walk-forward backtest, 2024-27", lambda L: np.array([p_over_total(r, L) for r in oos]), yh + ya),
        ("Live log (logged before kickoff)", lambda L: np.array([1 - sum(p[:math.floor(L) + 1]) for p, _ in live_rows]),
         np.array([t for _, t in live_rows])))):
    allp, allh = [], []
    for L in LINES:
        p = getp(L)
        hit = (ys > L).astype(float)
        a, b = logistic_slope(np.clip(p, 1e-6, 1 - 1e-6), hit)
        print(f"  {title}: over {L}: n={len(p)} predicted {p.mean():.3f} observed {hit.mean():.3f} Brier {np.mean((p - hit) ** 2):.4f} slope {b:.2f} intercept {a:+.2f}")
        allp.append(p)
        allh.append(hit)
    p, hit = np.concatenate(allp), np.concatenate(allh)
    pts = reliability(p, hit, 10 if len(p) > 3000 else 6)
    ax.plot([0, 1], [0, 1], color=T2, lw=1, ls=(0, (3, 3)))
    ax.plot([x for x, _, _ in pts], [y_ for _, y_, _ in pts], color=BLUE, lw=2, marker="o", ms=5)
    for x, y_, n in pts:
        se = math.sqrt(max(y_ * (1 - y_), 1e-9) / n)
        ax.plot([x, x], [y_ - 1.96 * se, y_ + 1.96 * se], color=BLUE, lw=1)
    ax.set_title(f"{title}\n{len(ys)} matches, overs 2.5-6.5 pooled", fontsize=9, loc="left", color="#0b0b0b")
    ax.set_xlabel("Model probability", fontsize=8, color=T2)
    ax.set_ylabel("Observed frequency", fontsize=8, color=T2)
    ax.set_facecolor(SURF)
    ax.grid(color=GRID, lw=0.6)
    ax.tick_params(labelsize=7, colors=T2)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
fig.tight_layout()
fig.savefig(f"{OUT}/reliability.png", facecolor=SURF)
print(f"  chart: review/out/reliability.png (points = equal-count bins, bars = 95% interval, dashed = perfect)")
m = metrics(oos)
print(f"  backtest 2024-27: log-loss {m['ll']:+.4f}, Brier {m['brier']:.4f}, RMSE {m['rmse']:.3f}")
ys = np.array([t for _, t in live_rows])
print(f"  live log: n={len(live_rows)}, log-loss {np.mean([math.log(max(p[int(t)], 1e-12)) for p, t in live_rows]):+.4f}, "
      f"Brier {np.mean([((1 - sum(p[:math.floor(L) + 1])) - (t > L)) ** 2 for p, t in live_rows for L in LINES]):.4f}, "
      f"projected {np.mean([sum(k * q for k, q in enumerate(p)) for p, _ in live_rows]):.2f} v actual {ys.mean():.2f}")
