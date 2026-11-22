"""Post-freeze test 2b in shadow: the each-team dependence model, its price-file block, the price checker's
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
from score_projections import report, score  # noqa: E402


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


def test_checker_keeps_the_live_verdict_and_shows_2b_beside_it(monkeypatch):
    import check_prices
    monkeypatch.setattr(check_prices, "book_weights", lambda con, book: None)
    block = dict(market="each", rows=[(1.5, 2.2, None), (2.5, 9.0, None)],
                 when=datetime(2026, 11, 28, 10, tzinfo=timezone.utc))
    live = price_block(None, fake(0.0), {"match_id": 1}, block, "Sky Bet")
    linked = price_block(None, fake(0.2), {"match_id": 1}, block, "Sky Bet")
    by = lambda out: {r["line"]: r for r in out["rows"]}
    for line in (1.5, 2.5):  # the live probability and verdict don't depend on the link
        assert by(live)[line]["p_over"] == by(linked)[line]["p_over"]
        assert by(live)[line]["verdict"] == by(linked)[line]["verdict"]
    r = by(linked)[1.5]
    assert r["p_2b"] > r["p_over"] and r["value_2b"] > r["value_over"]
    # 2+ at 2.2: live p 0.507 x 2.2 = +12% clears the bar; the 2b price shows more value
    assert r["verdict"] == "BET" and r["verdict_2b"] == "BET"
    fx = {"home": "Leeds", "away": "Everton", "referee_id": None, "referee": None, "ref_matches": 0}
    text = render(fx, block, linked)
    assert "each team" in text and "team link 0.20" in text and "2b (shadow, not yet live)" in text


def test_old_projection_log_gets_the_rho_column(tmp_path):
    path = tmp_path / "p.csv"
    old = FIELDS[:FIELDS.index("rho")]
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
