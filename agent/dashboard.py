#!/usr/bin/env python3
"""Phone dashboard: writes a Markdown page (reports/latest.md) that the GitHub app renders.

Today's and tomorrow's matches from price_sheet --json with the lowest odds worth taking (overs at
+8% EV, unders at the sheet's +5%), plus your pending bets and track record from cards.db.
Usage: python agent/dashboard.py /tmp/sheet.json [cards.db] [health.txt] > reports/latest.md
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
ISO = "%Y-%m-%dT%H:%M:%SZ"
OVER_EV = 0.08
LINES = (2.5, 3.5, 4.5, 5.5)
TEAM_LINES = (0.5, 1.5, 2.5)
REPO = "https://github.com/ddgbykyt4g-lab/card-analyzer"


def uk(iso: str) -> datetime:
    return datetime.strptime(iso, ISO).replace(tzinfo=timezone.utc).astimezone(UK)


def money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}£{abs(x):.2f}"


def odds(x: float | None) -> str:
    return f"{x:.2f}" if x and x < 100 else "–"


def match_block(m: dict) -> list[str]:
    ko = uk(m["kickoff_utc"])
    out = [f"### {ko:%H:%M} {m['fixture']} · {m['league']}"]
    if m["referee"]:
        ref = f"Ref: {m['referee']} ({m['ref_matches']} past matches)"
        out.append(ref + (" ⚠️ thin referee history" if m.get("thin_ref") else ""))
    else:
        out.append("⚠️ No referee yet: league average used")
    p = m["projection"]
    if p is None:
        return out + ["", f"Not priced: {m.get('skipped', 'not enough history')}", ""]
    home, away = m["fixture"].split(" v ", 1)
    split = f" ({home} {p['home_mu']:.1f}, {away} {p['away_mu']:.1f})" if "home_mu" in p else ""
    out += [f"Projected cards **{p['cards']:.1f}**{split}", "", "| Cards | Over at least | Under at least |",
            "|---|---|---|"]
    for r in m["lines"]:
        if r["line"] in LINES:
            over_min = (1 + OVER_EV - r["p_push"]) / r["p_over"] if r["p_over"] > 1e-9 else None
            out.append(f"| {r['line']:g} | {odds(over_min)} | {odds(r['min_under'])} |")
    for side, name in (("home", home), ("away", away)):
        rows = [t for t in m.get("team_lines", {}).get(side, []) if t["line"] in TEAM_LINES]
        if rows:
            cells = " · ".join(f"o{t['line']:g} {odds((1 + OVER_EV) / t['p_over'] if t['p_over'] > 1e-9 else None)}"
                               for t in rows)
            out.append(f"\n{name} cards, over at least: {cells}")
    return out + [""]


def bets_section(db: str) -> list[str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("""
        SELECT vb.bet_id, m.kickoff_utc, h.name || ' v ' || a.name, mk.code, vb.selection, o.line, vb.taken_odds,
               vb.stake, vb.result, vb.profit
        FROM value_bets vb JOIN odds_snapshots o USING (snapshot_id) JOIN markets mk ON mk.market_id = o.market_id
        JOIN matches m ON m.match_id = o.match_id
        JOIN teams h ON h.team_id = m.home_team_id JOIN teams a ON a.team_id = m.away_team_id
        WHERE vb.notes LIKE 'manual entry%' ORDER BY m.kickoff_utc DESC""").fetchall()
    con.close()
    market = {"TOTAL_CARDS": "match", "HOME_CARDS": "home team", "AWAY_CARDS": "away team",
              "EACH_TEAM_CARDS": "each team"}
    settled = [r for r in rows if r[8] not in ("pending", "void")]
    pending = [r for r in rows if r[8] == "pending"]
    staked = sum(r[7] for r in settled)
    profit = sum(r[9] or 0 for r in settled)
    won = sum(r[8] == "won" for r in settled)
    out = ["## Your bets", ""]
    if settled:
        roi = f"{profit / staked:+.0%}" if staked else "–"
        out.append(f"**{len(settled)} settled: {won} won, staked £{staked:.2f}, profit {money(profit)} ({roi})**")
        out.append("A few hundred settled bets are needed before this means much.")
    else:
        out.append("No settled bets yet.")
    out.append("")
    if pending:
        out += ["Pending:", ""]
        out += [f"- {uk(r[1]):%a %d %b} {r[2]}: {market.get(r[3], r[3])} {r[4]} {r[5]:g} @ {r[6]:.2f}, £{r[7]:g}"
                for r in pending]
        out.append("")
    recent = settled[:5]
    if recent:
        out += ["Latest results:", ""]
        out += [f"- {uk(r[1]):%d %b} {r[2]}: {market.get(r[3], r[3])} {r[4]} {r[5]:g} @ {r[6]:.2f} "
                f"→ **{r[8]}** ({money(r[9] or 0)})" for r in recent]
        out.append("")
    return out


def main() -> int:
    sheet = json.load(open(sys.argv[1]))
    db = sys.argv[2] if len(sys.argv) > 2 else "cards.db"
    made = uk(sheet["generated_at"])
    book = sheet.get("book", "Sky Bet")
    out = [f"# Card prices · {made:%a %d %b}", "",
           f"Updated {made:%H:%M} UK · priced for {book} · overs need +{OVER_EV:.0%} EV, unders +5%. "
           "Team news not checked.", "",
           f"**[➕ Log a bet]({REPO}/actions/workflows/log-bet.yml)** (tap *Run workflow*)", ""]
    health = open(sys.argv[3]).read().strip() if len(sys.argv) > 3 else "OK"
    if health != "OK":
        out += ["> ⚠️ **Data problem: check before betting**", ">"] + [f"> - {l}" for l in health.splitlines()] + [""]
    days: dict = {}
    for m in sheet["matches"]:
        days.setdefault(uk(m["kickoff_utc"]).date(), []).append(m)
    if not days:
        out += ["No matches in the next two days.", ""]
    for day, ms in sorted(days.items()):
        label = "Today" if day == made.date() else f"{day:%A %d %b}"
        out += [f"## {label}", ""]
        for m in ms:
            out += match_block(m)
    out += bets_section(db)
    out += ["---", "Odds shown are the **lowest worth taking**: take a price at or above them, skip anything "
            "below. Stake £10 flat. Nothing here places bets."]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
