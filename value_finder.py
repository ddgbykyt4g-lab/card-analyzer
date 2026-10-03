#!/usr/bin/env python3
"""
value_finder: flags +EV card-market bets on upcoming fixtures (schema: card_value_schema.sql).

Projected Card Rating (PCR)
    PCR = (home team avg match cards + away team avg match cards + referee avg match cards) / 3
    Each term is the average *match total* in that side's recent matches (home team at home,
    away team away, referee in any match), so all three share one scale. Only matches before
    kickoff are used.

From rating to probability
    PCR is an expected count, so P(Over/Under line) comes from a negative binomial with
    mean = PCR and dispersion fitted to the league's recent match totals (Poisson if the
    data isn't overdispersed). EV = p_win * odds + p_push - 1 against the offered price.

Usage
    python value_finder.py --db cards.db                    # evaluate, print, log flagged bets
    python value_finder.py --db cards.db --dry-run --json   # machine-readable, no writes
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Callable

log = logging.getLogger("value_finder")
ISO = "%Y-%m-%dT%H:%M:%SZ"
MODEL_VERSION = "pcr-v1"
MODEL_VERSIONS = {"pcr": "pcr-v1", "glm": "glm-v1"}


# ───────────────────────────── Config & types ─────────────────────────────
@dataclass(frozen=True)
class Weights:
    yellow: float
    second_yellow: float
    straight_red: float


# Used when a bookmaker/market has no row in settlement_rules
DEFAULT_WEIGHTS = {
    "cards": Weights(1, 1, 2),
    "booking_points": Weights(10, 25, 25),
}


@dataclass
class Config:
    min_ev: float = 0.05
    horizon_hours: int = 72
    max_odds_age_hours: float = 6.0
    team_lookback: int = 20
    ref_lookback: int = 30
    min_team_matches: int = 5
    min_ref_matches: int = 5
    venue_split: bool = True
    ref_fallback_league_avg: bool = False
    distribution: str = "negbin"  # or "poisson"; backtest (6 leagues): NB clearly better outside the SPL
    team_prior: float = 0.0  # pseudo-matches at the league mean blended into each team average
    ref_prior: float = 0.0   # same for the referee average
    trend_recent: int = 40   # scale by league mean of the last N league matches / last trend_base (0 = off)
    trend_base: int = 400
    trend_prior: float = 40.0  # pseudo-matches pulling that ratio toward 1
    model: str = "glm"  # "glm" (team-level regression, glm_model.py) or "pcr" (three-way average)


@dataclass
class Projection:
    rating: float
    home_avg: float
    home_n: int
    away_avg: float
    away_n: int
    ref_avg: float
    ref_n: int
    ref_source: str
    trend: float = 1.0


@dataclass
class Candidate:
    match_id: int
    kickoff_utc: str
    fixture: str
    referee: str | None
    bookmaker: str
    market: str
    line: float
    selection: str
    odds: float
    implied_prob: float
    fair_prob: float
    model_prob: float
    push_prob: float
    ev: float
    kelly: float
    rating: float
    snapshot_id: int
    flagged: bool
    components: str


# ───────────────────────────── Historical averages ─────────────────────────────
VENUE_FILTERS = {
    "home": "m.home_team_id = :id",
    "away": "m.away_team_id = :id",
    "any": ":id IN (m.home_team_id, m.away_team_id)",
    "referee": "m.referee_id = :id",
    "league": "m.season_id IN (SELECT season_id FROM seasons WHERE league_id = :id)",
}


class Stats:
    """Point-in-time match-total averages, weighted by the market's settlement rule."""

    def __init__(self, con: sqlite3.Connection, cfg: Config):
        self.con, self.cfg = con, cfg
        self._cache: dict[tuple, list[float]] = {}

    def totals(self, filt: str, entity_id: int, limit: int, w: Weights, before: str) -> list[float]:
        key = (filt, entity_id, limit, w, before)
        if key not in self._cache:
            sql = f"""
                WITH picked AS (
                    SELECT m.match_id FROM matches m
                    WHERE m.status = 'finished' AND m.kickoff_utc < :ko AND {VENUE_FILTERS[filt]}
                    ORDER BY m.kickoff_utc DESC LIMIT {int(limit)}
                )
                SELECT SUM(:wy * c.yellows + :w2 * c.second_yellows + :wr * c.straight_reds)
                FROM picked JOIN match_team_cards c USING (match_id)
                GROUP BY picked.match_id HAVING COUNT(*) = 2"""
            params = dict(id=entity_id, ko=before, wy=w.yellow, w2=w.second_yellow, wr=w.straight_red)
            self._cache[key] = [r[0] for r in self.con.execute(sql, params)]
        return self._cache[key]

    def team_avg(self, team_id: int, venue: str, w: Weights, before: str) -> tuple[float | None, int]:
        vals = self.totals(venue if self.cfg.venue_split else "any", team_id,
                           self.cfg.team_lookback, w, before)
        return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)

    def referee_avg(self, ref_id: int | None, league_id: int, w: Weights,
                    before: str) -> tuple[float | None, int, str]:
        if ref_id is not None:
            vals = self.totals("referee", ref_id, self.cfg.ref_lookback, w, before)
            if len(vals) >= self.cfg.min_ref_matches:
                return sum(vals) / len(vals), len(vals), "matches"
            snap = self.con.execute(
                """SELECT avg_yellows, avg_reds, n_matches FROM referee_stats
                   WHERE referee_id = ? AND as_of_date < ? AND (league_id = ? OR league_id IS NULL)
                   ORDER BY as_of_date DESC LIMIT 1""", (ref_id, before, league_id)).fetchone()
            if snap and snap[2] >= self.cfg.min_ref_matches:
                return snap[0] * w.yellow + snap[1] * w.straight_red, snap[2], "referee_stats"
        if self.cfg.ref_fallback_league_avg:
            mean, _, n = self.league_profile(league_id, w, before)
            return mean, n, "league_avg"
        return None, 0, "missing"

    def league_profile(self, league_id: int, w: Weights, before: str) -> tuple[float | None, float, int]:
        vals = self.totals("league", league_id, 400, w, before)
        if len(vals) < 2:
            return None, 0.0, len(vals)
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        return mean, var, len(vals)


