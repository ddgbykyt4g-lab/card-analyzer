import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from conftest import add_bet  # noqa: E402
import betfair  # noqa: E402
from betfair import (Betfair, fair_over, import_csvs, login_help, parse_card_market, snapshot)  # noqa: E402
from collect_cards import DB  # noqa: E402

KICKOFF = "2026-10-10T11:30:00Z"


def test_parse_card_market_total_team_and_rejects():
    ou = ["Under 4.5 Cards", "Over 4.5 Cards"]
    assert parse_card_market("Cards Over/Under 4.5", ou, "Arsenal", "Leeds") == ("TOTAL_CARDS", 4.5)
    assert parse_card_market("Arsenal Cards Over/Under 1.5", ["Under 1.5", "Over 1.5"], "Arsenal", "Leeds") == \
        ("HOME_CARDS", 1.5)
    assert parse_card_market("Leeds Cards O/U 2.5", ["Over 2.5", "Under 2.5"], "Arsenal", "Leeds") == ("AWAY_CARDS", 2.5)
    assert parse_card_market("Booking Points Over/Under 35.5", ["Under 35.5", "Over 35.5"], "Arsenal", "Leeds") is None
    assert parse_card_market("First Half Cards 1.5", ou, "Arsenal", "Leeds") is None
    assert parse_card_market("Cards Over/Under 4.5", ["Under 4.5", "Over 5.5"], "Arsenal", "Leeds") is None
    assert parse_card_market("Over/Under 2.5 Goals", ["Under 2.5 Goals", "Over 2.5 Goals"], "Arsenal", "Leeds") is None


def test_fair_over_uses_midpoints_and_normalises():
    row = dict(back_over="2.0", lay_over="2.04", back_under="2.0", lay_under="2.04")
    assert fair_over(row) == pytest.approx(0.5)
    one_side = dict(back_over="1.8", lay_over="1.9", back_under="", lay_under="")
    assert fair_over(one_side) == pytest.approx((1 / 1.8 + 1 / 1.9) / 2)
    backs_only = dict(back_over="1.9", lay_over="", back_under="2.1", lay_under="")
    assert fair_over(backs_only) == pytest.approx((1 / 1.9) / (1 / 1.9 + 1 / 2.1))
    assert fair_over(dict(back_over="", lay_over="", back_under="", lay_under="")) is None


def test_client_refuses_betting_operations():
    with pytest.raises(ValueError):
        Betfair("key", interval=0).call("placeOrders", {})


def test_login_help_points_to_uk_runner():
    assert "BETFAIR_RUNNER" in login_help("BETTING_RESTRICTED_LOCATION")


class FakeBetfair:
    """Canned listEvents / listMarketCatalogue / listMarketBook answers."""

    def __init__(self, over="2.1", under="1.9"):
        self.over, self.under, self.calls = over, under, []

    def call(self, method, params):
        self.calls.append(method)
        if method == "listEvents":
            return [{"event": {"id": "35", "name": "Arsenal v Leeds", "openDate": "2026-10-10T11:30:00.000Z"}},
                    {"event": {"id": "36", "name": "Arsenal Women v Leeds Women", "openDate": "2026-10-10T11:30:00.000Z"}},
                    {"event": {"id": "37", "name": "Real Madrid v Barcelona", "openDate": "2026-10-10T19:00:00.000Z"}}]
        if method == "listMarketCatalogue":
            assert params["filter"]["eventIds"] == ["35"]
            ev = {"id": "35", "name": "Arsenal v Leeds"}
            return [
                {"marketId": "1.1", "marketName": "Cards Over/Under 4.5", "event": ev,
                 "runners": [{"selectionId": 10, "runnerName": "Under 4.5 Cards", "sortPriority": 1},
                             {"selectionId": 11, "runnerName": "Over 4.5 Cards", "sortPriority": 2}]},
                {"marketId": "1.2", "marketName": "Leeds Cards Over/Under 1.5", "event": ev,
                 "runners": [{"selectionId": 20, "runnerName": "Under 1.5", "sortPriority": 1},
                             {"selectionId": 21, "runnerName": "Over 1.5", "sortPriority": 2}]},
                {"marketId": "1.3", "marketName": "Booking Points 35.5", "event": ev,
                 "runners": [{"selectionId": 30, "runnerName": "Under 35.5"}, {"selectionId": 31, "runnerName": "Over 35.5"}]},
                {"marketId": "1.4", "marketName": "Match Odds", "event": ev, "runners": []},
            ]
        if method == "listMarketBook":
            assert set(params["marketIds"]) == {"1.1", "1.2"}
            ex = lambda b, l: {"availableToBack": [{"price": float(b), "size": 25.0}],
                               "availableToLay": [{"price": float(l), "size": 12.0}]}
            return [{"marketId": "1.1", "status": "OPEN", "inplay": False, "totalMatched": 140.0,
                     "runners": [{"selectionId": 11, "ex": ex(self.over, float(self.over) + 0.06)},
                                 {"selectionId": 10, "ex": ex(self.under, float(self.under) + 0.04)}]},
                    {"marketId": "1.2", "status": "OPEN", "inplay": True, "runners": []}]  # in play: skipped
        raise AssertionError(method)


