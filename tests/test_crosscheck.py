"""Two card sources: the two-yellow rule, ESPN's counts, and settlement when football-data and ESPN differ."""
import sys
from datetime import datetime, timezone

import pytest

from conftest import ROOT, add_bet, finish

sys.path.insert(0, str(ROOT / "agent"))

from collect_cards import DB, espn_card_counts, two_yellow_split
from settle import confirm, disputes, espn_total, read_espn_cards, settle
from value_finder import Weights


def result(con, bet_id):
    return con.execute("SELECT result, settled_value, profit FROM value_bets WHERE bet_id = ?", (bet_id,)).fetchone()


def espn(home=(2, 0, 0), away=(3, 0, 0), eid="900", kickoff="2026-10-10T11:30:00Z", league="EPL",
         names=("Arsenal", "Leeds")) -> dict:
    """ESPN's file as read_espn_cards returns it: (yellows, reds, staff) per side."""
    row = dict(espn_id=eid, league=league, kickoff_utc=kickoff, home=names[0], away=names[1])
    for ha, (y, r, staff) in (("home", home), ("away", away)):
        row.update({f"{ha}_yellows": str(y), f"{ha}_reds": str(r), f"{ha}_staff": str(staff)})
    return {eid: row}


@pytest.fixture
def match(db):
    db.execute("UPDATE matches SET external_id = 'espn-fx:900' WHERE match_id = 1")
    db.commit()
    return db


# ── football-data's three ways of counting a two-yellow dismissal ──
@pytest.mark.parametrize("fd_yellows, fd_reds, espn_yellows, two, stored", [
    (6, 1, 5, 1, (5, 1, 0)),  # both of the dismissed player's yellows counted (Chelsea v Man Utd, Oct 2026)
    (2, 1, 1, 1, (1, 1, 0)),  # the same with no other bookings (Coventry v Sunderland)
    (5, 1, 5, 1, (5, 1, 0)),  # the first yellow only
    (4, 1, 5, 1, (5, 1, 0)),  # neither: England until 2025-26
    (6, 2, 5, 1, (5, 1, 1)),  # a straight red in the same team stays a straight red
    (8, 2, 6, 2, (6, 2, 0)),  # two such dismissals
    (7, 1, 5, 1, (6, 1, 0)),  # sources a card apart on the rest: the nearest convention, football-data kept
    (3, 1, 3, 0, (3, 0, 1)),  # no second yellow: untouched
])
def test_two_yellow_split(fd_yellows, fd_reds, espn_yellows, two, stored):
    assert two_yellow_split(fd_yellows, fd_reds, espn_yellows, two) == stored


def test_reload_keeps_a_split_under_any_convention_and_resplit_redoes_it(match, tmp_path):
    db = DB(str(tmp_path / "t.db"), None)
    cards = lambda: db.con.execute("SELECT yellows, second_yellows, straight_reds, reds_1h FROM match_team_cards "
                                   "WHERE match_id = 1 AND team_id = 1").fetchone()
    db.upsert_cards(1, 1, 1, yellows=6, second_yellows=0, straight_reds=1, fouls=10)
    db.con.execute("UPDATE match_team_cards SET yellows = 5, second_yellows = 1, straight_reds = 0, reds_1h = 0")
    for fd_yellows in (6, 5, 4):  # football-data's row, whichever way it counts the dismissal
        db.upsert_cards(1, 1, 1, yellows=fd_yellows, second_yellows=0, straight_reds=1, fouls=10)
        assert cards() == (5, 1, 0, 0)
    db.upsert_cards(1, 1, 1, yellows=7, second_yellows=0, straight_reds=1, fouls=10)  # football-data changed
    assert cards() == (7, 0, 1, None)
    db.con.execute("UPDATE match_team_cards SET yellows = 8, second_yellows = 1, straight_reds = 0, reds_1h = 0")
    db.resplit = True  # the old rule's result, one too many: --resplit starts it again from football-data
    db.upsert_cards(1, 1, 1, yellows=7, second_yellows=0, straight_reds=1, fouls=10)
    assert cards() == (7, 0, 1, None)


# ── ESPN's card events ──
def test_espn_counts_player_cards_and_sets_staff_cards_apart():
    comp = dict(competitors=[dict(homeAway="home", team=dict(id="357")), dict(homeAway="away", team=dict(id="367"))],
                details=[dict(team=dict(id="367"), yellowCard=True, athletesInvolved=[dict(displayName="A")]),
                         dict(team=dict(id="367"), yellowCard=True),  # the manager: no player on the event
                         dict(team=dict(id="367"), redCard=True, athletesInvolved=[dict(displayName="A")]),
                         dict(team=dict(id="357"), yellowCard=True, athletesInvolved=[dict(displayName="B")]),
                         dict(team=dict(id="357"), scoringPlay=True, athletesInvolved=[dict(displayName="B")])])
    assert espn_card_counts(comp) == dict(home=dict(yellows=1, reds=0, staff=0), away=dict(yellows=1, reds=1, staff=1))
    assert espn_card_counts(dict(competitors=[])) is None


