"""Serie A caution (23 Nov 2026): an over there must still clear the +8% bar with the projection 0.3 cards lower."""
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from conftest import ROOT

sys.path.insert(0, str(ROOT / "agent"))
from check_prices import price_block  # noqa: E402
from value_finder import make_pmf  # noqa: E402


class Proj:
    """Monza v Fiorentina, 23 Nov 2026: 3.95 cards projected, Monza 2.12, Fiorentina 1.83."""
    def price(self, fx, w):
        return SimpleNamespace(rating=3.95, pmf=make_pmf(3.95, None), scale=1.0,
                               detail=dict(model="glm", home_mu=2.12, away_mu=1.83, rho=0.05, distribution="Poisson"))


class Con:
    def execute(self, *a):
        return SimpleNamespace(fetchone=lambda: (0.26, 0.25, 0.49))   # Monza 26% to win: not a heavy underdog


def block(market, rows):
    return dict(market=market, rows=rows, when=datetime(2026, 11, 23, 16, 22, tzinfo=timezone.utc))


def verdicts(league, market, rows, monkeypatch):
    import check_prices
    monkeypatch.setattr(check_prices, "book_weights", lambda con, book: None)
    out = price_block(Con(), Proj(), {"match_id": 1, "league": league}, block(market, rows), "Sky Bet")
    return {r["line"]: r["verdict"] for r in out["rows"]}


def test_monza_2plus_at_4_5_is_a_pass_in_serie_a_but_a_bet_elsewhere(monkeypatch):
    assert verdicts("SERIEA", "home", [(1.5, 1.8, 20 / 23 + 1)], monkeypatch)[1.5].startswith("pass: Serie A caution")
    assert verdicts("EPL", "home", [(1.5, 1.8, 20 / 23 + 1)], monkeypatch)[1.5] == "BET"


def test_total_over_3_5_at_evens_is_a_pass_in_serie_a(monkeypatch):
    v = verdicts("SERIEA", "total", [(3.5, 2.0, 1.7), (4.5, 3.5, 1.25)], monkeypatch)
    assert v[3.5].startswith("pass: Serie A caution")
    assert verdicts("LALIGA", "total", [(3.5, 2.0, 1.7)], monkeypatch)[3.5] == "BET"


def test_a_big_enough_edge_still_bets_in_serie_a(monkeypatch):
    # Monza 2+ at 2.2: +40% on the model, still well over +8% with the projection shaded
    assert verdicts("SERIEA", "home", [(1.5, 2.2, None)], monkeypatch)[1.5] == "BET"