# ───────────────────────────── Distribution ─────────────────────────────
def make_pmf(mean: float, size: float | None):
    """Negative binomial pmf with given mean and size r; Poisson when size is None."""
    if size is None:
        return lambda k: math.exp(k * math.log(mean) - mean - math.lgamma(k + 1)) if k >= 0 else 0.0
    p = size / (size + mean)
    log_p, log_q = math.log(p), math.log(1 - p)

    def pmf(k: int) -> float:
        if k < 0:
            return 0.0
        return math.exp(math.lgamma(k + size) - math.lgamma(size) - math.lgamma(k + 1)
                        + size * log_p + k * log_q)
    return pmf


def side_probs(pmf, line_units: float, selection: str) -> tuple[float, float]:
    """(p_win, p_push) for Over/Under a line expressed in count units."""
    k = math.floor(line_units + 1e-9)
    cdf_k = sum(pmf(i) for i in range(k + 1))
    if abs(line_units - round(line_units)) < 1e-9:  # whole line: push possible
        push = pmf(k)
        return (1 - cdf_k, push) if selection == "over" else (cdf_k - push, push)
    return (1 - cdf_k, 0.0) if selection == "over" else (cdf_k, 0.0)


def is_quarter_line(line: float) -> bool:
    return abs((line * 4) % 2 - 1) < 1e-9  # x.25 / x.75


# ───────────────────────────── Core evaluation ─────────────────────────────
def weights_for(con: sqlite3.Connection, bookmaker_id: int, market_id: int, unit: str) -> Weights:
    row = con.execute("""SELECT w_yellow, w_second_yellow, w_straight_red FROM settlement_rules
                         WHERE bookmaker_id = ? AND market_id = ?""", (bookmaker_id, market_id)).fetchone()
    return Weights(*row) if row else DEFAULT_WEIGHTS[unit]


