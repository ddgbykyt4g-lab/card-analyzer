import pytest

from price_sheet import parse_bet, parse_odds, parse_prob


@pytest.mark.parametrize("text, odds", [("2.20", 2.20), ("5/2", 3.5), ("8/11", 1 + 8 / 11), ("20/23", 1 + 20 / 23)])
def test_parse_odds(text, odds):
    assert parse_odds(text) == pytest.approx(odds)


@pytest.mark.parametrize("text, p", [("0.45", 0.45), ("45", 0.45), ("45%", 0.45), ("", None), ("-", None)])
def test_parse_prob(text, p):
    assert parse_prob(text) == (pytest.approx(p) if p is not None else None)


def test_match_total():
    b = parse_bet("Brighton v Man Utd over 4.5 @ 2.20 bet365 £10")
    assert (b["home"], b["away"], b["kind"], b["selection"], b["line"]) == ("Brighton", "Man Utd", "total", "over", 4.5)
    assert (b["odds"], b["bookmaker"], b["stake"]) == (pytest.approx(2.2), "bet365", 10.0)


def test_team_market_fractional_odds():
    b = parse_bet("Aberdeen v Rangers: Rangers over 2.5 @ 5/2 Sky Bet £10")
    assert (b["kind"], b["team"], b["line"], b["odds"]) == ("team", "Rangers", 2.5, pytest.approx(3.5))


def test_each_team_plus_notation():
    b = parse_bet("St Mirren v Motherwell: each team 2+ @ 15/8 Sky Bet £10")
    assert (b["kind"], b["selection"], b["line"]) == ("each", "over", 1.5)


def test_under_and_default_stake():
    b = parse_bet("Inter v Parma under 3.5 @ 1.80 Sky Bet")
    assert (b["selection"], b["line"], b["stake"]) == ("under", 3.5, 1.0)


def test_pence_stake():
    assert parse_bet("Chelsea v Hull over 3.5 @ 23/20 Sky Bet £12.98")["stake"] == pytest.approx(12.98)


def test_shop_bookmaker():
    b = parse_bet("Everton v Newcastle over 4.5 @ 2.10 Coral shop £10")
    assert (b["bookmaker"], b["stake"]) == ("Coral shop", 10.0)


def test_research_pipe_line():
    b = parse_bet("Arsenal v Leeds | Leeds | over 1.5 @ 2.10 | Sky Bet £10 | 0.62 | 0.66 | Leeds booked a lot")
    assert (b["kind"], b["team"], b["line"], b["stake"]) == ("team", "Leeds", 1.5, 10.0)
    assert b["adjusted_p"] == pytest.approx(0.66)
    assert b["reason"] == "Leeds booked a lot"


def test_research_pipe_line_match_total():
    b = parse_bet("Arsenal v Leeds | total | over 3.5 @ 2.15 | Sky Bet £10 | 0.45 | 0.48 | strict ref")
    assert (b["kind"], b["line"]) == ("total", 3.5)


@pytest.mark.parametrize("bad", ["TEST from Claude - ignore this", "Arsenal v Leeds over 4.5", "Arsenal v Leeds @ 2.0"])
def test_rejects_unparseable(bad):
    with pytest.raises(SystemExit):
        parse_bet(bad)


def test_rejects_zero_stake():
    with pytest.raises(SystemExit):
        parse_bet("Arsenal v Leeds over 4.5 @ 2.0 Sky Bet £0")


@pytest.mark.parametrize("text, away, team", [
    ("Genoa v Fiorentina Fiorentina over 2.5 @ 2.38 Sky Bet £10", "Fiorentina", "Fiorentina"),  # bet #7 as typed
    ("Genoa v Fiorentina Genoa over 1.5 @ 4/6 Sky Bet £10", "Fiorentina", "Genoa"),
    ("Man United v Tottenham over 3.5 @ 7/10 Sky Bet £10", "Tottenham", None),
])
def test_team_named_without_colon_is_a_team_bet(text, away, team):
    bet = parse_bet(text)
    assert (bet["away"], bet["team"], bet["kind"]) == (away, team, "team" if team else "total")