def read(path):
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_snapshot_logs_card_markets_and_only_changes(db, tmp_path):
    con = db
    path = tmp_path / "t.db"
    con.close()
    bdb = DB(str(path), None)
    out = tmp_path / "exchange"
    early = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)
    stats = snapshot(FakeBetfair(), bdb, early, 36, out)
    assert stats == dict(fixtures=1, events=1, markets=2, rows=1)
    rows = read(out / "2026-10.csv")
    assert rows[0]["market_code"] == "TOTAL_CARDS" and rows[0]["line"] == "4.5"
    assert rows[0]["back_over"] == "2.1" and rows[0]["lay_under"] == "1.94" and rows[0]["match_id"] == "1"
    cat = read(out / "catalogue.csv")
    assert {c["market_name"]: c["parsed"] for c in cat} == {
        "Cards Over/Under 4.5": "TOTAL_CARDS 4.5", "Leeds Cards Over/Under 1.5": "AWAY_CARDS 1.5",
        "Booking Points 35.5": ""}
    # unchanged prices hours before kickoff: no new row; inside 90 minutes: always a row
    assert snapshot(FakeBetfair(), bdb, datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc), 36, out)["rows"] == 0
    assert snapshot(FakeBetfair(), bdb, datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc), 36, out)["rows"] == 1
    assert len(read(out / "catalogue.csv")) == 3  # patterns recorded once


def test_import_marks_closing_and_fills_clv(db, tmp_path):
    bet = add_bet(db, "TOTAL_CARDS", "over", 4.5, 2.30)
    out = tmp_path / "exchange"
    db.close()
    bdb = DB(str(tmp_path / "t.db"), None)
    snapshot(FakeBetfair("2.2", "1.8"), bdb, datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc), 36, out)
    snapshot(FakeBetfair("2.0", "2.0"), bdb, datetime(2026, 10, 10, 11, 7, tzinfo=timezone.utc), 36, out)
    con = sqlite3.connect(tmp_path / "t.db")
    before = import_csvs(con, datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc), [out / "2026-10.csv"])
    assert before["closing"] == 0 and before["clv_filled"] == 0  # not kicked off yet
    stats = import_csvs(con, datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc), [out / "2026-10.csv"])
    assert stats["snapshots_added"] == 0 and stats["closing"] == 1 and stats["clv_filled"] == 1
    closing = con.execute("""SELECT over_odds, captured_at FROM odds_snapshots o JOIN bookmakers b USING (bookmaker_id)
                             WHERE b.name = 'Betfair Exchange' AND is_closing = 1""").fetchall()
    assert closing == [(2.0, "2026-10-10T11:07:00Z")]
    p = (1 / 2.0 + 1 / 2.06) / 2
    p = p / (p + (1 / 2.0 + 1 / 2.04) / 2)
    odds, clv = con.execute("SELECT closing_odds, clv FROM value_bets WHERE bet_id = ?", (bet,)).fetchone()
    assert odds == pytest.approx(1 / p, abs=1e-4) and clv == pytest.approx(2.30 * p - 1, abs=1e-3)
    assert con.execute("SELECT is_sharp FROM bookmakers WHERE name = 'Betfair Exchange'").fetchone() == (1,)
    assert import_csvs(con, datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc), [out / "2026-10.csv"])["clv_filled"] == 0


