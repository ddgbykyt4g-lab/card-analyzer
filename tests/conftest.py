import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def db(tmp_path):
    """Empty database with the real schema, plus one Premier League match and Sky Bet's card rules."""
    con = sqlite3.connect(tmp_path / "t.db")
    con.executescript((ROOT / "card_value_schema.sql").read_text())
    con.execute("INSERT INTO seasons (league_id, label, start_date) SELECT league_id, '2026-27', '2026-08-01' "
                "FROM leagues WHERE code = 'EPL'")
    con.executemany("INSERT INTO teams (name, country) VALUES (?, 'England')", [("Arsenal",), ("Leeds",)])
    con.execute("""INSERT INTO matches (season_id, kickoff_utc, home_team_id, away_team_id, status)
                   VALUES (1, '2026-10-10T11:30:00Z', 1, 2, 'scheduled')""")
    con.execute("INSERT INTO bookmakers (name) VALUES ('Sky Bet')")
    for code, scope in (("TOTAL_CARDS", "match"), ("HOME_CARDS", "home"), ("AWAY_CARDS", "away"),
                        ("EACH_TEAM_CARDS", "match")):
        con.execute("INSERT INTO markets (code, unit, scope) VALUES (?, 'cards', ?)", (code, scope))
        con.execute("INSERT INTO settlement_rules SELECT 1, market_id, 1, 1, 1, 0 FROM markets WHERE code = ?",
                    (code,))
    con.commit()
    return con


def add_bet(con, market: str, selection: str, line: float, odds: float, stake: float = 10.0,
            notes: str = "manual entry: test | kickoff=2026-10-10T11:30:00Z") -> int:
    mid = con.execute("SELECT market_id FROM markets WHERE code = ?", (market,)).fetchone()[0]
    snap = con.execute(f"INSERT INTO odds_snapshots (match_id, bookmaker_id, market_id, line, "
                       f"{selection}_odds, captured_at) VALUES (1, 1, ?, ?, ?, '2026-10-09T10:00:00Z')",
                       (mid, line, odds)).lastrowid
    bet = con.execute("""INSERT INTO value_bets (snapshot_id, selection, model_version, model_prob, taken_odds,
                             stake, placed_at, notes) VALUES (?, ?, 'test', 0.5, ?, ?, '2026-10-09T10:00:00Z', ?)""",
                      (snap, selection, odds, stake, notes)).lastrowid
    con.commit()
    return bet


def finish(con, home: tuple, away: tuple) -> None:
    """Cards as (yellows incl. first of a two-yellow dismissal, second_yellows, straight_reds)."""
    con.execute("UPDATE matches SET status = 'finished', home_goals = 1, away_goals = 0 WHERE match_id = 1")
    for team, is_home, (y, y2, r) in ((1, 1, home), (2, 0, away)):
        con.execute("""INSERT INTO match_team_cards (match_id, team_id, is_home, yellows, second_yellows, straight_reds)
                       VALUES (1, ?, ?, ?, ?, ?)""", (team, is_home, y, y2, r))
    con.commit()
