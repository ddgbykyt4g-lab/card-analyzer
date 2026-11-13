"""The audit of 11 Nov 2026: past-bet settlement keeps the cross-check, each-team prices aren't valued as match
totals, a bet logged in the same minute as a screenshot, a price file with one bad block, experiment A's
latest price, a name that fits two teams, and the bet queue."""
import json

import pytest
import math
import sys
from datetime import datetime, timezone

from conftest import ROOT, add_bet, finish

sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "review"))

from collect_cards import DB, TeamResolver
from price_sheet import log_bet
from value_finder import Priced, latest_quotes

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


class FakeProjector:
    """Poisson 4 cards in total, 2 a team: the logger only needs a price, not a fitted model."""
    version = "test"

    def price(self, fx, w):
        pmf = lambda k: math.exp(k * math.log(4) - 4 - math.lgamma(k + 1))
        return Priced(4.0, pmf, 1.0, "test", dict(model="glm", home_mu=2.0, away_mu=2.0))


def espn_file(path, home, away, eid="900"):
    path.write_text("espn_id,league,kickoff_utc,home,away,home_yellows,home_reds,home_staff,away_yellows,away_reds,"
                    f"away_staff,fetched_at\n{eid},EPL,2026-10-10T11:30:00Z,Arsenal,Leeds,{home},0,0,{away},0,0,x\n")
    return path


# 1. logging a past bet settles pending bets with ESPN's counts too
def test_logging_a_past_bet_keeps_a_disputed_bet_pending(db, tmp_path):
    db.execute("UPDATE matches SET external_id = 'espn-fx:900' WHERE match_id = 1")
    disputed = add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.0)
    finish(db, home=(2, 0, 0), away=(1, 0, 0))           # football-data: 3, a loss
    espn = espn_file(tmp_path / "espn.csv", 3, 2)         # ESPN: 5, a win
    con = DB(str(tmp_path / "t.db"), None)
    import sqlite3
    con.con.row_factory = sqlite3.Row
    r = log_bet(con, FakeProjector(), "Arsenal v Leeds: Leeds over 0.5 @ 1.5 Sky Bet £1", NOW, 7, 0.05,
                placed=datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc), espn_cards=espn)
    assert r["result"] == "won"                            # the new bet settles: both sources agree
    assert con.con.execute("SELECT result FROM value_bets WHERE bet_id = ?", (disputed,)).fetchone()[0] == "pending"


# 2. each-team prices are left out of the match-total quotes value_finder prices
def test_each_team_price_is_not_a_match_total_quote(db):
    add_bet(db, "EACH_TEAM_CARDS", "over", 1.5, 2.875, stake=0)
    add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.0, stake=0)
    import sqlite3
    db.row_factory = sqlite3.Row
    quotes = latest_quotes(db, 1, datetime(2026, 10, 9, 11, 0, tzinfo=timezone.utc), 6)
    assert [(q["market"], q["line"]) for q in quotes] == [("TOTAL_CARDS", 4.5)]


# 3. a bet logged in the same minute as a screenshot price reuses that snapshot
def test_bet_in_the_same_minute_as_a_logged_price(db, tmp_path):
    db.execute("""INSERT INTO odds_snapshots (match_id, bookmaker_id, market_id, line, over_odds, under_odds, captured_at)
                  SELECT 1, 1, market_id, 4.5, 2.0, 1.8, '2026-10-10T09:45:00Z' FROM markets WHERE code = 'TOTAL_CARDS'""")
    db.commit()
    con = DB(str(tmp_path / "t.db"), None)
    import sqlite3
    con.con.row_factory = sqlite3.Row
    r = log_bet(con, FakeProjector(), "Arsenal v Leeds over 4.5 @ 2.1 Sky Bet £10", NOW, 7, 0.05,
                placed=datetime(2026, 10, 10, 9, 45, tzinfo=timezone.utc), espn_cards=tmp_path / "none.csv")
    snaps = con.con.execute("SELECT snapshot_id, over_odds, under_odds FROM odds_snapshots").fetchall()
    assert [tuple(s) for s in snaps] == [(r["snapshot_id"], 2.0, 1.8)]   # the screenshot's prices stay
    assert con.con.execute("SELECT taken_odds FROM value_bets").fetchone()[0] == 2.1