def test_main_without_secrets_exits_2(monkeypatch, tmp_path):
    for k in ("BETFAIR_APP_KEY", "BETFAIR_USERNAME", "BETFAIR_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert betfair.main(["log", "--db", str(tmp_path / "x.db")]) == 2


def test_health_flags_quiet_exchange_log(db, tmp_path):
    from health import exchange_problem
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    folder = tmp_path / "exchange"
    assert exchange_problem(db, now, folder) is None  # logger never set up
    folder.mkdir()
    (folder / "2026-10.csv").write_text("captured_at,match_id\n2026-10-09T18:00:00Z,1\n")
    assert "nothing logged since 2026-10-09 18:00" in exchange_problem(db, now, folder)
    assert exchange_problem(db, datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc), folder) is None  # recent


# ───────────── match identity across collector runs, idempotency, empty input ─────────────
def logged_row(match_id, home="Arsenal", away="Leeds", kickoff=KICKOFF, ext="", captured="2026-10-10T10:30:00Z"):
    return dict(captured_at=captured, match_id=str(match_id), kickoff_utc=kickoff, bf_event_id="35",
                bf_market_id="1.1", market_name="Cards Over/Under 4.5", market_code="TOTAL_CARDS", line="4.5",
                back_over="2.0", back_over_size="10", lay_over="2.04", lay_over_size="5", back_under="2.0",
                back_under_size="10", lay_under="2.04", lay_under_size="5", total_matched="100",
                external_id=ext, home=home, away=away)


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


def snapshot_matches(con):
    return [m for (m,) in con.execute("""SELECT o.match_id FROM odds_snapshots o JOIN bookmakers b USING (bookmaker_id)
                                         WHERE b.name = 'Betfair Exchange'""")]


def test_import_refinds_match_after_merge_and_id_reuse(db, tmp_path):
    # logged as match 1 (Arsenal v Leeds, ESPN 999); a later run merged it into a new row 3 and gave id 1's
    # teams to nobody: row 1 is now a different fixture (id reused after a delete)
    db.execute("INSERT INTO teams (name, country) VALUES ('Chelsea', 'England')")
    db.execute("UPDATE matches SET home_team_id = 3, away_team_id = 2 WHERE match_id = 1")  # id 1 reused
    db.execute("""INSERT INTO matches (match_id, season_id, kickoff_utc, home_team_id, away_team_id, status, external_id)
                  VALUES (3, 1, ?, 1, 2, 'scheduled', 'espn:999')""", (KICKOFF,))
    db.commit()
    by_espn = write_rows(tmp_path / "a.csv", [logged_row(1, ext="espn-fx:999")])
    import_csvs(db, datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc), [by_espn])
    assert snapshot_matches(db) == [3]
    by_teams = write_rows(tmp_path / "b.csv", [logged_row(1, captured="2026-10-10T10:40:00Z")])  # no ESPN id
    import_csvs(db, datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc), [by_teams])
    assert snapshot_matches(db) == [3, 3]  # never attached to the reused id 1 (Chelsea v Leeds)


