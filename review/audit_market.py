"""Audit items 5-7: model against the bookmaker, backtest realism, staking. Read-only.
Usage: python review/audit_market.py cards.db [data/projections.csv] [data/espn_cards.csv]

The only odds in the repo are Sky Bet card prices transcribed from the user's screenshots (odds_snapshots).
De-vigging: proportional (each side's 1/odds divided by their sum); the power method is shown as a check.
Model probability for a price = the projection logged before that price was captured (data/projections.csv)
when there is one, otherwise the model refitted on matches before the capture day ("reconstructed").
"""
import csv
import math
import sqlite3
import sys
from collections import defaultdict

import numpy as np

from wf import ROOT, SKY, boot_ci, connect, pois_over  # noqa: E402

from check_prices import verdicts  # noqa: E402
from glm_model import GLMPredictor  # noqa: E402
from value_finder import make_pmf  # noqa: E402

DB = sys.argv[1] if len(sys.argv) > 1 else "cards.db"
LOG = sys.argv[2] if len(sys.argv) > 2 else f"{ROOT}/data/projections.csv"
ESPN = sys.argv[3] if len(sys.argv) > 3 else f"{ROOT}/data/espn_cards.csv"
rng = np.random.default_rng(11)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def devig(over: float, under: float) -> tuple[float, float]:
    a, b = 1 / over, 1 / under
    return a / (a + b), a + b - 1


def devig_power(over: float, under: float) -> float:
    a, b = 1 / over, 1 / under
    lo, hi = 0.5, 3.0
    for _ in range(60):
        k = (lo + hi) / 2
        lo, hi = (k, hi) if a ** k + b ** k > 1 else (lo, k)
    return a ** k


def cluster_boot(values: np.ndarray, groups: np.ndarray, stat=np.mean, n: int = 4000) -> tuple[float, float]:
    ids = np.unique(groups)
    by = {g: values[groups == g] for g in ids}
    draws = [stat(np.concatenate([by[g] for g in rng.choice(ids, len(ids))])) for _ in range(n)]
    return float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return y * np.log(p) + (1 - y) * np.log(1 - p)


con = connect(DB)
con.row_factory = sqlite3.Row

# ── outcomes: Sky Bet count per team; ESPN's count where it has the match and differs (see CLAUDE.md, two sources) ──
espn = {}
try:
    espn = {r["espn_id"]: r for r in csv.DictReader(open(ESPN))}
except FileNotFoundError:
    pass
actual, swapped = {}, 0
for m in con.execute("""SELECT m.match_id, m.external_id,
        (SELECT yellows + second_yellows + straight_reds FROM match_team_cards c WHERE c.match_id = m.match_id AND c.team_id = m.home_team_id) h,
        (SELECT yellows + second_yellows + straight_reds FROM match_team_cards c WHERE c.match_id = m.match_id AND c.team_id = m.away_team_id) a
        FROM matches m WHERE m.status = 'finished'"""):
    if m["h"] is None or m["a"] is None:
        continue
    h, a = m["h"], m["a"]
    e = espn.get((m["external_id"] or "").rsplit(":", 1)[-1])
    if e:
        eh, ea = int(e["home_yellows"]) + int(e["home_reds"]), int(e["away_yellows"]) + int(e["away_reds"])
        swapped += (eh, ea) != (h, a)
        h, a = eh, ea
    actual[m["match_id"]] = dict(home=h, away=a, total=h + a)

# ── prices ──
snaps = [dict(r) for r in con.execute("""
    SELECT o.snapshot_id, o.match_id, o.line, o.over_odds, o.under_odds, o.captured_at, mk.code, m.kickoff_utc,
           h.name || ' v ' || a.name AS fixture, p.p_home, p.p_away, l.code AS league
    FROM odds_snapshots o JOIN markets mk USING (market_id) JOIN bookmakers b USING (bookmaker_id)
    JOIN matches m ON m.match_id = o.match_id JOIN seasons se ON se.season_id = m.season_id
    JOIN leagues l ON l.league_id = se.league_id JOIN teams h ON h.team_id = m.home_team_id
    JOIN teams a ON a.team_id = m.away_team_id LEFT JOIN match_prices p ON p.match_id = m.match_id
    WHERE b.name = 'Sky Bet' AND mk.code IN ('TOTAL_CARDS', 'HOME_CARDS', 'AWAY_CARDS') AND o.captured_at < m.kickoff_utc
    ORDER BY o.captured_at""")]
