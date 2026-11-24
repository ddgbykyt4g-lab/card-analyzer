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


def test_health_flags_match_without_referee(db, tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "agent"))
    from health import problems
    db.close()
    path = str(tmp_path / "t.db")
    day_before = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
    assert any("no referee" in p for p in problems(path, None, day_before))    # kickoff tomorrow, no referee
    assert not any("no referee" in p for p in problems(path, None, datetime(2026, 10, 1, tzinfo=timezone.utc)))


DFB_PAGE = """
<div class="c-MatchTable"><h6 class="dfb-Headline c-MatchTable-title">
  Bundesliga
</h6><div class="c-MatchTable-subtitle">Spieltag 5</div>
<div id="match_1" class="c-MatchTable-row">
  <p class="dfb-Paragraph">Samstag, 10.10.2026 15:30 Uhr</p>
  <div class="c-MatchTable-team--home">1. FSV Mainz 05 <img src="x.jpg" alt="1. FSV Mainz 05"></div>
  <div class="c-MatchTable-score"> - : - </div>
  <div class="c-MatchTable-team--away"><img src="y.jpg" alt="Bayer Leverkusen"> Bayer Leverkusen</div>
  <a href="#">Dr. Max Burda (SR)</a><a href="#">Sascha Thielert (SR-A. 1)</a>
</div></div>
<div class="c-MatchTable"><h6 class="dfb-Headline c-MatchTable-title">2. Bundesliga</h6>
<div id="match_2" class="c-MatchTable-row">
  <p>Sonntag, 11.10.2026 13:30 Uhr</p><div>FC St. Pauli</div><div> - : - </div><div>Karlsruher SC</div>
  <a href="#">Lukas Benen (SR)</a>
</div></div>"""


def test_dfb_schedule_reads_bundesliga_only():
    assert cc.parse_dfb_schedule(DFB_PAGE) == [
        ("1. FSV Mainz 05", "Bayer Leverkusen", datetime(2026, 10, 10, 13, 30, tzinfo=timezone.utc), "Max Burda")]


def test_league_feed_referee_fields():
    assert cc.seriea_referee({"referees": [{"role": "Assistant", "shortName": "A B"},
                                           {"role": "Referee", "shortName": "Michael Fabbri"}]}) == "Michael Fabbri"
    assert cc.seriea_referee({"referees": []}) is None
    assert cc.ligue1_referee({"officials": [{"name": "Christophe MOUYSSET", "type": 2},
                                            {"name": "Jérémy STINAT", "type": 1}]}) == "Jérémy Stinat"
    assert cc.ligue1_referee({"officials": []}) is None
    assert cc.laliga_referee({"persons_role": [{"person": {"name": "Jon Ander González Esteban"}, "role": {"id": 5}}]}) \
        == "Jon Ander González Esteban"
    assert cc.laliga_referee({"persons_role": None}) is None


def test_referee_names_match_without_accents_or_capitals(db, tmp_path):
    rid = db.execute("INSERT INTO referees (name, country) VALUES ('Jeremy Stinat', 'France')").lastrowid
    db.commit()
    db.close()
    d = cc.DB(str(tmp_path / "t.db"), None)
    assert d.referee_id("Jérémy Stinat", "France", "ligue1.fr") == rid
    assert d.referee_id("Éric Wattellier", "France", "ligue1.fr") != rid  # someone else: new record


def test_fixture_found_by_kickoff_and_one_team_learns_the_alias(db, tmp_path):
    db.close()
    d = cc.DB(str(tmp_path / "t.db"), None)
    rows = [("Arsenal", "LUFC", KO, "Paul Tierney")]   # away spelling unknown
    assert cc.assign_referees(d, "EPL", rows, "example.com", NOW, 10) == 1
    assert d.alias_lookup("example.com", "team", "LUFC") == 2
    late = [("Arsenal", "Somebody Else", datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc), "X Y")]
    assert cc.assign_referees(d, "EPL", late, "example.com", NOW, 10) == 0   # kickoff doesn't match: no guess


def test_fixtures_csv_lagging_a_moved_fixture_adds_no_second_row(db, tmp_path, monkeypatch):
    import asyncio
    db.execute("UPDATE matches SET external_id = 'espn-fx:777', kickoff_utc = '2026-10-11T15:30:00Z'")  # ESPN moved it
    db.commit()
    db.close()
    d = cc.DB(str(tmp_path / "t.db"), None)
    csv_text = "Div,Date,Time,HomeTeam,AwayTeam,Referee,AvgH,AvgD,AvgA\nE0,10/10/2026,12:30,Arsenal,Leeds,,1.5,4.2,6.5\n"

    async def fake_fetch(ctx, url, retries=3):
        return csv_text
    monkeypatch.setattr(cc, "fetch_text", fake_fetch)
    asyncio.run(cc.load_upcoming(None, d, ["EPL"], "2026-27"))
    assert d.one("SELECT COUNT(*) FROM matches") == 1
    assert d.one("SELECT kickoff_utc FROM matches WHERE match_id = 1") == "2026-10-11T15:30:00Z"  # ESPN's date kept


def test_espn_moneyline_handles_null_odds():
    """ESPN sends `"odds": [null]` for some leagues; the fixtures loader must skip prices, not crash."""
    from collect_cards import espn_moneyline
    for comp in ({}, {"odds": None}, {"odds": []}, {"odds": [None]}, {"odds": [{"moneyline": None}]}):
        assert espn_moneyline(comp) == ([None, None, None], "espn")


def test_espn_moneyline_reads_close_then_open():
    from collect_cards import espn_moneyline
    comp = {"odds": [{"provider": {"name": "DraftKings"}, "moneyline": {
        "home": {"close": {"odds": "+150"}}, "draw": {"open": {"odds": "+220"}}, "away": {"close": {"odds": "-120"}}}}]}
    dec, provider = espn_moneyline(comp)
    assert provider == "draftkings"
    assert [round(x, 3) for x in dec] == [2.5, 3.2, round(1 + 100 / 120, 3)]


def test_health_flags_finished_matches_left_without_referee(db, tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "agent"))
    from health import MISSING_REFS, problems
    for d in range(MISSING_REFS):   # recent finished matches, none with a referee
        db.execute("""INSERT INTO matches (season_id, kickoff_utc, home_team_id, away_team_id, status)
                      VALUES (1, ?, 1, 2, 'finished')""", (f"2026-10-0{d + 1}T15:00:00Z",))
    db.commit()
    db.close()
    path = str(tmp_path / "t.db")
    now = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
    assert any("finished in the last 14 days have no referee" in p for p in problems(path, None, now))
    later = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)   # outside the 14 days
    assert not any("finished in the last 14 days" in p for p in problems(path, None, later))
