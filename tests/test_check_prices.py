"""The price checker's verdicts, the in-play card count, and the scoring breakdown."""
import sys

import pytest

from conftest import ROOT, finish

sys.path.insert(0, str(ROOT / "agent"))

from check_prices import verdicts
from espn_match import card_lines, lineup_lines
from score_projections import report, score


def rows(*lines):
    """(line, model p, over odds, under odds) on half lines."""
    return [dict(line=line, p_over=p, p_push=0.0, over=over, under=under) for line, p, over, under in lines]


def verdict(out, line):
    return next(r["verdict"] for r in out if r["line"] == line)


def test_match_total_takes_the_shortest_line_that_clears_the_bar():
    """Leeds v Tottenham, 7 Nov 2026 (Sky Bet 09:45)."""
    out = verdicts(rows((3.5, 0.79, 4 / 3, 2.9), (4.5, 0.63, 11 / 6, 11 / 6), (5.5, 0.46, 2.8, 15 / 11),
                        (6.5, 0.30, 5.0, 1.125)))
    assert verdict(out, 3.5) == "no"                       # +5%: short of +8%
    assert verdict(out, 4.5) == "BET"
    assert verdict(out, 5.5).startswith("longer line")     # same view, more variance
    assert verdict(out, 6.5).startswith("pass: tail bet")
    bet = next(r for r in out if r["line"] == 4.5)
    assert bet["value_over"] == pytest.approx(0.155)
    assert bet["min_over"] == pytest.approx(1.08 / 0.63)


def test_value_only_in_the_tail_is_no_bet():
    """Brest v Lyon, 8 Nov 2026: 4+ break-even, 5+ +6%, 6+ +11% at 28%."""
    out = verdicts(rows((3.5, 0.64, 11 / 7, None), (4.5, 0.44, 2.375, None), (5.5, 0.28, 4.0, None)))
    assert [r["verdict"] for r in out] == ["no", "no", "pass: tail bet (model under 40%)"]


def test_long_team_lines():
    """Espanyol v La Coruna, 7 Nov 2026: 3+ at 19/20 is the bet; a big edge on 3+ would be a warning."""
    espanyol = rows((1.5, 0.79, 11 / 9, 3.7), (2.5, 0.57, 1.95, 19 / 11), (3.5, 0.34, 3.7, 11 / 9))
    out = verdicts(espanyol, team_line=True)
    assert [r["verdict"] for r in out] == ["no", "BET", "pass: tail bet (model under 40%)"]
    out = verdicts(rows((1.5, 0.80, 1.2, None), (2.5, 0.57, 2.2, None)), team_line=True)
    assert verdict(out, 2.5).startswith("warning: edge above 20%")
    assert verdict(verdicts(rows((2.5, 0.57, 2.2, None))), 2.5) == "BET"  # the same price on a match total
    out = verdicts(espanyol, team_line=True, underdog=True)
    assert verdict(out, 2.5) == "pass: heavy underdog, 2+ only"


def test_unders_are_shown_but_never_the_bet():
    """Real Sociedad v Vallecano, 8 Nov 2026: under 3.5 at 11/5 shows +9%."""
    out = verdicts(rows((3.5, 0.66, 9 / 7, 3.2), (4.5, 0.48, 1.7, 2.0)))
    assert verdict(out, 3.5) == "no; under shows +9%, not backed while the model runs low"
    assert verdict(out, 4.5) == "no"                       # +4%: short of the +5% unders need
    assert not any(r["verdict"].startswith("BET") for r in out)


def test_whole_line_counts_the_push():
    out = verdicts([dict(line=4.0, p_over=0.45, p_push=0.20, over=2.0, under=None)])
    assert out[0]["value_over"] == pytest.approx(0.45 * 2.0 + 0.20 - 1)
    assert out[0]["min_over"] == pytest.approx((1.08 - 0.20) / 0.45)
    assert verdicts([dict(line=4.5, p_over=0.5, p_push=0.0, over=None, under=None)])[0]["verdict"] == "no over price"


def test_in_play_count_leaves_out_the_manager():
    comp = dict(competitors=[dict(homeAway="home", team=dict(id="357")), dict(homeAway="away", team=dict(id="367"))],
                details=[dict(team=dict(id="367"), yellowCard=True, clock=dict(displayValue="15'"),
                              athletesInvolved=[dict(displayName="Micky van de Ven")]),
                         dict(team=dict(id="357"), yellowCard=True, clock=dict(displayValue="22'"),
                              athletesInvolved=[dict(displayName="Sean Longstaff")]),
                         dict(team=dict(id="367"), yellowCard=True, clock=dict(displayValue="51'"),
                              athletesInvolved=[dict(displayName="Pedro Porro")]),
                         dict(team=dict(id="367"), yellowCard=True, clock=dict(displayValue="51'"))])
    lines = card_lines(comp, dict(home="Leeds", away="Tottenham"))
    assert lines[0] == "Leeds: 1 player cards (22' Sean Longstaff)"
    assert lines[1] == "Tottenham: 2 player cards (15' Micky van de Ven; 51' Pedro Porro)"
    assert lines[2] == "Tottenham staff card, doesn't count: 51'"
    assert lines[-1] == "Total that counts: 3"


def test_lineups():
    assert lineup_lines({}) == ["Line-ups aren't out yet (usually an hour before kickoff)."]
    roster = [dict(starter=True, athlete=dict(displayName="Granit Xhaka")),
              dict(starter=False, athlete=dict(displayName="Kevin Danso"))]
    assert lineup_lines(dict(rosters=[dict(team=dict(displayName="Sunderland"), formation="4-2-3-1", roster=roster)])) \
        == ["Sunderland (4-2-3-1): Granit Xhaka", "  bench: Kevin Danso"]


def test_score_report_breaks_down_by_league_and_projected_level(db, tmp_path):
    finish(db, home=(3, 0, 0), away=(2, 0, 0))
    log = tmp_path / "projections.csv"
    pmf = " ".join(["0.05"] * 20)
    log.write_text("logged_at,match_id,external_id,league,kickoff_utc,home,away,model_version,w_yellow,"
                   "w_second_yellow,w_straight_red,mu_total,pmf\n"
                   f"2026-10-09T09:00:00Z,1,,EPL,2026-10-10T11:30:00Z,Arsenal,Leeds,glm-v4.1,1,1,1,4.2,{pmf}\n")
    acc = score(str(tmp_path / "t.db"), str(log))
    assert acc["league EPL"]["y"] == 5 and acc["level 4-4.5"]["mu"] == pytest.approx(4.2)
    text = report(acc)
    assert "By league (matches, projected, actual, gap):" in text
    assert "  EPL                       1  4.20  5.00  +0.80" in text
    assert "  4-4.5                     1  4.20  5.00  +0.80" in text
