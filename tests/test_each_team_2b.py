"""Post-freeze test 2b, live from glm-v4.2: the each-team dependence model, its price-file block, the price checker's
2b column (the live verdict unchanged), the projection log's rho column and the confirmation read."""
import csv
import math
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from conftest import ROOT, finish

sys.path.insert(0, str(ROOT / "agent"))
from check_prices import price_block, render  # noqa: E402
from glm_model import each_team_over, joint_prob  # noqa: E402
from log_prices import parse_file  # noqa: E402
from log_projections import FIELDS, upgrade_header  # noqa: E402
from price_sheet import poisson_tail  # noqa: E402
from score_projections import CONFIRM_N, FINAL_N, each_verdict, report, score  # noqa: E402


def test_no_link_is_the_independent_price():
    for line in (0.5, 1.5, 2.5):
        indep = poisson_tail(2.1, line, "over")[0] * poisson_tail(1.7, line, "over")[0]
        assert math.isclose(each_team_over(2.1, 1.7, 0.0, line), indep, rel_tol=1e-9)


def test_link_keeps_each_team_poisson_and_raises_each_team_lines():
    mh, ma, rho = 2.1, 1.7, 0.2
    for h in range(6):  # home marginal is still Poisson(mu_h)
        marg = sum(joint_prob(mh, ma, rho, h, a) for a in range(30))
        assert math.isclose(marg, math.exp(-mh) * mh ** h / math.factorial(h), rel_tol=1e-9)
    assert each_team_over(mh, ma, rho, 1.5) > each_team_over(mh, ma, 0.0, 1.5)
    both = sum(joint_prob(mh, ma, rho, h, a) for h in range(2, 30) for a in range(2, 30))
    assert math.isclose(each_team_over(mh, ma, rho, 1.5), both, rel_tol=1e-6)


def test_each_block_in_a_price_file():
    b = parse_file("@ 2026-11-28 10:00 | Leeds v Everton | each\n1+ 1/4\n2+ 6/4\n3+ 6/1\n")[0]
    assert b["market"] == "each" and b["rows"] == [(0.5, 1.25, None), (1.5, 2.5, None), (2.5, 7.0, None)]


def fake(rho):
    priced = SimpleNamespace(rating=4.2, pmf=None, scale=1.0,
                             detail=dict(model="glm", home_mu=2.6, away_mu=2.4, rho=rho))
    return SimpleNamespace(price=lambda fx, w: priced)


def test_checker_prices_each_team_with_the_link_and_shows_the_old_price(monkeypatch):
    import check_prices
    monkeypatch.setattr(check_prices, "book_weights", lambda con, book: None)
    block = dict(market="each", rows=[(1.5, 2.0, None), (2.5, 9.0, None)],
                 when=datetime(2026, 11, 28, 10, tzinfo=timezone.utc))
    unlinked = price_block(None, fake(0.0), {"match_id": 1}, block, "Sky Bet")
    linked = price_block(None, fake(0.2), {"match_id": 1}, block, "Sky Bet")
    by = lambda out: {r["line"]: r for r in out["rows"]}
    r0, r = by(unlinked)[1.5], by(linked)[1.5]
    assert math.isclose(r0["p_over"], r0["p_indep"])                 # no link: the old price
    assert r["p_over"] > r["p_indep"] == r0["p_indep"]                # the link is what's priced now
    # 2+ at 2.0: independent 0.507 x 2.0 = +1% (no); linked 0.530 x 2.0 = +6% (still no); the verdict uses linked
    assert r["value_over"] > r["value_indep"]
    assert math.isclose(r["value_over"], r["p_over"] * 2.0 - 1)
    fx = {"home": "Leeds", "away": "Everton", "referee_id": None, "referee": None, "ref_matches": 0}
    text = render(fx, block, linked)
    assert "each team" in text and "team link 0.20" in text and "glm-v4.2" in text and "indep" in text