def test_espn_file_round_trip(tmp_path):
    assert read_espn_cards(tmp_path / "none.csv") == {}
    path = tmp_path / "espn_cards.csv"
    path.write_text("espn_id,league,kickoff_utc,home,away,home_yellows,home_reds,home_staff,away_yellows,away_reds,"
                    "away_staff,fetched_at\n900,EPL,2026-10-10T11:30:00Z,Arsenal,Leeds,2,0,0,3,0,1,2026-10-10T14:00:00Z\n")
    assert read_espn_cards(path)["900"]["away_yellows"] == "3"


def test_espn_total_needs_a_rule_that_scores_every_red_alike():
    bet = dict(market="TOTAL_CARDS", scope="match", selection="over")
    row = espn(home=(2, 1, 1), away=(3, 0, 0))["900"]
    assert espn_total(bet, Weights(1, 1, 1), False, row) == 6
    assert espn_total(bet, Weights(1, 1, 1), True, row) == 7       # a book that counts staff cards
    assert espn_total(dict(bet, scope="home"), Weights(1, 1, 1), False, row) == 3
    assert espn_total(bet, Weights(1, 1, 2), False, row) is None   # can't tell a second yellow from a straight red
    assert espn_total(bet, Weights(1, 1, 1), False, None) is None


# ── settlement with both sources ──
def test_settles_when_both_sources_give_the_same_result(match):
    bet = add_bet(match, "TOTAL_CARDS", "over", 4.5, 1.83, stake=8.08)
    finish(match, home=(2, 0, 0), away=(3, 0, 0))
    settle(match, espn(home=(3, 0, 0), away=(3, 0, 0)))  # a card apart, but over 4.5 either way
    assert result(match, bet) == ("won", 5.0, pytest.approx(6.71))
    assert disputes(match, espn(home=(3, 0, 0), away=(3, 0, 0))) == []


def test_holds_a_staked_bet_the_sources_would_settle_differently(match):
    """Leeds v Tottenham, 7 Nov 2026: football-data had 3 cards, ESPN (and Sky Bet) 5."""
    bet = add_bet(match, "TOTAL_CARDS", "over", 4.5, 1.83, stake=8.08)
    paper = add_bet(match, "TOTAL_CARDS", "over", 3.5, 1.4, stake=0)
    finish(match, home=(1, 0, 0), away=(2, 0, 0))
    cards = espn(home=(2, 0, 0), away=(3, 0, 1))  # the manager's yellow doesn't count
    settle(match, cards)
    assert result(match, bet)[0] == "pending"
    assert result(match, paper)[0] == "lost"  # paper bets settle on football-data alone
    found = disputes(match, cards)
    assert [(d["bet_id"], d["football_data"], d["espn"]) for d in found] == [(bet, 3.0, 5.0)]
    confirm(match, bet, "won", cards)
    assert result(match, bet) == ("won", 5.0, pytest.approx(6.71))
    assert "confirmed by user: won (football-data 3, ESPN 5)" in match.execute(
        "SELECT notes FROM value_bets WHERE bet_id = ?", (bet,)).fetchone()[0]
    assert disputes(match, cards) == []
    settle(match, cards)  # a later run leaves the confirmed result alone
    assert result(match, bet)[0] == "won"


def test_a_bet_settled_before_the_check_existed_is_listed_and_can_be_corrected(match):
    bet = add_bet(match, "TOTAL_CARDS", "over", 4.5, 1.83, stake=8.08)
    finish(match, home=(1, 0, 0), away=(2, 0, 0))
    settle(match)  # no ESPN file: football-data alone, as before
    assert result(match, bet)[0] == "lost"
    cards = espn(home=(2, 0, 0), away=(3, 0, 0))
    assert [d["bet_id"] for d in disputes(match, cards)] == [bet]
    confirm(match, bet, "won", cards)
    assert result(match, bet) == ("won", 5.0, pytest.approx(6.71))


def test_settles_on_espn_the_same_night_and_flags_it_if_football_data_later_differs(match):
    team = add_bet(match, "HOME_CARDS", "over", 2.5, 1.95)
    paper = add_bet(match, "HOME_CARDS", "over", 1.5, 1.3, stake=0)
    cards = espn(home=(3, 0, 0), away=(1, 0, 0))
    settle(match, cards)  # the match is still 'scheduled' here: football-data hasn't posted it
    assert result(match, team) == ("won", 3.0, pytest.approx(9.5))
    assert result(match, paper)[0] == "pending"
    assert "settled on ESPN's card events" in match.execute(
        "SELECT notes FROM value_bets WHERE bet_id = ?", (team,)).fetchone()[0]
    assert disputes(match, cards) == []
    finish(match, home=(2, 0, 0), away=(1, 0, 0))  # football-data arrives with a different count
    assert [(d["bet_id"], d["result"]) for d in disputes(match, cards)] == [(team, "won")]