SIDE = {"TOTAL_CARDS": "total", "HOME_CARDS": "home", "AWAY_CARDS": "away"}

# ── model probabilities at capture time ──
logged = defaultdict(list)
ext = {m: (e or "").rsplit(":", 1)[-1] for m, e in con.execute("SELECT match_id, external_id FROM matches")}
by_ext = {v: k for k, v in ext.items() if v}
for r in csv.DictReader(open(LOG)):
    mid = by_ext.get((r["external_id"] or "").rsplit(":", 1)[-1])
    if mid is not None and r["mu_home"]:
        logged[mid].append(r)
recon: dict[str, GLMPredictor] = {}


def model_p(s: dict) -> tuple[float, str] | None:
    side = SIDE[s["code"]]
    rows = [r for r in logged.get(s["match_id"], []) if r["logged_at"] <= s["captured_at"]]
    if rows:
        r = max(rows, key=lambda r: r["logged_at"])
        if side == "total":
            pmf = [float(x) for x in r["pmf"].split()]
            return 1 - sum(pmf[:math.floor(s["line"]) + 1]), "logged"
        return pois_over(float(r[f"mu_{side}"]), s["line"]), "logged"
    day = s["captured_at"][:10]
    if day not in recon:
        recon[day] = GLMPredictor(con, s["captured_at"], weights=SKY)
    g = recon[day].predict(s["match_id"])
    if g is None:
        return None
    if side == "total":
        pmf = make_pmf(g["mean"], g["size"])
        return 1 - sum(pmf(k) for k in range(math.floor(s["line"]) + 1)), "reconstructed"
    return pois_over(g[side], s["line"]), "reconstructed"


latest: dict[tuple, dict] = {}
history = defaultdict(list)
for s in snaps:
    key = (s["match_id"], s["code"], s["line"])
    history[key].append(s)
    latest[key] = s  # ordered by captured_at: the last one before kickoff
rows = []
for s in latest.values():
    if s["match_id"] not in actual:
        continue
    mp = model_p(s)
    if mp is None:
        continue
    side = SIDE[s["code"]]
    rows.append(dict(s, side=side, p=mp[0], src=mp[1], y=float(actual[s["match_id"]][side] > s["line"]),
                     underdog=side != "total" and (s["p_home"] if side == "home" else s["p_away"]) is not None
                     and (s["p_home"] if side == "home" else s["p_away"]) < 0.20))

section("5a. What there is to benchmark against")
print(f"  Sky Bet price rows captured before kickoff: {len(snaps)}; distinct match/market/line: {len(latest)}; "
      f"settled and priced by the model: {len(rows)} lines on {len({r['match_id'] for r in rows})} matches")
print(f"  model probability from the pre-kickoff log: {sum(r['src'] == 'logged' for r in rows)}; reconstructed: {sum(r['src'] == 'reconstructed' for r in rows)}")
print(f"  outcomes taken from ESPN where it differs from football-data: {swapped} matches in the ESPN file")
print(f"  closing odds stored on any bet: {con.execute('SELECT COUNT(closing_odds) FROM value_bets').fetchone()[0]} "
      "(no closing-line value can be computed; the Betfair logger that would supply it is not merged)")
two = [r for r in rows if r["over_odds"] and r["under_odds"]]
for r in two:
    r["mkt"], r["over_round"] = devig(r["over_odds"], r["under_odds"])
    r["mkt_pow"] = devig_power(r["over_odds"], r["under_odds"])
for side in ("total", "home", "away"):
    v = [r["over_round"] for r in two if r["side"] == side]
    if v:
        print(f"  bookmaker margin, {side} lines with both prices: mean {np.mean(v):.1%} (n={len(v)})")

section("5b. Model against de-vigged Sky Bet, same lines, settled matches")


