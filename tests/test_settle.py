from datetime import datetime, timezone

import pytest

from conftest import add_bet, finish
from settle import outcome, settle, void_postponed


@pytest.mark.parametrize("sel, line, value, result", [
    ("over", 4.5, 5, "won"), ("over", 4.5, 4, "lost"), ("under", 4.5, 4, "won"),
    ("over", 4.0, 4, "push"), ("under", 4.0, 4, "push"), ("under", 3.0, 5, "lost")])
def test_outcome(sel, line, value, result):
    assert outcome(sel, line, value) == result


def result(con, bet_id):
    return con.execute("SELECT result, settled_value, profit FROM value_bets WHERE bet_id = ?", (bet_id,)).fetchone()


def test_sky_bet_counts_two_yellows_as_two_and_straight_red_as_one(db):
    total = add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.0)
    # home: 2 yellows + a two-yellow dismissal (stored as yellow + second yellow) = 4; away: 1 straight red = 1
    finish(db, home=(3, 1, 0), away=(0, 0, 1))
    settle(db)
    assert result(db, total) == ("won", 5.0, 10.0)


def test_team_and_each_team_markets(db):
    home = add_bet(db, "HOME_CARDS", "over", 2.5, 3.5)
    away = add_bet(db, "AWAY_CARDS", "under", 1.5, 1.8)
    each = add_bet(db, "EACH_TEAM_CARDS", "over", 1.5, 2.875)
    finish(db, home=(3, 0, 0), away=(1, 0, 0))
    settle(db)
    assert result(db, home) == ("won", 3.0, 25.0)
    assert result(db, away) == ("won", 1.0, pytest.approx(8.0))
    assert result(db, each)[:2] == ("lost", 1.0)  # each team over: settles on the lower team
    assert result(db, each)[2] == -10.0


def test_push_returns_stake(db):
    bet = add_bet(db, "TOTAL_CARDS", "over", 4.0, 2.0)
    finish(db, home=(2, 0, 0), away=(2, 0, 0))
    settle(db)
    assert result(db, bet) == ("push", 4.0, 0.0)


def test_pending_until_cards_loaded(db):
    bet = add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.0)
    db.execute("UPDATE matches SET status = 'finished' WHERE match_id = 1")
    settle(db)
    assert result(db, bet)[0] == "pending"


def test_abandoned_is_void(db):
    bet = add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.0)
    db.execute("UPDATE matches SET status = 'abandoned' WHERE match_id = 1")
    settle(db)
    assert result(db, bet) == ("void", None, 0.0)


def at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("new_kickoff, void", [
    ("2026-10-13T19:00:00Z", False),   # Saturday -> Tuesday evening: within 3 calendar days, stands
    ("2026-10-14T19:00:00Z", True)])   # Saturday -> Wednesday: void
def test_rescheduled_match(db, new_kickoff, void):
    bet = add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.0)
    db.execute("UPDATE matches SET kickoff_utc = ? WHERE match_id = 1", (new_kickoff,))
    void_postponed(db, at("2026-10-12T12:00:00"))
    assert (result(db, bet)[0] == "void") is void


@pytest.mark.parametrize("today, void", [("2026-10-13T20:00:00", False), ("2026-10-14T09:00:00", True)])
def test_postponed_without_new_date(db, today, void):
    bet = add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.0)
    db.execute("UPDATE matches SET status = 'postponed' WHERE match_id = 1")
    void_postponed(db, at(today))
    assert (result(db, bet)[0] == "void") is void


def test_paper_bet_gets_result_but_no_profit(db):
    bet = add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.0, stake=0)
    finish(db, home=(3, 0, 0), away=(3, 0, 0))
    settle(db)
    assert result(db, bet) == ("won", 6.0, 0.0)


def test_report_shows_clv_when_known(db, capsys):
    from settle import report
    a = add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.30)
    b = add_bet(db, "TOTAL_CARDS", "under", 5.5, 1.80)
    db.execute("UPDATE value_bets SET clv = 0.10 WHERE bet_id = ?", (a,))
    db.execute("UPDATE value_bets SET clv = -0.04 WHERE bet_id = ?", (b,))
    report(db)
    assert "vs the Betfair Exchange close: 2 bets, mean +3.0%, beat the close on 1" in capsys.readouterr().out