def test_logged_each_team_bet_uses_the_link(db, tmp_path):
    import sqlite3
    from collect_cards import DB
    from glm_model import each_team_under
    from price_sheet import log_bet
    from value_finder import Priced

    class Proj:
        version = "test"

        def price(self, fx, w):
            pmf = lambda k: math.exp(k * math.log(4) - 4 - math.lgamma(k + 1))
            return Priced(4.0, pmf, 1.0, "test", dict(model="glm", home_mu=2.0, away_mu=2.0, rho=0.2))

    db.close()
    con = DB(str(tmp_path / "t.db"), None)
    con.con.row_factory = sqlite3.Row
    now = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
    over = log_bet(con, Proj(), "Arsenal v Leeds: each team 2+ @ 15/8 Sky Bet £10", now, 7, 0.05,
                   espn_cards=tmp_path / "none.csv")
    assert math.isclose(over["model_prob"], each_team_over(2.0, 2.0, 0.2, 1.5))
    assert over["model_prob"] > poisson_tail(2.0, 1.5, "over")[0] ** 2
    under = log_bet(con, Proj(), "Arsenal v Leeds: each team under 1.5 @ 3.0 Sky Bet £10", now, 7, 0.05,
                    espn_cards=tmp_path / "none.csv")
    assert math.isclose(under["model_prob"], each_team_under(2.0, 2.0, 0.2, 1.5))


def test_old_projection_log_gets_the_rho_column(tmp_path):
    path = tmp_path / "p.csv"
    old = FIELDS[:-1]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, old)
        w.writeheader()
        w.writerow({k: "x" for k in old})
    upgrade_header(str(path))
    rows = list(csv.DictReader(open(path)))
    assert list(rows[0]) == FIELDS and rows[0]["rho"] == "" and rows[0]["pmf"] == "x"
    upgrade_header(str(path))  # already current: unchanged
    assert list(csv.DictReader(open(path)))[0] == rows[0]


def test_confirmation_read_scores_each_team_on_rows_with_the_link(db, tmp_path):
    log = tmp_path / "p.csv"
    with open(log, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerow(dict(logged_at="2026-10-10T08:00:00Z", match_id=1, external_id="espn:123", league="EPL",
                        kickoff_utc="2026-10-10T11:30:00Z", home="Arsenal", away="Leeds", model_version="glm-v4.1",
                        book="Sky Bet", w_yellow=1.0, w_second_yellow=1.0, w_straight_red=1.0, mu_home=2.0,
                        mu_away=2.0, mu_total=4.0, pmf=" ".join(["0.05"] * 20), rho=0.2))
    finish(db, home=(2, 0, 0), away=(3, 0, 0))   # each team 2+ landed
    db.close()
    acc = score(str(tmp_path / "t.db"), str(log))
    a = acc["each 2b"]
    assert a["n"] == 1 and a["hit2"] == 1 and a["hit3"] == 0
    assert a["2b2"] > a["cur2"] and a["ll_2b"] > a["ll_cur"]   # the link rates 2 and 3 more likely together
    text = report(acc)
    assert "confirmation read, 1 matches" in text and "not enough matches yet" in text
    assert "each 2b" not in text.split("Each team")[0]       # not listed as a model version


def acc(n, mean_gain, sd, hit2=0.40, cur2=0.36, b2=0.38):
    """Totals as score() keeps them, for n matches with the given per-match gain mean and spread."""
    return dict(n=n, ll_cur=-2.0 * n, ll_2b=(-2.0 + mean_gain) * n, gain_sq=n * (sd ** 2 + mean_gain ** 2),
                hit2=hit2 * n, cur2=cur2 * n, **{"2b2": b2 * n})


def test_confirmation_needs_the_interval_above_zero_not_just_a_positive_gain():
    assert each_verdict(acc(CONFIRM_N - 1, 0.02, 0.1))[0] == "not enough matches yet"
    assert each_verdict(acc(CONFIRM_N, 0.0127, 0.127))[0] == "PASS"          # the backtest's gain and spread
    v, gain, lower = each_verdict(acc(CONFIRM_N, 0.005, 0.127))             # positive, but within noise
    assert gain > 0 and lower < 0 and v.startswith("not yet")                # one more look, at FINAL_N
    assert each_verdict(acc(FINAL_N, 0.005, 0.127))[0] == "FAIL"             # still within noise: done
    assert each_verdict(acc(CONFIRM_N, -0.002, 0.127))[0] == "FAIL"          # no gain: no second look
    assert each_verdict(acc(CONFIRM_N, 0.0127, 0.127, b2=0.33))[0] == "FAIL"  # calibration gap wider
