"""Derby pairs for post-freeze test 6a (agent/POST_FREEZE.md, review/test6a_derby.py).

Fixed on 22 Nov 2026 from general football knowledge (city, regional and traditional rivalries), before any card
data was looked at. Don't edit it on results: a change is a new test. Names are as stored in teams.name.
"""
import sqlite3

DERBIES = {
    "EPL": [("Arsenal", "Tottenham"), ("Liverpool", "Everton"), ("Man United", "Man City"), ("Liverpool", "Man United"),
            ("Chelsea", "Tottenham"), ("Chelsea", "Arsenal"), ("West Ham", "Tottenham"), ("Newcastle", "Sunderland"),
            ("Crystal Palace", "Brighton"), ("Leeds", "Man United")],
    "LALIGA": [("Real Madrid", "Ath Madrid"), ("Real Madrid", "Barcelona"), ("Barcelona", "Espanol"),
               ("Sevilla", "Betis"), ("Ath Bilbao", "Sociedad"), ("Valencia", "Villarreal"), ("Valencia", "Levante"),
               ("Celta", "La Coruna")],
    "SERIEA": [("Inter", "Milan"), ("Roma", "Lazio"), ("Juventus", "Torino"), ("Genoa", "Sampdoria"),
               ("Juventus", "Inter"), ("Napoli", "Roma")],
    "BUNDES": [("Dortmund", "Schalke 04"), ("Dortmund", "Bayern Munich"), ("Hamburg", "Werder Bremen"),
               ("FC Koln", "M'gladbach"), ("FC Koln", "Leverkusen"), ("Hamburg", "St Pauli"),
               ("Union Berlin", "Hertha"), ("Ein Frankfurt", "Darmstadt")],
    "LIGUE1": [("Paris SG", "Marseille"), ("Lyon", "St Etienne"), ("Nice", "Monaco"), ("Lens", "Lille"),
               ("Rennes", "Nantes"), ("Rennes", "Brest"), ("Rennes", "Lorient"), ("Brest", "Lorient"),
               ("Marseille", "Lyon"), ("Paris SG", "Paris FC")],
    "SPL": [("Celtic", "Rangers"), ("Hearts", "Hibernian"), ("Dundee", "Dundee United"), ("Aberdeen", "Rangers")],
}

PAIRS = {(lg, frozenset(p)) for lg, ps in DERBIES.items() for p in ps}


def is_derby(league: str, home: str, away: str) -> bool:
    return (league, frozenset((home, away))) in PAIRS


def derby_matches(con: sqlite3.Connection) -> set[int]:
    """match_ids of every derby in the database (either venue)."""
    return {m for m, lg, h, a in con.execute(
        """SELECT m.match_id, l.code, th.name, ta.name FROM matches m JOIN seasons s USING (season_id)
           JOIN leagues l USING (league_id) JOIN teams th ON th.team_id = m.home_team_id
           JOIN teams ta ON ta.team_id = m.away_team_id""") if is_derby(lg, h, a)}
