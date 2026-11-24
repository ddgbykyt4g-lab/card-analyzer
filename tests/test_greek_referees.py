"""epo.gr referee appointments for the Greek Super League (shadow league GREEK)."""
from datetime import datetime, timezone

import collect_cards as cc


def test_greek_names_transliterate_as_espn_spells_them():
    assert cc.greek_to_latin("ΑΝΑΣΤΑΣΙΟΣ ΠΑΠΑΠΕΤΡΟΥ") == "Anastasios Papapetrou"
    assert cc.greek_to_latin("ΣΤΑΥΡΟΣ ΤΣΙΜΕΝΤΕΡΙΔΗΣ") == "Stavros Tsimenteridis"
    assert cc.greek_to_latin("ΜΠΟΥΞΜΠΑΟΥΜ") == "Bouxmpaoum"           # word-initial ΜΠ is B
    assert cc.greek_to_latin("ASTERAS AKTOR FC") == "Asteras Aktor Fc"   # Latin passes through


def test_club_names_lose_company_prefixes_and_years():
    assert cc.epo_club("ΠΑΕ ΑΤΡΟΜΗΤΟΣ ΑΘ1923") == "Atromitos"
    assert cc.epo_club("Π.Α.Ε. ΛΕΒΑΔΕΙΑΚΟΣ") == "Levadeiakos"
    assert cc.epo_club("ΠΑΕ Π.Ο.Τ. ΗΡΑΚΛΗΣ") == "Iraklis"
    assert cc.epo_club("ΠΑΕ ΟΦΗ 1925") == "Ofi"


ROW = """<table><tr><th>Αγωνιστική</th></tr>
<tr><td>11.1</td><td>22/11/2026</td><td>17:30</td><td>ΟΛΥΜΠΙΑΚΟ ΣΤΑΔΙΟ</td><td>ΠΑΕ ΠΑΝΑΘΗΝΑΙΚΟΣ</td>
<td>ΠΑΕ ΟΦΗ 1925</td><td>ΑΝΑΣΤΑΣΙΟΣ ΠΑΠΑΠΕΤΡΟΥ</td><td>ΑΘΗΝΩΝ</td></tr>
<tr><td>11.1</td><td>23/11/2026</td><td>17:00</td><td>Χ</td><td>ASTERAS AKTOR FC</td><td>ΠΑΕ  ΑΡΗΣ</td><td></td></tr></table>"""


def test_round_table_rows_give_teams_kickoff_in_utc_and_referee():
    rows = cc.epo_rows(ROW)
    assert rows == [("ΠΑΕ ΠΑΝΑΘΗΝΑΙΚΟΣ", "ΠΑΕ ΟΦΗ 1925", datetime(2026, 11, 22, 15, 30, tzinfo=timezone.utc),
                     "ΑΝΑΣΤΑΣΙΟΣ ΠΑΠΑΠΕΤΡΟΥ")]   # Athens is UTC+2 in November; a row with no referee is skipped