# 4. one block that can't be placed doesn't stop the rest of the price file
def test_price_file_skips_a_block_it_cant_place(db, tmp_path, capsys):
    from log_prices import log_blocks, parse_file
    con = DB(str(tmp_path / "t.db"), None)
    import sqlite3
    con.con.row_factory = sqlite3.Row
    blocks = parse_file("@ 2026-10-04 09:00 | Arsenal v Leeds | total\n4.5 2/1 1/3\n"   # 6 days ahead: in
                        "@ 2026-10-09 09:00 | Arsenal v Chelsea | total\n4.5 2/1 1/3\n"  # no such match
                        "@ 2026-10-10 13:00 | Arsenal v Leeds | home\n1.5 2/1 1/3\n")     # after kickoff
    assert log_blocks(con, blocks, "Sky Bet") == 1
    out = capsys.readouterr().out
    assert out.count("SKIPPED") == 2


# 5. experiment A scores the latest two-sided price, not a bet's later over-only snapshot
def test_experiment_a_keeps_the_two_sided_price():
    from experiment_a import latest_prices
    snaps = [dict(match_id=1, code="TOTAL_CARDS", line=4.5, over_odds=1.83, under_odds=1.83, at=1),
             dict(match_id=1, code="TOTAL_CARDS", line=4.5, over_odds=1.83, under_odds=None, at=2),   # the bet
             dict(match_id=1, code="TOTAL_CARDS", line=5.5, over_odds=2.8, under_odds=None, at=1),
             dict(match_id=1, code="TOTAL_CARDS", line=5.5, over_odds=3.0, under_odds=None, at=2)]
    got = latest_prices(snaps)
    assert got[(1, "TOTAL_CARDS", 4.5)]["at"] == 1
    assert got[(1, "TOTAL_CARDS", 5.5)]["at"] == 2      # only one-sided prices: the latest, as before


# 6. a name whose matching words fit two teams isn't guessed
def test_name_matching_two_teams_is_not_guessed(db, tmp_path):
    db.executemany("INSERT INTO teams (name, country) VALUES (?, 'France')",
                   [("Paris SG",), ("Paris FC",), ("Le Havre",), ("Le Mans",)])
    db.executemany("INSERT INTO teams (name, country) VALUES (?, 'Spain')", [("Sociedad",), ("Real Madrid",)])
    db.commit()
    con = DB(str(tmp_path / "t.db"), None)
    ids = lambda country: {t for (t,) in con.q("SELECT team_id FROM teams WHERE country = ?", country)}
    name = lambda tid: con.one("SELECT name FROM teams WHERE team_id = ?", tid)
    fr, es = TeamResolver(con, ids("France")), TeamResolver(con, ids("Spain"))
    assert fr.resolve("Paris Saint-Germain", "test", warn=False) is None     # "paris" fits both Paris teams
    assert name(fr.resolve("Le Havre AC", "test")) == "Le Havre"             # Le Mans shares "le" only
    assert name(es.resolve("Real Sociedad", "test")) == "Sociedad"           # Real Madrid shares "real" only


# 7. the bet queue: every queued bet is logged and removed, oldest first
def test_bet_queue(tmp_path):
    from bet_queue import add, run
    q = tmp_path / "queue"
    t = datetime(2026, 11, 14, 11, 0, tzinfo=timezone.utc)
    add(q, dict(BET="A v B over 4.5 @ 2.0 Sky Bet £10", RUN_ID="1"), now=t)
    add(q, dict(BET="C v D over 3.5 @ 1.9 Sky Bet £10", PLACED="2026-11-14 10:00", ADJ="0.5", RUN_ID="2"),
        now=t.replace(minute=5))
    calls = []

    class Done:
        def __init__(self, code): self.returncode, self.stdout, self.stderr = code, "out", ""

    def runner(args, **kw):
        calls.append(args)
        return Done(0 if "A v B" in args[args.index("--log-bet") + 1] else 1)

    results = run(q, tmp_path / "out.json", "cards.db", runner)
    assert [r["ok"] for r in results] == [True, False]
    assert calls[0][calls[0].index("--placed") + 1] == "2026-11-14 11:00"     # queued time, UK (GMT in November)
    assert calls[1][-4:] == ["--placed", "2026-11-14 10:00", "--adjusted-p", "0.5"]
    assert not list(q.glob("*.json"))
    assert json.loads((tmp_path / "out.json").read_text())[1]["bet"].startswith("C v D")


