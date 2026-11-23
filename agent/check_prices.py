#!/usr/bin/env python3
"""Checks logged bookmaker card prices against the model: value on each line, the minimum odds worth
taking, and a verdict that applies the shortlist rules of agent/SATURDAY.md the same way every time.

Reads the price file agent/log_prices.py loads (data/prices/<YYYY-MM>.txt) and, for each match that
hasn't kicked off, prices the latest block of each market. It only reads: nothing is logged or placed.

Rules (the overs bar and the cautions in SATURDAY.md step 5):
  overs need +8% value, unders +5%;
  an over the model gives under 40% is a tail bet: pass (the model is least reliable there);
  on a team line of 3+ cards, an edge above +20% is a warning, not a bet: take the shorter line or skip;
  a heavy underdog (under 20% to win) gets 2+ at most;
  only the shortest line that clears the bar is the bet: the longer ones are the same view with more variance;
  unders are shown but not recommended while the model projects too few cards (agent/POST_FREEZE.md).

Each-team blocks ("each": both teams 2+, ...) are priced with the teams' card link (post-freeze test 2b,
glm_model.each_team_over; live from glm-v4.2), and beside that, for comparison, as the old independent price.

Experiment A (review/EXPERIMENT_A.md) adds a second verdict beside that one, without changing it. The model's
probability is blended half-and-half (on the logit scale) with the bookmaker's own de-vigged probability, and
the blend rule backs the shortest over whose blended value is +2% or more. Both verdicts are printed; which to
follow with real money is the user's choice.
Usage: python agent/check_prices.py --db cards.db data/prices/2026-11.txt [--book "Sky Bet"] [--all]
"""
import argparse
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collect_cards import DB  # noqa: E402
from price_sheet import MIN_REF_MATCHES, book_weights, find_match, fixtures, poisson_tail  # noqa: E402
from value_finder import ISO, Config, Projector, side_probs  # noqa: E402
from glm_model import each_team_over  # noqa: E402

from log_prices import parse_file  # noqa: E402

UK = ZoneInfo("Europe/London")
OVER_EV, UNDER_EV = 0.08, 0.05
TAIL_P = 0.40          # an over the model rates below this is a tail bet
LONG_TEAM_LINE = 2.5   # "3 or more" and longer
BIG_EDGE = 0.20
UNDERDOG_P = 0.20
# Experiment A, fixed in advance (review/EXPERIMENT_A.md): don't tune these on the results they are judged by
BLEND_W = 0.5       # weight on the model, logit scale; the rest is the bookmaker's de-vigged probability
BLEND_EV = 0.02     # blended value needed: break-even if a third of the model's disagreement is real
OVERROUND = 1.089   # Sky Bet's two-sided card lines (220 lines to Nov 2026); assumed when only the over is quoted


def market_prob(over: float | None, under: float | None) -> tuple[float | None, bool]:
    """(the bookmaker's over probability with its margin removed, whether the margin had to be assumed).
    Proportional de-vig: 1/over divided by 1/over + 1/under. With no under price the sum is taken as OVERROUND."""
    if not over:
        return None, False
    if under:
        return (1 / over) / (1 / over + 1 / under), False
    return min((1 / over) / OVERROUND, 0.999), True


def blend(p_model: float, p_market: float, w: float = BLEND_W) -> float:
    logit = lambda p: math.log(min(max(p, 1e-6), 1 - 1e-6) / (1 - min(max(p, 1e-6), 1 - 1e-6)))
    return 1 / (1 + math.exp(-(w * logit(p_model) + (1 - w) * logit(p_market))))


