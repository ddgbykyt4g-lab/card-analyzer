#!/usr/bin/env python3
"""Phone-alert text for the free (no-Claude) Saturday run: today's matches from price_sheet --json.

Overs use an 8% EV bar (the model leans slightly towards overs); unders aren't listed (not backed while the
model runs low). Serie A matches are flagged: their overs must clear the bar with the projection 0.3 cards lower
(check_prices.py applies it). DAY is the date the run is for: a Friday-night run lists Saturday's matches.
Usage: python agent/free_summary.py /tmp/sheet.json [settle report file] [--day YYYY-MM-DD]
"""
import json
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
OVER_EV = 0.08
MAIN_LINES = (3.5, 4.5)


def main() -> int:
    args = sys.argv[1:]
    day = None
    if "--day" in args:
        i = args.index("--day")
        day = date.fromisoformat(args[i + 1])
        del args[i:i + 2]
    sheet = json.load(open(args[0]))
    today = day or datetime.now(UK).date()
    out = []
    for m in sheet["matches"]:
        ko = datetime.strptime(m["kickoff_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(UK)
        if ko.date() != today or m["projection"] is None:
            continue
        flags = (" PROV" if m["provisional"] else "") + (" THIN-REF" if m.get("thin_ref") else "") \
            + (" SERIE-A-CAUTION" if m.get("league") == "SERIEA" else "")
        cells = []
        for r in m["lines"]:
            if r["line"] in MAIN_LINES and r["p_over"] > 0:
                over_min = (1 + OVER_EV - r["p_push"]) / r["p_over"]
                cells.append(f"o{r['line']:g}>={over_min:.2f}")
        out.append(f"{ko:%H:%M} {m['fixture']} ({m['projection']['cards']:.1f}){flags}: " + " ".join(cells))
    when = "today" if today == datetime.now(UK).date() else f"{today:%A %d %b}"
    head = f"{len(out)} matches {when} (projected cards; min odds, overs at +{OVER_EV:.0%} EV)."
    body = [head] + out if out else [f"No matches priced for {when}."]
    if len(args) > 1:
        lines = [l.strip() for l in open(args[1]) if l.strip().startswith("Your bets")]
        body += lines[:1]
    body.append("Full sheet in reports/. Nothing placed; team news not checked.")
    print("\n".join(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
