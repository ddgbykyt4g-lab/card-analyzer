"""Post-freeze test 6a in shadow: the fixed derby list, the derby-adjusted line in the price checker (capped
at the +-8-point research limit, the live verdict unchanged) and the derby confirmation read."""
import csv
import math
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from conftest import ROOT, finish

sys.path.insert(0, str(ROOT / "agent"))
from check_prices import ADJ_CAP, price_block, render  # noqa: E402
from derbies import DERBIES, is_derby  # noqa: E402
from log_projections import FIELDS  # noqa: E402
from score_projections import report, score  # noqa: E402
from value_finder import make_pmf  # noqa: E402


def test_derby_list_is_either_venue_and_league_specific():
    assert is_derby("SPL", "Rangers", "Celtic") and is_derby("SPL", "Celtic", "Rangers")
    assert is_derby("LALIGA", "Betis", "Sevilla")
    assert not is_derby("EPL", "Celtic", "Rangers")          # right pair, wrong league
    assert not is_derby("EPL", "Arsenal", "Leeds")
    assert sum(len(v) for v in DERBIES.values()) == 46       # fixed 22 Nov 2026: a change is a new test


def priced(mean):
    return SimpleNamespace(rating=mean, pmf=make_pmf(mean, None), scale=1.0,
                           detail=dict(model="glm", home_mu=mean / 2, away_mu=mean / 2))


class Proj:
    def __init__(self, live, derby):
        self.live, self.derby = live, derby

    def price(self, fx, w):
        return priced(self.live)

    def price_derby(self, fx, w):
        return None if self.derby is None else dict(mean=self.derby, home_mu=self.derby / 2, away_mu=self.derby / 2,
                                                    size=None, pmf=make_pmf(self.derby, None))


class Con:
    def execute(self, *a):
        return SimpleNamespace(fetchone=lambda: None)


def block():
    return dict(market="total", rows=[(4.5, 2.1, 1.7), (5.5, 3.6, 1.28)],
                when=datetime(2026, 11, 28, 10, tzinfo=timezone.utc))


def test_derby_adjustment_is_capped_and_leaves_the_live_verdict(monkeypatch):
    import check_prices
    monkeypatch.setattr(check_prices, "book_weights", lambda con, book: None)
    plain = price_block(Con(), Proj(4.0, None), {"match_id": 1}, block(), "Sky Bet")
    derby = price_block(Con(), Proj(4.0, 6.0), {"match_id": 1}, block(), "Sky Bet")
    assert "derby" not in plain
    assert [r["verdict"] for r in plain["rows"]] == [r["verdict"] for r in derby["rows"]]   # live unchanged
    for live, adj in zip(derby["rows"], derby["derby"]["rows"]):
        assert adj["p_6a"] - live["p_over"] > ADJ_CAP                       # 6.0 v 4.0 cards: more than 8 points
        assert math.isclose(adj["p_over"], live["p_over"] + ADJ_CAP)       # capped
    fx = {"home": "Sevilla", "away": "Betis", "referee_id": None, "referee": None, "ref_matches": 0}
    text = render(fx, block(), derby)
    assert "derby (6a, shadow" in text and "with the derby adjustment" in text


def test_derby_confirmation_read(db, tmp_path):
    log = tmp_path / "p.csv"
    pmf = lambda mu: " ".join(f"{make_pmf(mu, None)(k):.6f}" for k in range(21))
    with open(log, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerow(dict(logged_at="2026-10-10T08:00:00Z", match_id=1, external_id="espn:123", league="EPL",
                        kickoff_utc="2026-10-10T11:30:00Z", home="Arsenal", away="Leeds", model_version="glm-v4.1",
                        book="Sky Bet", w_yellow=1.0, w_second_yellow=1.0, w_straight_red=1.0, mu_home=2.0,
                        mu_away=2.0, mu_total=4.0, pmf=pmf(4.0), mu_home_6a=3.0, mu_away_6a=3.0, pmf_6a=pmf(6.0)))
    finish(db, home=(3, 0, 0), away=(4, 0, 0))   # 7 cards: the derby projection was closer
    db.close()
    acc = score(str(tmp_path / "t.db"), str(log))
    a = acc["derby 6a"]
    assert a["n"] == 1 and a["ll_6a"] > a["ll_cur"] and a["mu_6a"] == 6.0
    text = report(acc)
    assert "1 derbies logged" in text and "not enough derbies yet" in text