def verdicts(rows: list[dict], team_line: bool = False, underdog: bool = False) -> list[dict]:
    """rows: dicts of line, p_over, p_push, over (decimal odds or None), under (or None), any order.
    Adds value_over, value_under, min_over and verdict to each, and for experiment A market, blend,
    value_blend and blend_verdict; returns them sorted by line."""
    out, bet_found, blend_found = [], False, False
    for r in sorted(rows, key=lambda r: r["line"]):
        p, push = r["p_over"], r["p_push"]
        mkt, assumed = market_prob(r.get("over"), r.get("under")) if push == 0 else (None, False)
        mix = blend(p, mkt) if mkt is not None else None
        r = dict(r, min_over=(1 + OVER_EV - push) / p if p > 1e-9 else None,
                 value_over=p * r["over"] + push - 1 if r.get("over") else None,
                 value_under=(1 - p - push) * r["under"] + push - 1 if r.get("under") else None,
                 market=mkt, margin_assumed=assumed, blend=mix,
                 value_blend=mix * r["over"] - 1 if mix is not None else None)
        vb = r["value_blend"]
        if vb is None:
            blend_verdict = "-"
        elif vb < BLEND_EV:
            blend_verdict = "no"
        elif p < TAIL_P:
            blend_verdict = "pass: tail"
        elif team_line and underdog and r["line"] >= LONG_TEAM_LINE:
            blend_verdict = "pass: underdog"
        elif blend_found:
            blend_verdict = "longer line"
        else:
            blend_verdict, blend_found = "BET", True
        r["blend_verdict"] = blend_verdict
        v = r["value_over"]
        if v is None:
            verdict = "no over price"
        elif v < OVER_EV:
            verdict = "no"
        elif p < TAIL_P:
            verdict = f"pass: tail bet (model under {TAIL_P:.0%})"
        elif team_line and underdog and r["line"] >= LONG_TEAM_LINE:
            verdict = "pass: heavy underdog, 2+ only"
        elif team_line and r["line"] >= LONG_TEAM_LINE and v > BIG_EDGE:
            verdict = f"warning: edge above {BIG_EDGE:.0%} on a long team line, take the shorter line or skip"
        elif bet_found:
            verdict = "longer line: prefer the shorter one"
        else:
            verdict, bet_found = "BET", True
        if r["value_under"] is not None and r["value_under"] >= UNDER_EV:
            verdict += f"; under shows {r['value_under']:+.0%}, not backed while the model runs low"
        out.append(dict(r, verdict=verdict))
    return out


def price_block(con: sqlite3.Connection, projector: Projector, fx: sqlite3.Row, block: dict, book: str) -> dict | None:
    """Model probabilities for a price block's lines, plus what the verdicts need to know about the match."""
    priced = projector.price(fx, book_weights(con, book))
    if priced is None:
        return None
    side = block["market"]
    rows = []
    for line, over, under in block["rows"]:
        if side == "total":
            p, push = side_probs(priced.pmf, line / priced.scale, "over")
        elif side == "each" and priced.detail.get("model") == "glm":
            if line != math.floor(line) + 0.5:
                continue  # each-team lines are "N or more": half lines only
            mu_h, mu_a = priced.detail["home_mu"], priced.detail["away_mu"]
            indep = poisson_tail(mu_h, line, "over")[0] * poisson_tail(mu_a, line, "over")[0]
            p = each_team_over(mu_h, mu_a, priced.detail.get("rho", 0.0), line)
            rows.append(dict(line=line, p_over=p, p_push=0.0, over=over, under=under, p_indep=indep))
            continue
        elif priced.detail.get("model") == "glm":
            p, push = poisson_tail(priced.detail[f"{side}_mu"], line, "over")
        else:
            return None  # team lines need the GLM's per-team projection
        rows.append(dict(line=line, p_over=p, p_push=push, over=over, under=under))
    if side == "each":
        live = verdicts(rows, team_line=True)
        for r in live:
            r["value_indep"] = r["p_indep"] * r["over"] - 1 if r.get("over") else None
        return dict(projected=priced.rating, p_win=None, rho=priced.detail.get("rho", 0.0), rows=live)
    win = con.execute("SELECT p_home, p_away FROM match_prices WHERE match_id = ?", (fx["match_id"],)).fetchone()
    p_win = None if win is None or side == "total" else win[0] if side == "home" else win[1]
    mu = priced.rating if side == "total" else priced.detail[f"{side}_mu"]
    return dict(projected=mu, p_win=p_win,
                rows=verdicts(rows, team_line=side != "total", underdog=p_win is not None and p_win < UNDERDOG_P))


def render_each(head: str, priced: dict) -> str:
    out = [head + f" · team link {priced['rho']:.2f}",
           f"  {'line':>5} {'over':>6} {'model':>6} {'value':>6} {'needs':>6} | {'indep':>5} {'value':>6}  verdict (current rule)"]
    for r in priced["rows"]:
        over = f"{r['over']:.2f}" if r["over"] else "-"
        value = f"{r['value_over']:+.0%}" if r["value_over"] is not None else "-"
        needs = f"{r['min_over']:.2f}" if r["min_over"] else "-"
        vi = f"{r['value_indep']:+.0%}" if r["value_indep"] is not None else "-"
        out.append(f"  {math.floor(r['line']) + 1:>4}+ {over:>6} {r['p_over']:>6.0%} {value:>6} {needs:>6} | "
                   f"{r['p_indep']:>5.0%} {vi:>6}  {r['verdict']}")
    pick = next((f"each team {math.floor(r['line']) + 1}+ at {r['over']:.2f}" for r in priced["rows"]
                 if r["verdict"].startswith("BET")), "no bet")
    out.append(f"  => current rule: {pick}   (model: teams linked, glm-v4.2; 'indep' = the old independent price)")
    return "\n".join(out)


