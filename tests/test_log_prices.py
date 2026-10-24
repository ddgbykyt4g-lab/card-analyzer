import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from conftest import ROOT  # noqa: F401,E402  (puts the repo on sys.path)
from log_prices import parse_file, parse_row  # noqa: E402


def test_parse_row_over_under_and_or_more():
    assert parse_row("3.5 1/8 4/1") == (3.5, 1.125, 5.0)
    assert parse_row("4+ 2/5") == (3.5, 1.4, None)
    assert parse_row("5.5 - 8/15") == (5.5, None, 1 + 8 / 15)
    assert parse_row("2.5 2.38") == (2.5, 2.38, None)


def test_parse_file_converts_uk_time_and_ignores_comments():
    blocks = parse_file("# note\n@ 2026-10-24 10:28 | Valencia v Villarreal | total  # derby\n3.5 1/8 4/1\n"
                        "@ 2026-10-25 14:00 | Hull v Brentford | away\n1+ 1/20\n")
    assert [b["market"] for b in blocks] == ["total", "away"]
    assert blocks[0]["when"].strftime("%Y-%m-%dT%H:%MZ") == "2026-10-24T09:28Z"  # BST
    assert blocks[1]["when"].strftime("%Y-%m-%dT%H:%MZ") == "2026-10-25T14:00Z"  # GMT after the clocks change
    assert blocks[1]["rows"] == [(0.5, 1.05, None)]