def test_no_espn_row_settles_on_football_data_as_before(match):
    bet = add_bet(match, "TOTAL_CARDS", "under", 4.5, 2.0)
    finish(match, home=(2, 0, 0), away=(1, 0, 0))
    settle(match, espn(eid="other"))
    assert result(match, bet) == ("won", 3.0, 10.0)


# ── health check ──
def test_health_reports_a_disputed_bet_and_a_league_whose_sources_disagree(match, tmp_path):
    from health import SOURCE_GAP, problems
    now = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    path = str(tmp_path / "t.db")
    bet = add_bet(match, "TOTAL_CARDS", "over", 4.5, 1.83)
    finish(match, home=(1, 0, 0), away=(2, 0, 0))
    settle(match)
    cards = espn(home=(2, 0, 0), away=(3, 0, 0))
    found = problems(path, None, now, cards)
    assert any(f"Bet #{bet} Arsenal v Leeds over 4.5 (recorded lost): football-data counts 3, ESPN 5" in p
               for p in found)
    assert not any("count different cards" in p for p in found)  # one match apart is normal
    for i in range(2, SOURCE_GAP + 1):  # the same wrong round across the league
        mid = match.execute("""INSERT INTO matches (season_id, kickoff_utc, home_team_id, away_team_id, status,
                                   external_id) VALUES (1, ?, 1, 2, 'finished', ?)""",
                            (f"2026-10-{i:02d}T14:00:00Z", f"espn-fx:{900 + i}")).lastrowid
        match.executemany("INSERT INTO match_team_cards (match_id, team_id, is_home, yellows) VALUES (?, ?, ?, 1)",
                          [(mid, 1, 1), (mid, 2, 0)])
        cards.update(espn(home=(2, 0, 0), away=(1, 0, 0), eid=str(900 + i), kickoff=f"2026-10-{i:02d}T14:00:00Z"))
    match.commit()
    assert any(p.startswith(f"EPL: football-data and ESPN count different cards in {SOURCE_GAP} matches")
               for p in problems(path, None, now, cards))
    assert problems(path, None, now) == problems(path, None, now, {})  # no ESPN file: the old checks only


# ── ESPN's count stored where football-data's differs (football-data's 7 Nov 2026 rows slid one match) ──
def stored(con):
    return con.execute("""SELECT is_home, yellows, second_yellows, straight_reds, reds_1h FROM match_team_cards
                          WHERE match_id = 1 ORDER BY is_home DESC""").fetchall()


def test_espn_count_replaces_football_data_where_they_differ(match, tmp_path):
    from collect_cards import apply_espn_counts
    finish(match, home=(1, 0, 0), away=(2, 0, 0))                        # Leeds v Tottenham as football-data had it
    match.execute("UPDATE match_team_cards SET reds_1h = 0")
    match.commit()
    db = DB(str(tmp_path / "t.db"), None)
    assert apply_espn_counts(db, espn(home=(2, 0, 0), away=(3, 0, 0))) == ["Arsenal v Leeds"]
    assert stored(db.con) == [(1, 2, 0, 0, 0), (0, 3, 0, 0, 0)]
    assert apply_espn_counts(db, espn(home=(2, 0, 0), away=(3, 0, 0))) == []   # nothing left to change


def test_espn_reds_reset_the_split_for_split_reds(match, tmp_path):
    from collect_cards import apply_espn_counts
    finish(match, home=(4, 0, 1), away=(2, 0, 0))                        # Roma's row carried Parma's red
    db = DB(str(tmp_path / "t.db"), None)
    apply_espn_counts(db, espn(home=(0, 0, 0), away=(3, 1, 0)))
    assert stored(db.con) == [(1, 0, 0, 0, None), (0, 3, 0, 1, None)]   # split_reds then checks the away red


def test_two_yellow_convention_is_not_a_disagreement(match, tmp_path):
    from collect_cards import apply_espn_counts
    finish(match, home=(6, 0, 1), away=(2, 0, 0))   # football-data counts both yellows of the dismissed player
    db = DB(str(tmp_path / "t.db"), None)
    assert apply_espn_counts(db, espn(home=(5, 1, 0), away=(2, 0, 0))) == []


def test_match_with_a_pending_staked_bet_keeps_both_sources(match, tmp_path):
    from collect_cards import apply_espn_counts
    bet = add_bet(match, "TOTAL_CARDS", "over", 4.5, 2.0)
    finish(match, home=(1, 0, 0), away=(2, 0, 0))
    db = DB(str(tmp_path / "t.db"), None)
    two_sources = espn(home=(2, 0, 0), away=(3, 0, 0))
    assert apply_espn_counts(db, two_sources) == []                       # settle.py lists it as CHECK instead
    confirm(db.con, bet, "won", two_sources)
    assert apply_espn_counts(db, two_sources) == ["Arsenal v Leeds"]       # once confirmed, corrected
