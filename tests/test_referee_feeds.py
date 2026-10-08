from datetime import datetime, timezone

import collect_cards as cc
from conftest import ROOT  # noqa: F401  (puts the repo on sys.path)

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
KO = datetime(2026, 10, 10, 11, 30, tzinfo=timezone.utc)


def test_scottish_premiership_competitions_only():
    comps = [{"competitionId": 1, "competitionName": "William Hill Premiership 2026/27 Regular Season"},
             {"competitionId": 2, "competitionName": "West of Scotland Football Development League Premiership 26/27"},
             {"competitionId": 3, "competitionName": "William Hill Championship 2026/27"},
             {"competitionId": 4, "competitionName": "William Hill Premiership 2025/26 Regular Season"},
             {"competitionId": 5, "competitionName": "William Hill Premiership 2026/27 Play-Off"},
             {"competitionId": 6, "competitionName": "William Hill Premiership 2026/27 Championship Section"}]
    assert cc.sfa_competition_ids(comps, "2026-27") == [1, 6]


def test_sfa_rows_take_the_confirmed_referee():
    results = [
        {"homeTeamName": "Rangers FC", "awayTeamName": "Kilmarnock F.C.", "matchDateUtc": "2026-10-10T14:00:00+00:00",
         "officials": [{"personName": "Jo Bloggs", "role": "AR1", "roleStatus": "CONFIRMED"},
                       {"personName": "Matthew MacDermid", "role": "Referee", "roleStatus": "CONFIRMED"}]},
        {"homeTeamName": "Celtic F.C.", "awayTeamName": "Hearts", "matchDateUtc": "2026-10-17T14:00:00+00:00",
         "officials": []},
        {"homeTeamName": "Dundee F.C.", "awayTeamName": "Rangers FC", "matchDateUtc": "2026-10-17T19:00:00+00:00",
         "officials": [{"personName": "Not Yet", "role": "Referee", "roleStatus": "PENDING"}]}]
    assert cc.sfa_rows(results) == [("Rangers FC", "Kilmarnock F.C.",
                                     datetime(2026, 10, 10, 14, tzinfo=timezone.utc), "Matthew MacDermid")]


def test_pl_referee():
    officials = {"matchOfficials": [{"official": {"name": "Richard West"}, "type": "Assistant Referee#1"},
                                    {"official": {"name": "Paul Tierney"}, "type": "Referee"}]}
    assert cc.pl_referee(officials) == "Paul Tierney"
    assert cc.pl_referee({"matchOfficials": []}) is None
    assert cc.pl_referee(None) is None


def test_assign_referee_to_upcoming_fixture(db, tmp_path):
    rid = db.execute("INSERT INTO referees (name, country) VALUES ('P Tierney', 'England')").lastrowid
    db.commit()
    db.close()
    d = cc.DB(str(tmp_path / "t.db"), None)
    rows = [("Arsenal", "Leeds United", KO, "Paul Tierney"),                       # matches, existing referee
            ("Arsenal", "Leeds United", datetime(2026, 11, 1, tzinfo=timezone.utc), "Someone Else")]  # past horizon
    assert cc.assign_referees(d, "EPL", rows, "premierleague.com", NOW, 10) == 1
    assert d.one("SELECT referee_id FROM matches WHERE match_id = 1") == rid
    assert d.one("SELECT COUNT(*) FROM referees") == 1  # "Paul Tierney" reused "P Tierney", no new referee


def test_club_suffix_is_dropped_before_matching(db, tmp_path):
    db.close()
    d = cc.DB(str(tmp_path / "t.db"), None)
    assert cc.assign_referees(d, "EPL", [("Arsenal F.C.", "Leeds United FC", KO, "Paul Tierney")],
                              "scottishfa.co.uk", NOW, 10) == 1


def test_health_flags_epl_match_without_referee(db, tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "agent"))
    from health import problems
    db.close()
    path = str(tmp_path / "t.db")
    day_before = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
    assert any("no referee" in p for p in problems(path, None, day_before))    # kickoff tomorrow, no referee
    assert not any("no referee" in p for p in problems(path, None, datetime(2026, 10, 1, tzinfo=timezone.utc)))
