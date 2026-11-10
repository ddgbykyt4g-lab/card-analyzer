"""Shared helpers for the review scripts. Read-only: nothing here writes to cards.db or the repo's code.

walk() repeats glm_model.walk_forward (refit each Monday-based week on everything before it) but also keeps
each team's own cards, so team lines and home/away dependence can be checked. Card count = Sky Bet's
(yellow 1, second yellow 1, straight red 1), the units the bets are priced in.
"""
import math
import os
import pickle
import sqlite3
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "agent"))
from glm_model import CardGLM, Data, GLMConfig  # noqa: E402
from value_finder import Weights, make_pmf  # noqa: E402

OUT = os.path.join(ROOT, "review", "out")
SKY = Weights(1, 1, 1)
TUNED = ["2022-23", "2023-24"]                 # seasons the half-life and penalties were tuned on
UNTUNED = ["2024-25", "2025-26", "2026-27"]   # reported in the repo as out-of-sample


def connect(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def walk(con: sqlite3.Connection, seasons: list[str], cfg: GLMConfig = GLMConfig(), only_weeks=None) -> list[dict]:
    d = Data(con, weights=SKY, fouls=cfg.fouls, fouls_prior=cfg.fouls_prior, half_life=cfg.half_life_days)
    model = CardGLM(d, cfg)
    test = np.where(d.finished & np.isin(d.season, seasons))[0]
    weeks = (d.day[test] + 3) // 7
    out = []
    for wk in sorted(set(weeks.tolist())):
        if only_weeks is not None and wk not in only_weeks:
            continue
        idx = test[weeks == wk]
        start = wk * 7 - 3
        model.fit(np.where(d.finished & (d.day < start))[0], start)
        mh, ma, sizes = model.predict(idx)
        for i, h, a, size in zip(idx, mh, ma, sizes):
            out.append(dict(match_id=int(d.match_id[i]), week=int(wk), day=int(d.day[i]), league=d.league[i],
                            season=d.season[i], mu_h=float(h), mu_a=float(a), size=size,
                            y_h=float(d.y[2 * i]), y_a=float(d.y[2 * i + 1]), has_prices=bool(d.has_prices[i]),
                            has_ref=bool(d.ref[2 * i] >= 0)))
    return out


def cached_walk(db: str, seasons: list[str], cfg: GLMConfig = GLMConfig(), tag: str = "base") -> list[dict]:
    key = os.path.join(OUT, f"wf_{tag}_{os.path.getsize(db)}_{int(os.path.getmtime(db))}_{'-'.join(s[:4] for s in seasons)}.pkl")
    if os.path.exists(key):
        return pickle.load(open(key, "rb"))
    rows = walk(connect(db), seasons, cfg)
    pickle.dump(rows, open(key, "wb"))
    return rows


def total_pmf(r: dict):
    return make_pmf(r["mu_h"] + r["mu_a"], r["size"])


def p_over_total(r: dict, line: float) -> float:
    pmf = total_pmf(r)
    return 1 - sum(pmf(k) for k in range(math.floor(line) + 1))


def pois_over(mu: float, line: float) -> float:
    return 1 - sum(math.exp(-mu) * mu ** k / math.factorial(k) for k in range(math.floor(line) + 1))


def logloss_total(rows: list[dict]) -> float:
    return float(np.mean([math.log(max(total_pmf(r)(int(r["y_h"] + r["y_a"])), 1e-300)) for r in rows]))


def boot_ci(values, n=4000, seed=1, stat=np.mean) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    v = np.asarray(values, dtype=float)
    draws = [stat(v[rng.integers(0, len(v), len(v))]) for _ in range(n)]
    return float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))
