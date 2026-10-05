import math

import pytest

from price_sheet import odds_for, poisson_tail
from value_finder import is_quarter_line, make_pmf, side_probs


def pois(mu):
    return lambda k: math.exp(k * math.log(mu) - mu - math.lgamma(k + 1))


def test_half_line_has_no_push_and_sides_sum_to_one():
    pmf = pois(4.0)
    over, push = side_probs(pmf, 4.5, "over")
    under, _ = side_probs(pmf, 4.5, "under")
    assert push == 0
    assert over + under == pytest.approx(1)
    assert over == pytest.approx(1 - sum(pmf(k) for k in range(5)))


def test_whole_line_push_is_exact_count():
    pmf = pois(4.0)
    over, push = side_probs(pmf, 4.0, "over")
    under, push_u = side_probs(pmf, 4.0, "under")
    assert push == push_u == pytest.approx(pmf(4))
    assert over + under + push == pytest.approx(1)


def test_fair_and_minimum_odds_account_for_push():
    # EV = p_win * odds + p_push - 1, so fair odds = (1 - push) / p and +5% odds = (1.05 - push) / p
    assert odds_for(0.4, 0.2, 0) == pytest.approx(2.0)
    assert odds_for(0.4, 0.0, 0.05) == pytest.approx(2.625)
    assert odds_for(0.0, 0.0, 0) is None


def test_negative_binomial_sums_to_one_and_keeps_mean():
    pmf = make_pmf(4.2, 15.0)
    probs = [pmf(k) for k in range(80)]
    assert sum(probs) == pytest.approx(1, abs=1e-9)
    assert sum(k * p for k, p in enumerate(probs)) == pytest.approx(4.2, rel=1e-6)


def test_poisson_tail_matches_side_probs():
    assert poisson_tail(1.8, 1.5, "over")[0] == pytest.approx(1 - math.exp(-1.8) * (1 + 1.8))


@pytest.mark.parametrize("line, quarter", [(4.25, True), (4.75, True), (4.5, False), (4.0, False)])
def test_quarter_lines(line, quarter):
    assert is_quarter_line(line) is quarter