def project(stats: Stats, cfg: Config, fx: sqlite3.Row, w: Weights) -> Projection | None:
    ko = fx["kickoff_utc"]
    h_avg, h_n = stats.team_avg(fx["home_team_id"], "home", w, ko)
    a_avg, a_n = stats.team_avg(fx["away_team_id"], "away", w, ko)
    r_avg, r_n, r_src = stats.referee_avg(fx["referee_id"], fx["league_id"], w, ko)
    if h_n < cfg.min_team_matches or a_n < cfg.min_team_matches or r_avg is None:
        log.debug("Skip %s v %s: home n=%d, away n=%d, ref=%s",
                  fx["home"], fx["away"], h_n, a_n, r_src)
        return None
    if cfg.team_prior or cfg.ref_prior:  # shrink thin averages toward the league mean
        l_mean, _, _ = stats.league_profile(fx["league_id"], w, ko)
        if l_mean is not None:
            def shrink(avg: float, n: int, k: float) -> float:
                return (n * avg + k * l_mean) / (n + k) if n + k else avg
            h_avg, a_avg = shrink(h_avg, h_n, cfg.team_prior), shrink(a_avg, a_n, cfg.team_prior)
            if r_src != "league_avg":
                r_avg = shrink(r_avg, r_n, cfg.ref_prior)
    trend = 1.0
    if cfg.trend_recent:  # league-wide shift (e.g. a disciplinary crackdown) the averages lag behind
        recent = stats.totals("league", fx["league_id"], cfg.trend_recent, w, ko)
        base = stats.totals("league", fx["league_id"], cfg.trend_base, w, ko)
        if recent and base and sum(base):
            n = len(recent)
            ratio = (sum(recent) / n) / (sum(base) / len(base))
            trend = 1 + (ratio - 1) * n / (n + cfg.trend_prior)
    return Projection(trend * (h_avg + a_avg + r_avg) / 3, h_avg, h_n, a_avg, a_n, r_avg, r_n, r_src, trend)


def fit_pmf(stats: Stats, cfg: Config, league_id: int, w: Weights, before: str, rating: float):
    """(pmf, NB size or None, scale) for a projected rating. Modelled in count units:
    booking points are scaled by the yellow weight (approximation)."""
    scale = w.yellow
    size = None
    if cfg.distribution == "negbin":
        l_mean, l_var, l_n = stats.league_profile(league_id, w, before)
        if l_mean and l_n >= 50:
            m_c, v_c = l_mean / scale, l_var / scale ** 2
            size = m_c ** 2 / (v_c - m_c) if v_c > m_c * 1.02 else None
    return make_pmf(rating / scale, size), size, scale


@dataclass
class Priced:
    rating: float              # projected total in market units
    pmf: Callable[[int], float]  # in count units: compare against line / scale
    scale: float
    components: str
    detail: dict = field(default_factory=dict)


class Projector:
    """Prices a fixture with the configured model. The GLM needs match_prices (migration 001);
    without it, or for a match it can't see, this falls back to the PCR average."""

    def __init__(self, con: sqlite3.Connection, cfg: Config, now: datetime):
        self.stats, self.cfg, self.glm = Stats(con, cfg), cfg, None
        self.version = MODEL_VERSIONS["pcr"]
        if cfg.model == "glm":
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='match_prices'").fetchone():
                from glm_model import GLMPredictor  # lazy: glm_model imports this module
                self.glm = GLMPredictor(con, now.strftime(ISO))
                self.version = MODEL_VERSIONS["glm"]
            else:
                log.warning("match_prices missing (migration 001); using the PCR model")

    def price(self, fx: sqlite3.Row, w: Weights) -> Priced | None:
        scale = w.yellow
        if self.glm and (g := self.glm.predict(fx["match_id"])):
            # GLM is fitted in card units (1/1/2); other settlement weights are approximated by scale
            size = g["size"]
            return Priced(g["mean"] * scale, make_pmf(g["mean"], size), scale,
                          f"GLM H {g['home']:.2f} A {g['away']:.2f}"
                          + ("" if g["has_prices"] else " (no 1X2 prices)")
                          + f" dist={'NB r=%.1f' % size if size else 'Poisson'}",
                          dict(model="glm", home_mu=round(g["home"], 3), away_mu=round(g["away"], 3),
                               has_prices=g["has_prices"],
                               distribution=f"NB r={size:.1f}" if size else "Poisson"))
        proj = project(self.stats, self.cfg, fx, w)
        if proj is None:
            return None
        pmf, size, scale = fit_pmf(self.stats, self.cfg, fx["league_id"], w, fx["kickoff_utc"], proj.rating)
        return Priced(proj.rating, pmf, scale,
                      f"H {proj.home_avg:.2f}/{proj.home_n} A {proj.away_avg:.2f}/{proj.away_n} "
                      f"R {proj.ref_avg:.2f}/{proj.ref_n} ({proj.ref_source}) "
                      + (f"trend x{proj.trend:.3f} " if proj.trend != 1 else "")
                      + f"dist={'NB r=%.1f' % size if size else 'Poisson'}",
                      dict(model="pcr", home_avg=round(proj.home_avg, 3), home_n=proj.home_n,
                           away_avg=round(proj.away_avg, 3), away_n=proj.away_n,
                           ref_avg=round(proj.ref_avg, 3), ref_n=proj.ref_n, ref_source=proj.ref_source,
                           trend=round(proj.trend, 4), distribution=f"NB r={size:.1f}" if size else "Poisson"))