def render(fx: sqlite3.Row, block: dict, priced: dict) -> str:
    team = {"total": "match total", "home": f"{fx['home']} cards", "away": f"{fx['away']} cards",
            "each": "each team"}[block["market"]]
    ref = "no referee yet (league average used)" if fx["referee_id"] is None else \
        f"{fx['referee']} ({fx['ref_matches']} matches{', thin record' if fx['ref_matches'] < MIN_REF_MATCHES else ''})"
    head = (f"{fx['home']} v {fx['away']} · {team} · prices at {block['when'].astimezone(UK):%a %H:%M} · "
            f"projected {priced['projected']:.2f} · {ref}")
    if priced["p_win"] is not None and priced["p_win"] < UNDERDOG_P:
        head += f" · heavy underdog ({priced['p_win']:.0%} to win)"
    if block["market"] == "each":
        return render_each(head, priced)
    out = [head, f"  {'line':>5} {'over':>6} {'model':>6} {'value':>6} {'needs':>6} | {'Sky':>5} {'blend':>6} {'value':>6}  verdict (current rule) / blend rule"]
    for r in priced["rows"]:
        over = f"{r['over']:.2f}" if r["over"] else "-"
        value = f"{r['value_over']:+.0%}" if r["value_over"] is not None else "-"
        needs = f"{r['min_over']:.2f}" if r["min_over"] else "-"
        sky = "-" if r["market"] is None else f"{r['market']:.0%}{'*' if r['margin_assumed'] else ''}"
        mix = "-" if r["blend"] is None else f"{r['blend']:.0%}"
        vb = "-" if r["value_blend"] is None else f"{r['value_blend']:+.0%}"
        out.append(f"  {r['line']:>5g} {over:>6} {r['p_over']:>6.0%} {value:>6} {needs:>6} | {sky:>5} {mix:>6} {vb:>6}  "
                   f"{r['verdict']} / {r['blend_verdict']}")
    pick = lambda key: next((f"over {r['line']:g} at {r['over']:.2f}" for r in priced["rows"] if r[key].startswith("BET")), "no bet")
    out.append(f"  => current rule: {pick('verdict')}   |   blend rule (experiment A): {pick('blend_verdict')}")
    if any(r["margin_assumed"] for r in priced["rows"]):
        out.append(f"  * no under price: Sky Bet's margin assumed at {OVERROUND - 1:.1%}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--book", default="Sky Bet")
    ap.add_argument("--days", type=float, default=7, help="how far ahead to look for the matches")
    ap.add_argument("--all", action="store_true", help="every block of each market, not just the latest")
    args = ap.parse_args()
    db = DB(args.db, None)
    db.con.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    upcoming = {fx["match_id"]: fx for fx in fixtures(db.con, now, args.days)}
    projector = Projector(db.con, Config(ref_fallback_league_avg=True), now)
    latest: dict[tuple, dict] = {}
    for block in parse_file(open(args.file).read()):
        try:
            fx = find_match(db, dict(home=block["home"], away=block["away"]), block["when"], args.days, block["when"])
        except SystemExit:
            continue  # a match the database doesn't have
        if fx["match_id"] in upcoming and fx["kickoff_utc"] > block["when"].strftime(ISO):
            key = (fx["match_id"], block["market"]) + ((block["when"],) if args.all else ())
            if key not in latest or block["when"] >= latest[key]["when"]:
                latest[key] = dict(block, fx=upcoming[fx["match_id"]])
    if not latest:
        print("No logged prices for matches that haven't kicked off.")
        return 0
    for block in sorted(latest.values(), key=lambda b: (b["fx"]["kickoff_utc"], b["fx"]["match_id"], b["market"], b["when"])):
        priced = price_block(db.con, projector, block["fx"], block, args.book)
        print(render(block["fx"], block, priced) if priced else
              f"{block['home']} v {block['away']}: the model can't price this market yet")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