def compare(sub: list[dict], label: str) -> None:
    if len(sub) < 10:
        print(f"  {label}: only {len(sub)} lines")
        return
    y = np.array([r["y"] for r in sub])
    pm, pk, pw = (np.array([r[k] for r in sub]) for k in ("p", "mkt", "mkt_pow"))
    g = np.array([r["match_id"] for r in sub])
    d = ll(pm, y) - ll(pk, y)
    lo, hi = cluster_boot(d, g)
    print(f"  {label}: {len(sub)} lines, {len(set(g))} matches")
    print(f"     log-loss  model {ll(pm, y).mean():+.4f}   market {ll(pk, y).mean():+.4f} (power de-vig {ll(pw, y).mean():+.4f})"
          f"   model minus market {d.mean():+.4f} (95% CI {lo:+.4f} to {hi:+.4f}; above 0 = model better)")
    print(f"     Brier     model {np.mean((pm - y) ** 2):.4f}   market {np.mean((pk - y) ** 2):.4f}"
          f"   mean prob: model {pm.mean():.3f}, market {pk.mean():.3f}, observed {y.mean():.3f}")
    # how much weight on the model minimises log-loss when blended with the market (logit scale)
    lm, lk = np.log(pm / (1 - pm)), np.log(pk / (1 - pk))
    grid = np.linspace(0, 1, 21)

    def best(ix=None):
        ix = slice(None) if ix is None else ix
        return grid[int(np.argmax([ll(1 / (1 + np.exp(-(w * lm[ix] + (1 - w) * lk[ix]))), y[ix]).mean() for w in grid]))]
    ids = np.unique(g)
    draws = []
    for _ in range(1000):
        pick = rng.choice(ids, len(ids))
        draws.append(best(np.concatenate([np.where(g == i)[0] for i in pick])))
    print(f"     best blend weight on the model: {best():.2f} (95% CI {np.percentile(draws, 2.5):.2f} to {np.percentile(draws, 97.5):.2f}; "
          "0 = market alone, 1 = model alone)")


mid = [r for r in two if 0.10 <= r["mkt"] <= 0.90]
compare(mid, "all lines the market puts between 10% and 90%")
compare([r for r in mid if r["side"] == "total"], "match totals")
compare([r for r in mid if r["side"] != "total"], "team lines")
compare([r for r in mid if r["src"] == "logged"], "only prices with a logged pre-kickoff projection")
main_line = {}
for r in mid:
    k = (r["match_id"], r["side"])
    if k not in main_line or abs(r["mkt"] - 0.5) < abs(main_line[k]["mkt"] - 0.5):
        main_line[k] = r
compare(list(main_line.values()), "one line per match and market (nearest to 50%)")

section("5b-ii. Were overs simply underpriced in these matches? One line per match (its total, else a team line)")
one = {}
for (m_, side), r in sorted(main_line.items(), key=lambda kv: kv[0][1] != "total"):
    one.setdefault(m_, r)
one = list(one.values())
y1, k1, p1 = (np.array([r[k] for r in one]) for k in ("y", "mkt", "p"))
sim = (rng.random((200000, len(one))) < k1).sum(axis=1)
sim_m = (rng.random((200000, len(one))) < p1).sum(axis=1)
print(f"  {len(one)} matches: overs landed in {int(y1.sum())}; Sky Bet's de-vigged prices expected {k1.sum():.1f}, the model {p1.sum():.1f}")
print(f"  chance of {int(y1.sum())}+ if Sky Bet's prices were right: {np.mean(sim >= y1.sum()):.2%}; if the model's were: {np.mean(sim_m >= y1.sum()):.2%}")
by_lg = defaultdict(list)
for r in one:
    by_lg[r["league"]].append(r)
print("  by league (matches, overs landed, market expected, model expected): " + "; ".join(
    f"{lg} {len(v)}, {int(sum(r['y'] for r in v))}, {sum(r['mkt'] for r in v):.1f}, {sum(r['p'] for r in v):.1f}" for lg, v in sorted(by_lg.items())))

section("5c. Price moves (the only stand-in for closing-line value in the repo)")
moves = []
for key, h in history.items():
    if len({s["captured_at"] for s in h}) > 1 and h[0]["over_odds"] and h[-1]["over_odds"] and h[0]["over_odds"] != h[-1]["over_odds"]:
        mp = model_p(h[0])
        if mp:
            moves.append((h[0]["fixture"], SIDE[key[1]], key[2], h[0]["over_odds"], h[-1]["over_odds"], mp[0] * h[0]["over_odds"] - 1))
print(f"  lines captured more than once with a changed over price: {len(moves)}")
for fx, side, line, a, b, ev in moves:
    print(f"    {fx} {side} over {line:g}: {a:.2f} -> {b:.2f}; model value at the first price {ev:+.0%}")
if moves:
    agree = sum((b < a) == (ev > 0) for *_, a, b, ev in moves)
    print(f"  moved toward the model's view in {agree} of {len(moves)}: far too few to test")

