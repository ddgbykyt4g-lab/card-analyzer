#!/usr/bin/env python3
"""Phone-alert text for the free (no-Claude) Saturday run: today's matches from price_sheet --json.

Overs use an 8% EV bar (the model leans slightly towards overs), unders the sheet's own minimum.
Usage: python agent/free_summary.py /tmp/sheet.json [settle report file]
"""
import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
OVER_EV = 0.08
MAIN_LINES = (3.5, 4.5)


def main() -> int:
    sheet = json.load(open(sys.argv[1]))
    today = datetime.now(UK).date()
    out = []
    for m in sheet["matches"]:
        ko = datetime.strptime(m["kickoff_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(UK)
        if ko.date() != today or m["projection"] is None:
            continue
        flags = (" PROV" if m["provisional"] else "") + (" THIN-REF" if m.get("thin_ref") else "")
        cells = []
        for r in m["lines"]:
            if r["line"] in MAIN_LINES and r["p_over"] > 0:
                over_min = (1 + OVER_EV - r["p_push"]) / r["p_over"]
                cells.append(f"o{r['line']:g}>={over_min:.2f} u{r['line']:g}>={r['min_under']:.2f}")
        out.append(f"{ko:%H:%M} {m['fixture']} ({m['projection']['cards']:.1f}){flags}: " + " ".join(cells))
    head = f"{len(out)} matches today (projected cards; min odds, overs at +{OVER_EV:.0%} EV)."
    body = [head] + out if out else ["No matches priced for today."]
    if len(sys.argv) > 2:
        lines = [l.strip() for l in open(sys.argv[2]) if l.strip().startswith("Your bets")]
        body += lines[:1]
    body.append("Full sheet in reports/. Nothing placed; team news not checked.")
    print("\n".join(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
