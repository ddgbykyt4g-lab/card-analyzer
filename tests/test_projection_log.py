import csv
import math
import sys

from conftest import ROOT, finish

sys.path.insert(0, str(ROOT / "agent"))
from log_projections import FIELDS  # noqa: E402
from score_projections import score  # noqa: E402


def write_log(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        for logged_at, mu, pmf in rows:
            w.writerow(dict(logged_at=logged_at, match_id=1, external_id="espn:123", league="EPL",
                            kickoff_utc="2026-10-10T11:30:00Z", home="Arsenal", away="Leeds", model_version="glm-v4",
                            book="Sky Bet", w_yellow=1.0, w_second_yellow=1.0, w_straight_red=1.0, mu_total=mu,
                            pmf=" ".join(str(p) for p in pmf)))


def test_scores_latest_projection_before_kickoff_in_sky_bet_units(db, tmp_path):
    early = [0.1] * 10
    late = [0.0] * 5 + [0.5, 0.5] + [0.0] * 4
    after = [0.0] * 4 + [1.0]                         # logged after kickoff: must be ignored
    log = tmp_path / "p.csv"
    write_log(log, [("2026-10-09T08:00:00Z", 4.0, early), ("2026-10-10T08:00:00Z", 5.5, late),
                    ("2026-10-10T12:00:00Z", 4.0, after)])
    finish(db, home=(3, 1, 0), away=(0, 0, 1))      # Sky Bet: 3 + 1 + 1 = 5 cards
    db.close()
    a = score(str(tmp_path / "t.db"), str(log))["glm-v4"]
    assert a["n"] == 1 and a["y"] == 5.0
    assert math.isclose(a["ll"], math.log(0.5))
    assert math.isclose(a["se"], 0.25)
    assert a["o4.5"] == 1 and a["o5.5"] == 0


def test_unfinished_match_is_not_scored(db, tmp_path):
    log = tmp_path / "p.csv"
    write_log(log, [("2026-10-09T08:00:00Z", 4.0, [0.1] * 10)])
    db.close()
    assert score(str(tmp_path / "t.db"), str(log)) == {}


def test_row_whose_match_id_now_belongs_to_another_match_is_not_misscored(db, tmp_path):
    # the weekday workflow discards its cards.db, so a logged id can later belong to a different fixture
    db.execute("INSERT INTO teams (name, country) VALUES ('Chelsea', 'England')")
    db.execute("""INSERT INTO matches (season_id, kickoff_utc, home_team_id, away_team_id, status)
                  VALUES (1, '2026-10-20T19:00:00Z', 3, 1, 'scheduled')""")   # match 2: Chelsea v Arsenal
    finish(db, home=(3, 1, 0), away=(0, 0, 1))
    db.close()
    log = tmp_path / "p.csv"
    with open(log, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerow(dict(logged_at="2026-10-09T08:00:00Z", match_id=2, external_id="", league="EPL",
                        kickoff_utc="2026-10-10T11:30:00Z", home="Arsenal", away="Leeds", model_version="glm-v4",
                        book="Sky Bet", w_yellow=1.0, w_second_yellow=1.0, w_straight_red=1.0, mu_total=5.0,
                        pmf=" ".join(["0.1"] * 10)))
    a = score(str(tmp_path / "t.db"), str(log))["glm-v4"]
    assert a["n"] == 1 and a["y"] == 5.0          # scored against Arsenal v Leeds (match 1), not match 2