def latest_quotes(con: sqlite3.Connection, match_id: int, now: datetime, max_age_h: float):
    return con.execute(
        """SELECT * FROM (
               SELECT o.snapshot_id, o.bookmaker_id, b.name AS bookmaker, o.market_id,
                      mk.code AS market, mk.unit, o.line, o.over_odds, o.under_odds,
                      ROW_NUMBER() OVER (PARTITION BY o.bookmaker_id, o.market_id, o.line
                                         ORDER BY o.captured_at DESC) AS rn
               FROM odds_snapshots o
               JOIN markets mk USING (market_id)
               JOIN bookmakers b USING (bookmaker_id)
               WHERE o.match_id = ? AND mk.scope = 'match' AND mk.period = 'FT'
                 AND o.captured_at BETWEEN ? AND ?)
           WHERE rn = 1""",
        (match_id, (now - timedelta(hours=max_age_h)).strftime(ISO), now.strftime(ISO))).fetchall()


def evaluate(con: sqlite3.Connection, cfg: Config, now: datetime,
             projector: Projector | None = None) -> list[Candidate]:
    projector = projector or Projector(con, cfg, now)
    fixtures = con.execute(
        """SELECT m.match_id, m.kickoff_utc, m.home_team_id, m.away_team_id, m.referee_id,
                  s.league_id, h.name AS home, a.name AS away, r.name AS referee
           FROM matches m
           JOIN seasons s USING (season_id)
           JOIN teams h ON h.team_id = m.home_team_id
           JOIN teams a ON a.team_id = m.away_team_id
           LEFT JOIN referees r USING (referee_id)
           WHERE m.status = 'scheduled' AND m.kickoff_utc > ? AND m.kickoff_utc <= ?
           ORDER BY m.kickoff_utc""",
        (now.strftime(ISO), (now + timedelta(hours=cfg.horizon_hours)).strftime(ISO))).fetchall()

    out: list[Candidate] = []
    for fx in fixtures:
        for q in latest_quotes(con, fx["match_id"], now, cfg.max_odds_age_hours):
            if is_quarter_line(q["line"]):
                continue  # Asian quarter lines need split-stake settlement; not modelled here
            w = weights_for(con, q["bookmaker_id"], q["market_id"], q["unit"])
            priced = projector.price(fx, w)
            if priced is None:
                break  # same inputs for every quote on this fixture
            pmf, scale = priced.pmf, priced.scale

            prices = {"over": q["over_odds"], "under": q["under_odds"]}
            overround = sum(1 / o for o in prices.values() if o)
            for sel, odds in prices.items():
                if not odds:
                    continue
                p_win, p_push = side_probs(pmf, q["line"] / scale, sel)
                ev = p_win * odds + p_push - 1
                b, p_lose = odds - 1, 1 - p_win - p_push
                implied = 1 / odds
                out.append(Candidate(
                    match_id=fx["match_id"], kickoff_utc=fx["kickoff_utc"],
                    fixture=f"{fx['home']} v {fx['away']}", referee=fx["referee"],
                    bookmaker=q["bookmaker"], market=q["market"], line=q["line"], selection=sel,
                    odds=odds, implied_prob=implied,
                    fair_prob=implied / overround if all(prices.values()) else implied,
                    model_prob=p_win, push_prob=p_push, ev=ev,
                    kelly=max(0.0, (b * p_win - p_lose) / b), rating=priced.rating,
                    snapshot_id=q["snapshot_id"], flagged=ev >= cfg.min_ev,
                    components=priced.components,
                ))
    return out


