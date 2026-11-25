"""Phone alert of the free Saturday run (agent/free_summary.py)."""
import json
import subprocess
import sys

from conftest import ROOT


def sheet(tmp_path):
    line = lambda L, p: dict(line=L, p_over=p, p_push=0.0, min_under=9.9)
    m = lambda ko, fx, lg: dict(kickoff_utc=ko, fixture=fx, league=lg, provisional=False, thin_ref=False,
                                projection=dict(cards=4.2), lines=[line(3.5, 0.6), line(4.5, 0.4)])
    path = tmp_path / "sheet.json"
    path.write_text(json.dumps(dict(matches=[m("2026-11-27T19:30:00Z", "Friday v Night", "BUNDES"),
                                             m("2026-11-28T15:00:00Z", "Everton v Liverpool", "EPL"),
                                             m("2026-11-28T17:00:00Z", "Milan v Inter", "SERIEA")])))
    return path


def run(*args):
    return subprocess.run([sys.executable, str(ROOT / "agent" / "free_summary.py"), *args],
                          capture_output=True, text=True, check=True).stdout


def test_friday_night_run_lists_saturdays_matches(tmp_path):
    out = run(str(sheet(tmp_path)), "--day", "2026-11-28")
    assert "2 matches Saturday 28 Nov" in out
    assert "Everton v Liverpool" in out and "Friday v Night" not in out
    assert "o3.5>=1.80" in out and "u3.5" not in out          # no unders: they aren't backed
    assert "Milan v Inter (4.2) SERIE-A-CAUTION" in out


def test_without_a_day_it_is_still_today(tmp_path):
    out = run(str(sheet(tmp_path)))
    assert "matches today" in out or "No matches priced for today" in out
