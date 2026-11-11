"""Several bookmakers: a price block names its bookmaker, each bookmaker's card rules are entered once, and the
price check shows the best value across bookmakers (each priced under its own rules)."""
import sqlite3
import sys
from datetime import datetime, timezone

from conftest import ROOT

sys.path.insert(0, str(ROOT / "agent"))

from add_book_rules import add_rules
from check_prices import best_lines, has_rules, render, verdicts
from collect_cards import DB
from log_prices import log_blocks, parse_file


def test_price_block_header_may_name_the_bookmaker():
    a, b = parse_file("@ 2026-10-24 10:28 | Valencia v Villarreal | total\n3.5 1/8 4/1\n"
                      "@ 2026-10-24 10:30 | Valencia v Villarreal | total | bet365\n3.5 1/7 7/2\n")
    assert (a["book"], b["book"]) == (None, "bet365")
    assert b["rows"] == [(3.5, 1 + 1 / 7, 4.5)]


def test_rules_are_entered_once_for_every_card_market(db):
    add_rules(db, "bet365", 1, 1, 2)
    rules = db.execute("""SELECT m.code, r.w_yellow, r.w_second_yellow, r.w_straight_red, r.counts_staff
                          FROM settlement_rules r JOIN bookmakers b USING (bookmaker_id) JOIN markets m USING (market_id)
                          WHERE b.name = 'bet365' ORDER BY m.code""").fetchall()
    assert rules == [(c, 1.0, 1.0, 2.0, 0) for c in ("AWAY_CARDS", "EACH_TEAM_CARDS", "HOME_CARDS", "TOTAL_CARDS")]
    add_rules(db, "BET365", 1, 2, 2, staff=True)                       # re-running replaces, any capitals
    assert db.execute("SELECT COUNT(*), MIN(w_second_yellow), MIN(counts_staff) FROM settlement_rules "
                      "WHERE bookmaker_id = (SELECT bookmaker_id FROM bookmakers WHERE name = 'bet365')").fetchone() == (4, 2.0, 1)
    assert has_rules(db, "Bet365") and has_rules(db, "Sky Bet") and not has_rules(db, "Paddy Power")


def test_prices_are_stored_under_each_blocks_bookmaker(db, tmp_path, capsys):
    con = DB(str(tmp_path / "t.db"), None)
    con.con.row_factory = sqlite3.Row
    blocks = parse_file("@ 2026-10-09 10:00 | Arsenal v Leeds | total\n4.5 2/1 1/3\n"
                        "@ 2026-10-09 10:05 | Arsenal v Leeds | total | Paddy Power\n4.5 9/4 1/3\n")
    assert log_blocks(con, blocks, "Sky Bet") == 2
    assert [tuple(r) for r in con.con.execute("""SELECT b.name, o.over_odds FROM odds_snapshots o JOIN bookmakers b USING (bookmaker_id)
                              ORDER BY o.captured_at""").fetchall()] == [("Sky Bet", 3.0), ("Paddy Power", 3.25)]
    assert "no card-counting rules for Paddy Power" in capsys.readouterr().out


def test_best_value_across_bookmakers_is_by_value_not_odds():
    rows = lambda over, p: [dict(line=4.5, p_over=p, p_push=0.0, over=over, under=None)]
    sky = dict(rows=verdicts(rows(2.1, 0.50)))
    other = dict(rows=verdicts(rows(2.3, 0.44)))                       # longer odds, but counts cards differently
    assert best_lines([("Sky Bet", sky), ("bet365", other)]) == [
        "  Best value across bookmakers (each under its own card rules), best first:",
        "  over 4.5: Sky Bet 2.10 (+5%) · bet365 2.30 (+1%)"]
    assert best_lines([("Sky Bet", sky)]) == []                        # one bookmaker: nothing to compare


def test_other_bookmakers_get_no_blend_columns():
    fx = dict(home="Arsenal", away="Leeds", referee_id=None, referee=None, ref_matches=0)
    block = dict(market="total", when=datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc), book="bet365")
    priced = dict(projected=4.3, p_win=None,
                  rows=verdicts([dict(line=4.5, p_over=0.5, p_push=0.0, over=2.3, under=1.6)]))
    text = render(fx, block, priced)
    assert "bet365 prices at Fri 11:00" in text and "blend" not in text and "=> current rule: over 4.5 at 2.30" in text
    assert "blend rule" in render(fx, dict(block, book="Sky Bet"), priced)