def log_bets(con: sqlite3.Connection, cands: list[Candidate], now: datetime,
             model_version: str = MODEL_VERSION) -> int:
    """Insert flagged bets as paper bets (stake 0). Skips if an identical pending bet exists."""
    logged = 0
    for c in (c for c in cands if c.flagged):
        dup = con.execute(
            """SELECT 1 FROM value_bets vb JOIN odds_snapshots o USING (snapshot_id)
               WHERE vb.result = 'pending' AND vb.model_version = ? AND vb.selection = ?
                 AND o.match_id = ? AND o.line = ?
                 AND o.bookmaker_id = (SELECT bookmaker_id FROM odds_snapshots WHERE snapshot_id = ?)
                 AND o.market_id    = (SELECT market_id    FROM odds_snapshots WHERE snapshot_id = ?)""",
            (model_version, c.selection, c.match_id, c.line, c.snapshot_id, c.snapshot_id)).fetchone()
        if dup:
            continue
        con.execute(
            """INSERT INTO value_bets (snapshot_id, selection, model_version, expected_mean, model_prob,
                   model_push_prob, taken_odds, kelly_fraction, placed_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (c.snapshot_id, c.selection, model_version, c.rating, c.model_prob, c.push_prob,
             c.odds, c.kelly, now.strftime(ISO), c.components))
        logged += 1
    con.commit()
    return logged


# ───────────────────────────── CLI ─────────────────────────────
def print_table(cands: list[Candidate], show_all: bool) -> None:
    rows = sorted((c for c in cands if show_all or c.flagged), key=lambda c: -c.ev)
    if not rows:
        print("No bets at or above the EV threshold.")
        return
    for c in rows:
        mark = "FLAG" if c.flagged else "    "
        print(f"{mark} {c.kickoff_utc[:16].replace('T', ' ')}  {c.fixture}  [ref: {c.referee or '-'}]\n"
              f"     {c.bookmaker} {c.market} {c.selection.title()} {c.line} @ {c.odds:.2f}  "
              f"Proj {c.rating:.2f}  model {c.model_prob:.1%}  fair {c.fair_prob:.1%}  "
              f"EV {c.ev:+.1%}  Kelly {c.kelly:.1%}\n     {c.components}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Flag +EV card bets using the Projected Card Rating")
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--min-ev", type=float, default=Config.min_ev, help="0.05 = 5%%")
    ap.add_argument("--horizon-hours", type=int, default=Config.horizon_hours)
    ap.add_argument("--max-odds-age-hours", type=float, default=Config.max_odds_age_hours)
    ap.add_argument("--team-lookback", type=int, default=Config.team_lookback)
    ap.add_argument("--ref-lookback", type=int, default=Config.ref_lookback)
    ap.add_argument("--min-team-matches", type=int, default=Config.min_team_matches)
    ap.add_argument("--min-ref-matches", type=int, default=Config.min_ref_matches)
    ap.add_argument("--no-venue-split", dest="venue_split", action="store_false")
    ap.add_argument("--ref-fallback-league-avg", action="store_true",
                    help="use league average when the referee is unknown or has too few matches")
    ap.add_argument("--model", choices=["glm", "pcr"], default=Config.model,
                    help="glm = team-level regression (default), pcr = three-way average")
    ap.add_argument("--distribution", choices=["negbin", "poisson"], default=Config.distribution,
                    help="PCR model only; the GLM fits its own per-league dispersion")
    ap.add_argument("--team-prior", type=float, default=Config.team_prior,
                    help="pseudo-matches at the league mean added to team averages")
    ap.add_argument("--trend-recent", type=int, default=Config.trend_recent,
                    help="scale projections by the league's last N matches vs its last --trend-base (0 = off)")
    ap.add_argument("--trend-base", type=int, default=Config.trend_base)
    ap.add_argument("--trend-prior", type=float, default=Config.trend_prior)
    ap.add_argument("--ref-prior", type=float, default=Config.ref_prior,
                    help="pseudo-matches at the league mean added to the referee average")
    ap.add_argument("--dry-run", action="store_true", help="don't write to value_bets")
    ap.add_argument("--json", action="store_true", help="print candidates as JSON")
    ap.add_argument("--show-all", action="store_true", help="include unflagged quotes")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)

    cfg = Config(**{f.name: getattr(args, f.name) for f in fields(Config)})
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    now = datetime.now(timezone.utc)

    projector = Projector(con, cfg, now)
    cands = evaluate(con, cfg, now, projector)
    logged = 0 if args.dry_run else log_bets(con, cands, now, projector.version)
    flagged = sum(c.flagged for c in cands)

    if args.json:
        shown = cands if args.show_all else [c for c in cands if c.flagged]
        print(json.dumps({"evaluated": len(cands), "flagged": flagged, "logged": logged,
                          "candidates": [asdict(c) for c in shown]}, indent=2))
    else:
        print_table(cands, args.show_all)
        print(f"\n{len(cands)} quotes evaluated, {flagged} flagged, {logged} logged to value_bets")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
