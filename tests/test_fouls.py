import math
import sqlite3

import numpy as np
import pytest

from conftest import ROOT
from glm_model import foul_features


@pytest.fixture
def fdb(tmp_path):
    """One league, three teams, matches on three days; team 1 fouls a lot."""
    con = sqlite3.connect(tmp_path / "f.db")
    con.executescript((ROOT / "card_value_schema.sql").read_text())
    con.execute("INSERT INTO seasons (league_id, label, start_date) VALUES (1, '2026-27', '2026-08-01')")
    con.executemany("INSERT INTO teams (name, country) VALUES (?, 'England')", [("A",), ("B",), ("C",)])
    games = [(1, "2026-08-01T15:00:00Z", 1, 2, 20, 10), (2, "2026-08-01T15:00:00Z", 3, 2, 10, 10),
             (3, "2026-08-08T15:00:00Z", 1, 3, 20, 10), (4, "2026-08-15T15:00:00Z", 2, 1, 10, 20)]
    for mid, ko, h, a, fh, fa in games:
        con.execute("""INSERT INTO matches (match_id, season_id, kickoff_utc, home_team_id, away_team_id, status)
                       VALUES (?, 1, ?, ?, ?, 'finished')""", (mid, ko, h, a))
        con.execute("INSERT INTO match_team_cards (match_id, team_id, is_home, fouls) VALUES (?, ?, 1, ?)", (mid, h, fh))
        con.execute("INSERT INTO match_team_cards (match_id, team_id, is_home, fouls) VALUES (?, ?, 0, ?)", (mid, a, fa))
    con.commit()
    return con


def feats(con, prior=2.0):
    return foul_features(con, [1, 2, 3, 4], prior, 120.0)


def test_first_day_has_no_history(fdb):
    f = feats(fdb)
    assert np.allclose(f[:4], 0)  # matches 1 and 2 (same day) see nothing, including each other


def test_heavy_fouler_scores_positive_and_its_opponents_draw(fdb):
    f = feats(fdb)
    a_committed_in_match3 = f[4, 0]       # team A (home) in match 3
    c_drawn_vs_a = f[5, 1]                # team C's row: opponent A's fouls drawn... (A drew 10, league ~12.5)
    assert a_committed_in_match3 > 0
    assert f[7, 0] > f[6, 0]              # match 4: A (away) fouls more than B (home)
    assert math.isfinite(c_drawn_vs_a)


def test_no_leakage_from_the_match_itself(fdb):
    before = feats(fdb)
    fdb.execute("UPDATE match_team_cards SET fouls = 99 WHERE match_id = 4")
    after = feats(fdb)
    assert np.allclose(before[6:8], after[6:8])   # match 4's own fouls don't feed its features
    fdb.execute("UPDATE match_team_cards SET fouls = 99 WHERE match_id = 3")
    assert not np.allclose(before[6:8], feats(fdb)[6:8])  # but an earlier match's do


def test_shrinkage_pulls_toward_zero(fdb):
    assert abs(feats(fdb, prior=1000.0)[4, 0]) < abs(feats(fdb, prior=2.0)[4, 0])