def test_import_follows_moved_fixture_and_drops_postponed(db, tmp_path):
    db.execute("UPDATE matches SET kickoff_utc = '2026-10-10T14:00:00Z' WHERE match_id = 1")  # moved 2.5 hours
    db.commit()
    p = write_rows(tmp_path / "m.csv", [logged_row(1)])
    stats = import_csvs(db, datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc), [p])
    assert stats["snapshots_added"] == 1 and stats["skipped"] == 0
    db.execute("UPDATE matches SET kickoff_utc = '2026-11-04T19:45:00Z' WHERE match_id = 1")  # re-played weeks later
    db.commit()
    p2 = write_rows(tmp_path / "p.csv", [logged_row(1, captured="2026-10-10T10:45:00Z")])
    stats = import_csvs(db, datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc), [p2])
    assert stats["snapshots_added"] == 0 and stats["skipped"] == 1
    db.execute("UPDATE matches SET status = 'postponed' WHERE match_id = 1")
    db.commit()
    assert import_csvs(db, datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc), [p2])["skipped"] == 1


def test_import_old_rows_without_identity_trust_match_id(db, tmp_path):
    row = {k: v for k, v in logged_row(1).items() if k not in ("external_id", "home", "away")}
    p = write_rows(tmp_path / "old.csv", [row])
    assert import_csvs(db, datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc), [p])["snapshots_added"] == 1


def test_import_is_idempotent_and_safe_without_files(db, tmp_path, monkeypatch):
    assert import_csvs(db, datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc), [])["rows"] == 0
    empty = tmp_path / "2026-10.csv"
    empty.write_text("")
    assert import_csvs(db, datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc), [empty])["rows"] == 0
    monkeypatch.setattr(betfair, "OUT_DIR", tmp_path / "missing")
    db.close()
    assert betfair.main(["import", "--db", str(tmp_path / "t.db")]) == 0
    con = sqlite3.connect(tmp_path / "t.db")
    bet = add_bet(con, "TOTAL_CARDS", "over", 4.5, 2.30)
    p = write_rows(tmp_path / "x.csv", [logged_row(1)])
    after_ko = datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc)
    first = import_csvs(con, after_ko, [p])
    assert first["snapshots_added"] == 1 and first["clv_filled"] == 1
    snap = lambda: con.execute("SELECT COUNT(*), SUM(is_closing) FROM odds_snapshots").fetchone()
    clv = lambda: con.execute("SELECT closing_odds, clv FROM value_bets WHERE bet_id = ?", (bet,)).fetchone()
    before = (snap(), clv())
    again = import_csvs(con, after_ko, [p])
    assert again["snapshots_added"] == 0 and again["clv_filled"] == 0 and (snap(), clv()) == before


def test_snapshot_rows_carry_fixture_identity(db, tmp_path):
    db.execute("UPDATE matches SET external_id = 'espn-fx:777' WHERE match_id = 1")
    db.commit()
    db.close()
    bdb = DB(str(tmp_path / "t.db"), None)
    out = tmp_path / "exchange"
    snapshot(FakeBetfair(), bdb, datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc), 36, out)
    row = read(out / "2026-10.csv")[0]
    assert (row["external_id"], row["home"], row["away"]) == ("espn-fx:777", "Arsenal", "Leeds")


def test_value_finder_ignores_sharp_books(db):
    from value_finder import latest_quotes
    db.execute("INSERT INTO bookmakers (name, is_sharp) VALUES ('Betfair Exchange', 1)")
    total = db.execute("SELECT market_id FROM markets WHERE code = 'TOTAL_CARDS'").fetchone()[0]
    for book in (1, 2):
        db.execute("""INSERT INTO odds_snapshots (match_id, bookmaker_id, market_id, line, over_odds, under_odds,
                      captured_at) VALUES (1, ?, ?, 4.5, 2.0, 1.9, '2026-10-10T09:00:00Z')""", (book, total))
    db.commit()
    db.row_factory = sqlite3.Row
    quotes = latest_quotes(db, 1, datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc), 24)
    assert [q["bookmaker"] for q in quotes] == ["Sky Bet"]