# ───────────────────────────── 6. Backtest realism ─────────────────────────────
section("6a. The real bets (value_bets with a stake)")
bets = [dict(r) for r in con.execute("""SELECT vb.bet_id, vb.taken_odds o, vb.stake, vb.model_prob p, vb.result, vb.profit,
        vb.placed_at, o.over_odds, o.under_odds FROM value_bets vb JOIN odds_snapshots o USING (snapshot_id)
        WHERE vb.stake > 0 AND vb.result IN ('won', 'lost') ORDER BY vb.placed_at""")]
margin = float(np.mean([r["over_round"] for r in two])) if two else 0.07
n, wins = len(bets), sum(b["result"] == "won" for b in bets)
staked, profit = sum(b["stake"] for b in bets), sum(b["profit"] for b in bets)
ret = np.array([b["profit"] / b["stake"] for b in bets])
path = np.cumsum([b["profit"] for b in bets])
dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], path])) - np.concatenate([[0], path])))
lo, hi = boot_ci(ret, n=20000)
print(f"  {n} bets, {wins} won, staked £{staked:.2f}, profit £{profit:+.2f}, ROI {profit / staked:+.1%}")
print(f"  mean return per bet {ret.mean():+.1%}, bootstrap 95% CI {lo:+.1%} to {hi:+.1%}; average odds {np.mean([b['o'] for b in bets]):.2f}; max drawdown £{dd:.2f}")


def luck(ps: np.ndarray, odds: np.ndarray, stakes: np.ndarray, wins: int, profit: float, sims: int = 200000):
    w = rng.random((sims, len(ps))) < ps
    pr = (w * stakes * (odds - 1) - (~w) * stakes).sum(axis=1)
    return float(np.mean(w.sum(axis=1) >= wins)), float(np.mean(pr >= profit - 1e-9))


odds, stakes = np.array([b["o"] for b in bets]), np.array([b["stake"] for b in bets])
for label, ps in (("prices exactly fair (win chance = 1/odds)", 1 / odds),
                  (f"prices carry Sky Bet's measured margin ({margin:.1%} over two sides)", 1 / odds / (1 + margin)),
                  ("the model's own probabilities are right", np.array([b["p"] for b in bets]))):
    a, b_ = luck(ps, odds, stakes, wins, profit)
    print(f"  if {label}: expected wins {ps.sum():.1f}; chance of {wins}+ wins {a:.2%}; chance of this profit or more {b_:.2%}")

section("6b. The shortlist rules applied mechanically to every logged Sky Bet price")
blocks = defaultdict(list)
for r in rows:
    blocks[(r["match_id"], r["side"])].append(r)


def run_rule(name: str, pick) -> list[dict]:
    chosen = []
    for (mid_, side), rs in blocks.items():
        chosen += pick(side, rs)
    if not chosen:
        print(f"  {name}: no bets")
        return chosen
    pr = np.array([(c["odds"] - 1) if c["won"] else -1.0 for c in chosen])
    g = np.array([c["match_id"] for c in chosen])
    lo, hi = cluster_boot(pr, g)
    path = np.concatenate([[0], np.cumsum(pr)])
    dd = float(np.max(np.maximum.accumulate(path) - path))
    if len(set(g)) == len(chosen):  # one bet a match: independent enough to simulate
        fair = np.array([c["fair"] for c in chosen])
        w = rng.random((200000, len(chosen))) < fair
        sim = (w * (np.array([c["odds"] for c in chosen]) - 1) - (~w)).sum(axis=1)
        luck_txt = f"chance of this or better if Sky Bet's de-vigged prices are right {np.mean(sim >= pr.sum() - 1e-9):.1%}"
    else:
        luck_txt = "several lines a match, so not independent: read the interval, which resamples whole matches"
    print(f"  {name}: {len(chosen)} bets on {len(set(g))} matches, {int(sum(c['won'] for c in chosen))} won, "
          f"ROI {pr.mean():+.1%} (95% CI {lo:+.1%} to {hi:+.1%}), max drawdown {dd:.1f} units, "
          f"mean model edge {np.mean([c['edge'] for c in chosen]):+.1%}; {luck_txt}")
    return chosen


def as_bet(r: dict, sel: str) -> dict:
    o = r["over_odds"] if sel == "over" else r["under_odds"]
    p = r["p"] if sel == "over" else 1 - r["p"]
    fair = (r["mkt"] if sel == "over" else 1 - r["mkt"]) if "mkt" in r else (1 / o) / (1 + margin)
    won = r["y"] == (1.0 if sel == "over" else 0.0)
    return dict(match_id=r["match_id"], odds=o, won=won, fair=fair, edge=p * o - 1, p=p, sel=sel,
                fixture=r["fixture"], side=r["side"], line=r["line"], captured_at=r["captured_at"])