# Experiment A amendment (11 Nov 2026): closing-line value against Sky Bet's price in the last 30 minutes
def test_clv_against_sky_bets_closing_screenshot(db):
    from experiment_a import sky_clv
    bet = add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.2, notes="manual entry: test")      # snapshot at 10:00 on 9 Oct
    snap = lambda when, over, under: db.execute(
        """INSERT INTO odds_snapshots (match_id, bookmaker_id, market_id, line, over_odds, under_odds, captured_at)
           SELECT 1, 1, market_id, 4.5, ?, ?, ? FROM markets WHERE code = 'TOTAL_CARDS'""", (over, under, when))
    snap("2026-10-10T10:30:00Z", 1.5, 2.5)        # an hour before kickoff: not the close
    assert sky_clv(db, "2026-10-01") == []
    snap("2026-10-10T11:10:00Z", 2.0, 1.8)        # 20 minutes before kickoff (11:30): the close
    snap("2026-10-10T11:31:00Z", 3.0, 1.3)        # after kickoff: ignored
    db.commit()
    [c] = sky_clv(db, "2026-10-01")
    fair_p = (1 / 2.0) / (1 / 2.0 + 1 / 1.8)
    assert c["bet_id"] == bet and c["clv"] == pytest.approx(2.2 * fair_p - 1) and not c["assumed"]
    assert sky_clv(db, "2026-10-10") == []         # bets placed before the start date don't count


# Screenshot reminders for the daily and Saturday phone alerts
def test_reminders_list_unpriced_benchmark_matches_and_closing_screenshots(db):
    from experiment_a import is_benchmark, reminders
    eid = next(f"espn-fx:{i}" for i in range(401870000, 401871000) if is_benchmark(f"espn-fx:{i}"))
    db.execute("UPDATE matches SET external_id = ? WHERE match_id = 1", (eid,))   # Arsenal v Leeds, 10 Oct 11:30 UTC
    db.commit()
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    assert reminders(db, now) == [
        "Experiment A benchmark matches: screenshot Sky Bet's Total cards over/under tab (both columns):",
        "  Sat 12:30 Arsenal v Leeds (EPL)"]                                            # UK time (BST)
    add_bet(db, "AWAY_CARDS", "over", 1.5, 2.0)                                         # a staked Leeds cards bet
    db.execute("""INSERT INTO odds_snapshots (match_id, bookmaker_id, market_id, line, over_odds, under_odds, captured_at)
                  SELECT 1, 1, market_id, 4.5, 2.0, 1.8, '2026-10-09T11:00:00Z' FROM markets WHERE code = 'TOTAL_CARDS'""")
    db.commit()
    assert reminders(db, now) == [                                                      # priced: benchmark done
        "Your bets: screenshot the same line in the 30 minutes before kickoff (closing price):",
        "  Arsenal v Leeds: Leeds cards over 1.5, screenshot between Sat 12:00 and 12:30"]
    assert reminders(db, now, hours=12) == []                                           # nothing due yet


def test_remind_prints_nothing_when_nothing_is_due(db, tmp_path, capsys):
    from argparse import Namespace
    from experiment_a import cmd_remind
    db.execute("UPDATE matches SET status = 'finished'")
    db.commit()
    cmd_remind(Namespace(db=str(tmp_path / "t.db"), hours=48))
    assert capsys.readouterr().out == ""                   # not even a newline: the alert step sends on any output
