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
<tr><th class="national-referees-table--sheet">11.1</th>
<td class="national-referees-table--date">22/11/2026</td><td class="national-referees-table--time">17:30</td>
<td class="national-referees-table--cort">ΟΛΥΜΠΙΑΚΟ ΣΤΑΔΙΟ</td><td class="national-referees-table--hometeam">ΠΑΕ ΠΑΝΑΘΗΝΑΙΚΟΣ</td>
<td class="national-referees-table--guest">ΠΑΕ ΟΦΗ 1925</td>
<td class="national-referees-table--referee">ΑΝΑΣΤΑΣΙΟΣ ΠΑΠΑΠΕΤΡΟΥ</td><td class="national-referees-table--association">ΑΘΗΝΩΝ</td>
<td class="national-referees-table--referee">ΤΡΥΦΩΝΑΣ ΠΕΤΡΟΠΟΥΛΟΣ</td></tr>
<tr><th class="national-referees-table--sheet">11.1</th><td class="national-referees-table--date">23/11/2026</td>
<td class="national-referees-table--time">17:00</td><td class="national-referees-table--hometeam">ASTERAS AKTOR FC</td>
<td class="national-referees-table--guest">ΠΑΕ  ΑΡΗΣ</td><td class="national-referees-table--referee"></td></tr></table>"""


def test_round_table_rows_give_teams_kickoff_in_utc_and_referee():
    rows = cc.epo_rows(ROW)
    assert rows == [("ΠΑΕ ΠΑΝΑΘΗΝΑΙΚΟΣ", "ΠΑΕ ΟΦΗ 1925", datetime(2026, 11, 22, 15, 30, tzinfo=timezone.utc),
                     "ΑΝΑΣΤΑΣΙΟΣ ΠΑΠΑΠΕΤΡΟΥ")]   # Athens is UTC+2 in November; the first referee cell (not the assistant's); a row with no referee is skipped
