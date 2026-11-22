#!/usr/bin/env python3
"""Scores the pre-kickoff projection log (data/projections.csv) on finished matches: out-of-sample by
construction, since every row was written before its match kicked off.

Per match it uses the latest projection logged before the actual kickoff, and counts cards with the weights
stored in that row (Sky Bet: yellow 1, second yellow 1, straight red 1). Reports log-loss, Brier over lines
2.5-6.5, RMSE and over-rate calibration, per model version, next to the walk-forward backtest figures,
then projected against actual cards by league and by projected level (where the model runs high or low),
and, for rows logged with the league's team link (rho, from 22 Nov 2026), the each-team confirmation read of
post-freeze test 2b.
Usage: python agent/score_projections.py [cards.db] [data/projections.csv]
"""
import csv
import math
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from glm_model import each_team_over, joint_prob  # noqa: E402

LINES = (2.5, 3.5, 4.5, 5.5, 6.5)
# glm-v4.1 walk-forward backtest, untuned seasons 2024-27, all leagues, Sky Bet card count, scored on the
# corrected card data (two-yellow dismissals counted once). glm-v4 scored on the same outcomes: -2.115 and 2.062
BACKTEST = {"log-loss": -2.111, "rmse": 2.054}
LEVELS = (3.5, 4.0, 4.5, 5.0)  # edges of the projected-cards bands
BANDS = [f"under {LEVELS[0]:g}"] + [f"{a:g}-{b:g}" for a, b in zip(LEVELS, LEVELS[1:])] + [f"{LEVELS[-1]:g}+"]


def resolve_match(r: dict, kickoff: dict[int, str], by_ext: dict[str, int],
                  teams: dict[int, tuple[str, str]] | None) -> int | None:
    """The match a log row was written for. Its match_id alone can't be trusted: the weekday workflow logs
    from a cards.db it then discards, so a fixture it inserted had an id that cards.db may since have
    given to a different match. ESPN event id first, then the logged id if its teams agree, then the
    same teams on the logged kickoff day."""
    ext = r["external_id"].split(":", 1)[-1] if r["external_id"] else ""
    if ext in by_ext:
        return by_ext[ext]
    mid = int(r["match_id"])
    if teams is None or teams.get(mid) == (r["home"], r["away"]):
        return mid
    return next((m for m, pair in teams.items()
                 if pair == (r["home"], r["away"]) and kickoff.get(m, "")[:10] == r["kickoff_utc"][:10]), None)


def latest_before_kickoff(path: str, kickoff: dict[int, str], by_ext: dict[str, int],
                          teams: dict[int, tuple[str, str]] | None = None) -> dict[int, dict]:
    best: dict[int, dict] = {}
    for r in csv.DictReader(open(path)):
        mid = resolve_match(r, kickoff, by_ext, teams)
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
    teams = {m: (h, a) for m, h, a in con.execute(
        """SELECT m.match_id, h.name, a.name FROM matches m
           JOIN teams h ON h.team_id = m.home_team_id JOIN teams a ON a.team_id = m.away_team_id""")}
    cards = {m: (y, sy, sr) for m, y, sy, sr in con.execute(
        """SELECT m.match_id, SUM(c.yellows), SUM(c.second_yellows), SUM(c.straight_reds)
           FROM matches m JOIN match_team_cards c USING (match_id)
           WHERE m.status = 'finished' GROUP BY m.match_id""")}
    sides = {m: (h, a) for m, h, a in con.execute(
        """SELECT m.match_id,
                  (SELECT yellows + second_yellows + straight_reds FROM match_team_cards c
                   WHERE c.match_id = m.match_id AND c.team_id = m.home_team_id),
                  (SELECT yellows + second_yellows + straight_reds FROM match_team_cards c
                   WHERE c.match_id = m.match_id AND c.team_id = m.away_team_id)
           FROM matches m WHERE m.status = 'finished'""") if h is not None and a is not None}
    con.close()
    acc: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for mid, r in latest_before_kickoff(path, kickoff, by_ext, teams).items():
        if mid not in cards:
            continue
        y, sy, sr = cards[mid]
        actual = (y * float(r["w_yellow"]) + sy * float(r["w_second_yellow"])
                  + sr * float(r["w_straight_red"])) / float(r["w_yellow"])  # count units, as the pmf
        pmf = [float(x) for x in r["pmf"].split()]
        k = int(round(actual))
        p_k = pmf[k] if k < len(pmf) else max(1 - sum(pmf), 1e-6)
        mu = float(r["mu_total"]) / float(r["w_yellow"])
        band = BANDS[sum(mu >= e for e in LEVELS)]
        for key in (f"league {r['league']}", f"level {band}"):
            acc[key]["n"] += 1
            acc[key]["mu"] += mu
            acc[key]["y"] += actual
        if r.get("rho") not in (None, "") and r.get("mu_home") and mid in sides:
            each_team(acc["each 2b"], r, *sides[mid])
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


EACH_LINES = (1, 2, 3)  # each team N or more


