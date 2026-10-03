#!/usr/bin/env python3
"""
glm_model: team-level card regression (alternative to the PCR average).

For each team in each match, cards (settlement weights: yellow 1, second yellow 1, red 2) are
modelled as Poisson with
    log mu = league + home*is_home + disc[team] + prov[opponent] + ref[referee]
             + b_gap*(p_own - p_opp) + b_abs*|p_own - p_opp| + b_draw*p_draw
where p_* are the de-margined pre-match 1X2 market probabilities (match_prices, migration 001).
disc = how many cards a team picks up, prov = how many it draws from opponents. Team and
referee effects carry L2 penalties (shrinkage toward the league average); older matches are
down-weighted with an exponential half-life. The match total is home mu + away mu, priced with
a negative binomial whose dispersion is fitted per league on the training residuals.

Usage
    python glm_model.py --db cards.db --backtest                     # walk-forward vs PCR
    python glm_model.py --db cards.db --backtest --half-life 365 --team-l2 20
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from dataclasses import dataclass, replace

import numpy as np

from value_finder import DEFAULT_WEIGHTS, Config, make_pmf, side_probs

W = DEFAULT_WEIGHTS["cards"]
N_FEAT = 3  # gap, |gap|, p_draw


@dataclass(frozen=True)
class GLMConfig:
    half_life_days: float = 120.0  # tuned on 2022-24 (walk-forward), see backtest
    team_l2: float = 60.0
    ref_l2: float = 100.0
    fixed_l2: float = 1e-3
    iters: int = 25


# ───────────────────────────── Data ─────────────────────────────
class Data:
    """Two rows per match (home team, away team) with index columns for the design matrix."""

    def __init__(self, con: sqlite3.Connection, include_scheduled: bool = False):
        status = "('finished','scheduled')" if include_scheduled else "('finished')"
        rows = con.execute(f"""
            SELECT m.match_id, m.kickoff_utc, m.status, s.league_id, s.label, l.code,
                   m.home_team_id, m.away_team_id, m.referee_id,
                   p.p_home, p.p_draw, p.p_away,
                   ch.yellows, ch.second_yellows, ch.straight_reds,
                   ca.yellows, ca.second_yellows, ca.straight_reds
            FROM matches m
            JOIN seasons s USING (season_id)
            JOIN leagues l USING (league_id)
            LEFT JOIN match_prices p USING (match_id)
            LEFT JOIN match_team_cards ch ON ch.match_id = m.match_id AND ch.team_id = m.home_team_id
            LEFT JOIN match_team_cards ca ON ca.match_id = m.match_id AND ca.team_id = m.away_team_id
            WHERE m.status IN {status}
            ORDER BY m.kickoff_utc""").fetchall()
        rows = [r for r in rows if r[2] == "scheduled" or r[12] is not None and r[15] is not None]
        self.match_id = np.array([r[0] for r in rows])
        self.kickoff = [r[1] for r in rows]
        self.day = np.array([np.datetime64(r[1][:10], "D").astype(int) for r in rows])
        self.season = [r[4] for r in rows]
        self.league = [r[5] for r in rows]
        self.finished = np.array([r[2] == "finished" for r in rows])

        leagues = sorted(set(r[3] for r in rows))
        teams = sorted({t for r in rows for t in (r[6], r[7])})
        refs = sorted({r[8] for r in rows if r[8] is not None})
        self.l_ix = {x: i for i, x in enumerate(leagues)}
        self.t_ix = {x: i for i, x in enumerate(teams)}
        self.r_ix = {x: i for i, x in enumerate(refs)}
        # parameter layout: [leagues | home | features | disc teams | prov teams | refs]
        self.o_home = len(leagues)
        self.o_feat = self.o_home + 1
        self.o_disc = self.o_feat + N_FEAT
        self.o_prov = self.o_disc + len(teams)
        self.o_ref = self.o_prov + len(teams)
        self.n_par = self.o_ref + len(refs)

        n = len(rows)
        has_p = np.array([r[9] is not None for r in rows])
        ph = np.array([r[9] if r[9] is not None else 0.44 for r in rows])
        pd = np.array([r[10] if r[10] is not None else 0.25 for r in rows])
        pa = np.array([r[11] if r[11] is not None else 0.31 for r in rows])
        self.has_prices = has_p
        # team-row arrays, index 2*i = home, 2*i+1 = away
        self.lg = np.repeat([self.l_ix[r[3]] for r in rows], 2)
        self.is_home = np.tile([1.0, 0.0], n)
        own = np.array([[self.t_ix[r[6]], self.t_ix[r[7]]] for r in rows]).ravel()
        opp = np.array([[self.t_ix[r[7]], self.t_ix[r[6]]] for r in rows]).ravel()
        self.own, self.opp = own, opp
        self.ref = np.repeat([self.r_ix.get(r[8], -1) for r in rows], 2)
        gap = np.column_stack([ph - pa, pa - ph]).ravel()
        self.feat = np.column_stack([gap, np.abs(gap), np.repeat(pd, 2)])

        def cards(y, y2, r):
            return W.yellow * (y or 0) + W.second_yellow * (y2 or 0) + W.straight_red * (r or 0)
        self.y = np.array([[cards(*r[12:15]), cards(*r[15:18])] for r in rows], dtype=float).ravel()

    def design(self, team_rows: np.ndarray) -> np.ndarray:
        X = np.zeros((len(team_rows), self.n_par))
        k = np.arange(len(team_rows))
        X[k, self.lg[team_rows]] = 1
        X[:, self.o_home] = self.is_home[team_rows]
        X[:, self.o_feat:self.o_feat + N_FEAT] = self.feat[team_rows]
        X[k, self.o_disc + self.own[team_rows]] = 1
        X[k, self.o_prov + self.opp[team_rows]] = 1
        rr = self.ref[team_rows]
        has = rr >= 0
        X[k[has], self.o_ref + rr[has]] = 1
        return X


# ───────────────────────────── Model ─────────────────────────────
class CardGLM:
    def __init__(self, data: Data, cfg: GLMConfig):
        self.d, self.cfg = data, cfg
        pen = np.full(data.n_par, cfg.fixed_l2)
        pen[data.o_disc:data.o_ref] = cfg.team_l2
        pen[data.o_ref:] = cfg.ref_l2
        self.pen = pen
        self.beta = np.zeros(data.n_par)
        self.size: dict[int, float | None] = {}

    def fit(self, train_matches: np.ndarray, asof_day: int) -> "CardGLM":
        d, cfg = self.d, self.cfg
        tr = np.column_stack([2 * train_matches, 2 * train_matches + 1]).ravel()
        X, y = d.design(tr), d.y[tr]
        age = asof_day - d.day[tr // 2]
        w = 0.5 ** (age / cfg.half_life_days)
        beta = self.beta.copy()
        if not beta[:d.o_home].any():  # cold start: league intercepts at log mean
            for lg, i in d.l_ix.items():
                m = d.lg[tr] == i
                beta[i] = math.log(max(y[m].mean(), 0.1)) if m.any() else 0.0
        for _ in range(cfg.iters):
            mu = np.exp(np.clip(X @ beta, -10, 5))
            g = X.T @ (w * (y - mu)) - self.pen * beta
            H = (X * (w * mu)[:, None]).T @ X + np.diag(self.pen)
            step = np.linalg.solve(H, g)
            beta += step
            if np.abs(step).max() < 1e-6:
                break
        self.beta = beta
        # per-league NB dispersion of match totals, method of moments on weighted residuals
        mu = np.exp(np.clip(X @ beta, -10, 5))
        mu_t, y_t, w_t = mu[0::2] + mu[1::2], y[0::2] + y[1::2], w[0::2]
        lg_t = d.lg[tr][0::2]
        self.size = {}
        for i in set(lg_t.tolist()):
            m = lg_t == i
            excess = np.sum(w_t[m] * ((y_t[m] - mu_t[m]) ** 2 - mu_t[m]))
            self.size[i] = float(np.sum(w_t[m] * mu_t[m] ** 2) / excess) if excess > 0 else None
        return self

    def predict(self, matches: np.ndarray) -> tuple[np.ndarray, np.ndarray, list]:
        """(home mu, away mu, NB size per match) for match indices."""
        rows = np.column_stack([2 * matches, 2 * matches + 1]).ravel()
        mu = np.exp(np.clip(self.d.design(rows) @ self.beta, -10, 5))
        sizes = [self.size.get(int(self.d.lg[2 * m])) for m in matches]
        return mu[0::2], mu[1::2], sizes


class GLMPredictor:
    """Fits once on every finished match before `asof`, then predicts any loaded match."""

    def __init__(self, con: sqlite3.Connection, asof: str, cfg: GLMConfig = GLMConfig()):
        self.d = Data(con, include_scheduled=True)
        day = int(np.datetime64(asof[:10], "D").astype(int))
        train = np.where(self.d.finished & (self.d.day < day))[0]
        self.model = CardGLM(self.d, cfg).fit(train, day)
        self.ix = {int(m): i for i, m in enumerate(self.d.match_id)}

    def predict(self, match_id: int) -> dict | None:
        i = self.ix.get(int(match_id))
        if i is None:
            return None
        h, a, sizes = self.model.predict(np.array([i]))
        return dict(mean=float(h[0] + a[0]), home=float(h[0]), away=float(a[0]), size=sizes[0],
                    has_prices=bool(self.d.has_prices[i]))


# ───────────────────────────── Walk-forward backtest ─────────────────────────────
def walk_forward(con: sqlite3.Connection, seasons: list[str], cfg: GLMConfig, data: Data | None = None) -> dict:
    """Refit weekly on everything before each week; returns {match_id: prediction dict}."""
    d = data or Data(con)
    model = CardGLM(d, cfg)
    test = np.where(d.finished & np.isin(d.season, seasons))[0]
    weeks = (d.day[test] + 3) // 7  # Monday-based week buckets
    out = {}
    for wk in sorted(set(weeks.tolist())):
        idx = test[weeks == wk]
        start = wk * 7 - 3
        train = np.where(d.finished & (d.day < start))[0]
        model.fit(train, start)
        mh, ma, sizes = model.predict(idx)
        for i, h, a, size in zip(idx, mh, ma, sizes):
            out[int(d.match_id[i])] = dict(mu=float(h + a), mu_home=float(h), mu_away=float(a), size=size,
                                           actual=float(d.y[2 * i] + d.y[2 * i + 1]),
                                           league=d.league[i], season=d.season[i])
    return out


def score(pred: dict, line: float = 4.5) -> dict:
    pmf = make_pmf(pred["mu"], pred["size"])
    k = int(round(pred["actual"]))
    p_over = side_probs(pmf, line, "over")[0]
    return dict(ll=math.log(max(pmf(k), 1e-300)), brier=(p_over - (pred["actual"] > line)) ** 2,
                sq=(pred["mu"] - pred["actual"]) ** 2)


def compare(con: sqlite3.Connection, seasons: list[str], cfg: GLMConfig) -> None:
    from backtest import run as pcr_run
    glm = walk_forward(con, seasons, cfg)
    pcr_rows, _ = pcr_run(con, Config(ref_fallback_league_avg=True), seasons)
    pcr = {r["match_id"]: r for r in pcr_rows}
    common = [m for m in glm if m in pcr]
    print(f"GLM vs PCR on {len(common)} matches ({', '.join(seasons)}); "
          f"half-life {cfg.half_life_days:g}d, team L2 {cfg.team_l2:g}, ref L2 {cfg.ref_l2:g}\n")
    print(f"{'':16} {'n':>5}  {'LL GLM':>8} {'LL PCR':>8}  {'Brier4.5 GLM':>12} {'PCR':>7}  {'RMSE GLM':>8} {'PCR':>6}")

    def row(name: str, ids: list[int]) -> None:
        g = [score(glm[m]) for m in ids]
        p = [dict(ll=pcr[m]["ll_nb"], brier=(pcr[m]["p_over_4.5"] - (pcr[m]["actual"] > 4.5)) ** 2,
                  sq=(pcr[m]["pcr"] - pcr[m]["actual"]) ** 2) for m in ids]
        mean = lambda xs, k: float(np.mean([x[k] for x in xs]))
        print(f"{name:16} {len(ids):5d}  {mean(g, 'll'):8.4f} {mean(p, 'll'):8.4f}  "
              f"{mean(g, 'brier'):12.4f} {mean(p, 'brier'):7.4f}  "
              f"{math.sqrt(mean(g, 'sq')):8.3f} {math.sqrt(mean(p, 'sq')):6.3f}")

    row("All", common)
    for lg in sorted({glm[m]["league"] for m in common}):
        row(f"  {lg}", [m for m in common if glm[m]["league"] == lg])
    for s in seasons:
        row(f"  {s}", [m for m in common if glm[m]["season"] == s])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Team-level card GLM")
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--seasons", nargs="+", default=["2022-23", "2023-24", "2024-25", "2025-26", "2026-27"])
    ap.add_argument("--half-life", type=float, default=GLMConfig.half_life_days)
    ap.add_argument("--team-l2", type=float, default=GLMConfig.team_l2)
    ap.add_argument("--ref-l2", type=float, default=GLMConfig.ref_l2)
    args = ap.parse_args(argv)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='match_prices'").fetchone():
        raise SystemExit("match_prices table missing: apply migrations/001_match_prices.sql and re-run the collector")
    cfg = GLMConfig(half_life_days=args.half_life, team_l2=args.team_l2, ref_l2=args.ref_l2)
    if args.backtest:
        compare(con, args.seasons, cfg)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