def rule_pick(side, rs):
    v = verdicts([dict(line=r["line"], p_over=r["p"], p_push=0.0, over=r["over_odds"], under=r["under_odds"], _r=r) for r in rs],
                 team_line=side != "total", underdog=rs[0]["underdog"])
    return [as_bet(x["_r"], "over") for x in v if x["verdict"].startswith("BET")]


ruled = run_rule("overs by the full rule set (agent/check_prices.py)", rule_pick)
run_rule("every over showing +8% or more (no cautions)", lambda side, rs: [as_bet(r, "over") for r in rs if r["over_odds"] and r["p"] * r["over_odds"] - 1 >= 0.08])
run_rule("every under showing +5% or more", lambda side, rs: [as_bet(r, "under") for r in rs if r["under_odds"] and (1 - r["p"]) * r["under_odds"] - 1 >= 0.05])
run_rule("the main over line of each match, model ignored (one per match, nearest 50%)",
         lambda side, rs: [as_bet(r, "over") for r in rs if any(r is o for o in one)])
run_rule("every over offered (what the margin alone costs)", lambda side, rs: [as_bet(r, "over") for r in rs if r["over_odds"] and 1.2 <= r["over_odds"] <= 6])
print("  Odds are the ones on screen when the price was captured, so they were available; stake limits, account")
print("  restrictions and whether a price was still there minutes later cannot be verified from the repo.")
print("  Matches were chosen for screenshots because the model liked them, so this is not a random sample.")

# ───────────────────────────── 7. Staking ─────────────────────────────
section("7. Staking: flat against Kelly, on the mechanical rule bets in time order (bank = 100 units)")
seq = sorted(ruled, key=lambda c: c["captured_at"])


def kelly(p: float, o: float) -> float:
    return max(0.0, (p * o - 1) / (o - 1))


def bank_path(stake_of) -> tuple[float, float, float]:
    bank, peak, dd, biggest = 100.0, 100.0, 0.0, 0.0
    for c in seq:
        s = min(stake_of(c) * bank, bank)
        biggest = max(biggest, s / bank if bank else 0)
        bank += s * (c["odds"] - 1) if c["won"] else -s
        peak = max(peak, bank)
        dd = max(dd, (peak - bank) / peak)
    return bank, dd, biggest


plans = (("flat 2% of the starting bank", None),
         ("full Kelly on the model's probability", lambda c: kelly(c["p"], c["odds"])),
         ("half Kelly", lambda c: 0.5 * kelly(c["p"], c["odds"])),
         ("quarter Kelly", lambda c: 0.25 * kelly(c["p"], c["odds"])),
         ("half Kelly on a 50/50 blend of model and market", lambda c: 0.5 * kelly(0.5 * c["p"] + 0.5 * c["fair"], c["odds"])))
if seq:
    for name, f in plans:
        if f is None:
            bank = 100 + 2 * sum((c["odds"] - 1) if c["won"] else -1 for c in seq)
            p_ = np.concatenate([[100], 100 + 2 * np.cumsum([(c["odds"] - 1) if c["won"] else -1 for c in seq])])
            print(f"  {name}: final bank {bank:.1f}, max drawdown {np.max((np.maximum.accumulate(p_) - p_) / np.maximum.accumulate(p_)):.1%}, largest stake 2.0%")
        else:
            bank, dd, big = bank_path(f)
            print(f"  {name}: final bank {bank:.1f}, max drawdown {dd:.1%}, largest stake {big:.1%} of bank")
    print("  expected growth per bet (%, log scale) if the truth sits between market and model:")
    print("     share of the model's edge that is real:    0%     25%     50%    100%")
    for name, f in plans:
        cells = []
        for alpha in (0.0, 0.25, 0.5, 1.0):
            g = []
            for c in seq:
                t = c["fair"] + alpha * (c["p"] - c["fair"])
                s = 0.02 if f is None else min(f(c), 0.99)
                g.append(t * math.log(1 + s * (c["odds"] - 1)) + (1 - t) * math.log(1 - s))
            cells.append(f"{100 * np.mean(g):+6.2f}")
        print(f"     {name:48} " + "  ".join(cells))
print(f"\n  Kelly fractions the model implied on the real bets: " + ", ".join(f"{kelly(b['p'], b['o']):.0%}" for b in bets))