def each_team(a: dict, r: dict, h: int, aw: int) -> None:
    """Post-freeze test 2b's confirmation read: each-team lines, live (independent) against the shadow
    dependence model, on matches logged with the league's team link. Sky Bet units (all cards count 1)."""
    mh, ma, rho = float(r["mu_home"]), float(r["mu_away"]), float(r["rho"])
    cur = math.log(max(joint_prob(mh, ma, 0.0, h, aw), 1e-12))
    new = math.log(max(joint_prob(mh, ma, rho, h, aw), 1e-12))
    a["n"] += 1
    a["ll_cur"] += cur
    a["ll_2b"] += new
    a["gain_sq"] += (new - cur) ** 2
    for k in EACH_LINES:
        a[f"cur{k}"] += each_team_over(mh, ma, 0.0, k - 0.5)
        a[f"2b{k}"] += each_team_over(mh, ma, rho, k - 0.5)
        a[f"hit{k}"] += h >= k and aw >= k


def each_verdict(a: dict) -> tuple[str, float, float]:
    """(verdict, mean joint log-loss gain, lower end of its 95% interval). Pass mark fixed on 22 Nov 2026, before
    any match was logged with the link: CONFIRM_N+ matches, the interval above 0, and the each team 2+ gap to
    actual narrower than the live pricing's. 'Gain above 0' alone would pass about half the time with no effect.
    At CONFIRM_N a positive gain still inside the noise gets one more look, at FINAL_N (about 60% power at 500 for
    the backtest's gain, about 90% at 1000; two looks put a false pass at about 8% instead of 5%)."""
    n = a["n"]
    gain = (a["ll_2b"] - a["ll_cur"]) / n
    var = max(a["gain_sq"] / n - gain ** 2, 0.0) * n / (n - 1) if n > 1 else float("inf")
    lower = gain - 1.96 * math.sqrt(var / n)
    if n < CONFIRM_N:
        return "not enough matches yet", gain, lower
    narrower = abs(a["hit2"] - a["2b2"]) < abs(a["hit2"] - a["cur2"])
    if lower > 0 and narrower:
        return "PASS", gain, lower
    if n < FINAL_N and gain > 0 and narrower:  # promising but within noise: one more look, fixed in advance
        return f"not yet: positive but within noise, final read at {FINAL_N}", gain, lower
    return "FAIL", gain, lower


def report_each(a: dict) -> list[str]:
    n = a["n"]
    verdict, gain, lower = each_verdict(a)
    return [f"Each team, live v 2b shadow (confirmation read, {n:.0f} matches logged with the team link):",
            f"  joint log-loss: live {a['ll_cur'] / n:+.4f}, 2b {a['ll_2b'] / n:+.4f}, gain {gain:+.4f} "
            f"(95% interval lower end {lower:+.4f})",
            "  each team N+, projected live / 2b / actual: "
            + "  ".join(f"{k}+ {a[f'cur{k}'] / n:.0%}/{a[f'2b{k}'] / n:.0%}/{a[f'hit{k}'] / n:.0%}" for k in EACH_LINES),
            f"  pass mark (agent/POST_FREEZE.md): {CONFIRM_N}+ matches (final look {FINAL_N}), gain's 95% interval "
            f"above 0 and the 2+ gap to actual narrower: {verdict}"]


CONFIRM_N = 500  # about 10 weeks of fixtures; at 300 the interval is about as wide as the expected gain
FINAL_N = 1000   # the one further look, about the end of the season


def report(acc: dict[str, dict]) -> str:
    if not acc:
        return "No scored matches yet: the log needs finished matches that were projected before kickoff."
    out = []
    for key in sorted((k for k in acc if not k.startswith(("league ", "level ", "each "))), key=lambda k: (k == "all", k)):
        a, n = acc[key], acc[key]["n"]
        out += [f"{key}: {n:.0f} matches",
                f"  log-loss {a['ll'] / n:+.3f} (backtest {BACKTEST['log-loss']:+.3f})   "
                f"RMSE {math.sqrt(a['se'] / n):.3f} (backtest {BACKTEST['rmse']:.3f})   "
                f"Brier {a['brier'] / n:.4f}",
                f"  mean cards: projected {a['mu'] / n:.2f}, actual {a['y'] / n:.2f}",
                "  over rate, projected vs actual: "
                + "  ".join(f"o{L:g} {a[f'p{L}'] / n:.0%}/{a[f'o{L}'] / n:.0%}" for L in LINES)]
    for title, prefix in (("By league", "league "), ("By projected cards", "level ")):
        out.append(f"{title} (matches, projected, actual, gap):")
        for key in sorted((k for k in acc if k.startswith(prefix)),
                          key=lambda k: BANDS.index(k[6:]) if prefix == "level " else k):
            a, n = acc[key], acc[key]["n"]
            out.append(f"  {key[len(prefix):]:22} {n:4.0f}  {a['mu'] / n:.2f}  {a['y'] / n:.2f}  {(a['y'] - a['mu']) / n:+.2f}")
    if acc.get("each 2b", {}).get("n"):
        out += report_each(acc["each 2b"])
    out.append("Under a few hundred matches these figures are noisy; compare with the backtest, not with zero.")
    return "\n".join(out)


if __name__ == "__main__":
    print(report(score(sys.argv[1] if len(sys.argv) > 1 else "cards.db",
                       sys.argv[2] if len(sys.argv) > 2 else "data/projections.csv")))
