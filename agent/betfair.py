#!/usr/bin/env python3
"""Read-only Betfair Exchange card-price logger: reference prices and closing line value (CLV).

Never places, cancels or changes a bet. It only reads prices with the free delayed application key.

  log     Logs in, finds Betfair events for our scheduled fixtures in the next --hours, reads every card
          over/under market (match total, home team, away team) and appends best back/lay prices to
          data/exchange/<YYYY-MM>.csv. A row is written when a price changes, and on every run in the
          last 90 minutes before kickoff so each match has a near-kickoff observation. Card-like markets
          are also listed in data/exchange/catalogue.csv (parsed or not) so the parser can be checked.
  import  Loads the CSVs into odds_snapshots as bookmaker "Betfair Exchange" (is_sharp = 1), marks each
          market's last pre-kickoff row in the 90 minutes before kickoff as closing, and fills
          value_bets.closing_odds (fair: back/lay midpoints, normalised over/under) and clv for every
          bet on a matching match, market and half line. Re-running is harmless.

Credentials come from the environment, never from files in the repo:
  BETFAIR_APP_KEY                     delayed application key (free)
  BETFAIR_USERNAME, BETFAIR_PASSWORD  account login
  BETFAIR_CERT, BETFAIR_KEY           optional: PEM text of a self-signed certificate and key uploaded to
                                      the account; uses certificate login (needed if two-step login is on)
Betfair refuses logins from restricted countries (including the US, where most GitHub-hosted runners
are); the logger then stops with a clear message and exit code 2.

Usage: python agent/betfair.py log --db cards.db [--hours 36]
       python agent/betfair.py import --db cards.db
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import ssl
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collect_cards import DB, TeamResolver, norm  # noqa: E402

ISO = "%Y-%m-%dT%H:%M:%SZ"
BOOK = "Betfair Exchange"
SOURCE = "betfair"  # name_aliases source
OUT_DIR = ROOT / "data" / "exchange"
CLOSING_WINDOW = timedelta(minutes=90)
KICKOFF_TOLERANCE = timedelta(minutes=20)
LOGIN_URL = "https://identitysso.betfair.com/api/login"
CERT_LOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"
LOGOUT_URL = "https://identitysso.betfair.com/api/logout"
API_URL = "https://api.betfair.com/exchange/betting/rest/v1.0/"
FOOTBALL = "1"
CATALOGUE_BATCH = 3   # events per listMarketCatalogue call (zero-weight projections, 1000-market cap)
API_INTERVAL = 1.0    # seconds between Betfair calls (its own API, far below its rate limits)
BOOK_BATCH = 10       # markets per listMarketBook call (EX_BEST_OFFERS weighs 5 of the 200 allowed)
NOT_SENIOR = re.compile(r"\b(women|w|u\d\d|ii|b|reserves?|youth)\b", re.I)
CARDISH = re.compile(r"\b(cards?|bookings?)\b", re.I)
NOT_COUNT = re.compile(r"points|player|first|last|1st|2nd|half|red card|handicap|most|to be|shown|corner|"
                       r"time|minute|both teams|each team", re.I)
RUNNER = re.compile(r"^(?P<side>over|under)\s+(?P<line>\d+(?:\.\d+)?)", re.I)
CSV_FIELDS = ["captured_at", "match_id", "kickoff_utc", "bf_event_id", "bf_market_id", "market_name",
              "market_code", "line", "back_over", "back_over_size", "lay_over", "lay_over_size",
              "back_under", "back_under_size", "lay_under", "lay_under_size", "total_matched"]
PRICE_FIELDS = CSV_FIELDS[8:16]


class LoginRefused(SystemExit):
    """Betfair rejected the login (bad credentials, restricted location, two-step login)."""


# ───────────────────────────── Betfair client (read-only) ─────────────────────────────
class Betfair:
    """Only login, logout and the three read operations this logger needs. No betting operations."""
    READ_ONLY = {"listEvents", "listMarketCatalogue", "listMarketBook"}

    def __init__(self, app_key: str, interval: float = API_INTERVAL):
        self.app_key, self.interval, self.token, self._last = app_key, interval, None, 0.0

    def _post(self, url: str, data: bytes, headers: dict, ctx: ssl.SSLContext | None = None) -> dict:
        gap = self._last + self.interval - time.monotonic()
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()
        req = urllib.request.Request(url, data=data, headers={"X-Application": self.app_key,
                                                              "Accept": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"Betfair HTTP {exc.code} on {url.rsplit('/', 2)[-2]}: {body[:300]}") from None

    def login(self, username: str, password: str, cert_pem: str | None = None, key_pem: str | None = None) -> None:
        form = urllib.parse.urlencode({"username": username, "password": password}).encode()
        hdr = {"Content-Type": "application/x-www-form-urlencoded"}
        if cert_pem and key_pem:
            with tempfile.TemporaryDirectory() as tmp:
                crt, key = Path(tmp, "c.crt"), Path(tmp, "c.key")
                crt.write_text(cert_pem), key.write_text(key_pem)
                ctx = ssl.create_default_context()
                ctx.load_cert_chain(crt, key)
                r = self._post(CERT_LOGIN_URL, form, hdr, ctx)
            status, token, error = r.get("loginStatus"), r.get("sessionToken"), r.get("loginStatus")
        else:
            r = self._post(LOGIN_URL, form, hdr)
            status, token, error = r.get("status"), r.get("token"), r.get("error")
        if status != "SUCCESS" or not token:
            raise LoginRefused(login_help(error or status or "unknown error"))
        self.token = token

    def call(self, method: str, params: dict):
        if method not in self.READ_ONLY:
            raise ValueError(f"{method} is not a read-only operation")
        return self._post(API_URL + method + "/", json.dumps(params).encode(),
                          {"Content-Type": "application/json", "X-Authentication": self.token or ""})

    def logout(self) -> None:
        if self.token:
            try:
                self._post(LOGOUT_URL, b"", {"X-Authentication": self.token})
            except Exception:
                pass
            self.token = None


def login_help(error: str) -> str:
    hints = {
        "BETTING_RESTRICTED_LOCATION": "Betfair blocks this machine's country. GitHub's hosted runners are "
            "mostly in the US: run the logger on a UK machine (set the BETFAIR_RUNNER repository variable to "
            "a self-hosted runner label).",
        "SECURITY_RESTRICTED_LOCATION": "Betfair blocks this machine's location; run it from the UK.",
        "INVALID_USERNAME_OR_PASSWORD": "Check the BETFAIR_USERNAME and BETFAIR_PASSWORD secrets.",
        "INVALID_APP_KEY": "Check the BETFAIR_APP_KEY secret (use the delayed key).",
        "PENDING_AUTH": "Two-step login is on: add the BETFAIR_CERT and BETFAIR_KEY secrets for certificate login.",
        "ACTIONS_REQUIRED": "Log in at betfair.com and complete what the site asks for.",
        "CERT_AUTH_REQUIRED": "This account needs certificate login: add BETFAIR_CERT and BETFAIR_KEY.",
    }
    return f"Betfair login refused ({error}). " + hints.get(error, "See the Betfair login error list.")


# ───────────────────────────── Parsing ─────────────────────────────
def parse_card_market(name: str, runners: list[str], home: str, away: str) -> tuple[str, float] | None:
    """(market code, line) for a full-time card-count over/under market, else None.
    Team markets name the team as Betfair spells it in the event name."""
    if not CARDISH.search(name) or NOT_COUNT.search(name) or len(runners) != 2:
        return None
    parsed = [RUNNER.match(r.strip()) for r in runners]
    if not all(parsed) or {m["side"].lower() for m in parsed} != {"over", "under"}:
        return None
    lines = {float(m["line"]) for m in parsed}
    if len(lines) != 1:
        return None
    line = lines.pop()
    key = f" {norm(name)} "
    in_home, in_away = f" {norm(home)} " in key, f" {norm(away)} " in key
    if in_home and in_away:
        return None
    return ("HOME_CARDS" if in_home else "AWAY_CARDS" if in_away else "TOTAL_CARDS"), line


def best(prices: list[dict]) -> tuple[float | None, float | None]:
    return (prices[0]["price"], prices[0]["size"]) if prices else (None, None)


def split_event(name: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+v(?:s)?\.?\s+", name, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else None


# ───────────────────────────── Logging prices ─────────────────────────────
def upcoming(con: sqlite3.Connection, now: datetime, hours: float) -> list[sqlite3.Row]:
    return con.execute("""SELECT match_id, kickoff_utc, home_team_id, away_team_id FROM matches
                          WHERE status = 'scheduled' AND kickoff_utc > ? AND kickoff_utc <= ?
                          ORDER BY kickoff_utc""",
                       (now.strftime(ISO), (now + timedelta(hours=hours)).strftime(ISO))).fetchall()


def match_events(db: DB, fixtures: list[sqlite3.Row], events: list[dict]) -> dict[str, dict]:
    """Betfair event id -> fixture info, for events whose teams and kickoff match one of our fixtures."""
    when = lambda s: datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    resolver = TeamResolver(db, {t for f in fixtures for t in (f["home_team_id"], f["away_team_id"])})
    out = {}
    for e in events:
        ev = e.get("event", e)
        teams = split_event(ev.get("name", ""))
        if not teams or any(NOT_SENIOR.search(t) for t in teams):
            continue
        start = when(ev["openDate"])
        near = [f for f in fixtures if abs(when(f["kickoff_utc"]) - start) <= KICKOFF_TOLERANCE]
        if not near:
            continue
        h, a = resolver.resolve(teams[0], SOURCE, warn=False), resolver.resolve(teams[1], SOURCE, warn=False)
        fx = next((f for f in near if (f["home_team_id"], f["away_team_id"]) == (h, a)), None)
        if fx is None:
            if h or a:
                print(f"Unmatched Betfair event {ev['name']!r}: add the team to name_aliases (source 'betfair')")
            continue
        out[str(ev["id"])] = dict(match_id=fx["match_id"], kickoff_utc=fx["kickoff_utc"], home=teams[0],
                                  away=teams[1], name=ev["name"])
    db.commit()
    return out


def last_rows(path: Path) -> dict[tuple, dict]:
    """Latest logged row per (Betfair market, line)."""
    out: dict[tuple, dict] = {}
    if path.exists():
        with path.open(newline="") as fh:
            for r in csv.DictReader(fh):
                out[(r["bf_market_id"], r["line"])] = r
    return out


def snapshot(client, db: DB, now: datetime, hours: float, out_dir: Path = OUT_DIR) -> dict:
    con = db.con
    con.row_factory = sqlite3.Row
    fixtures = upcoming(con, now, hours)
    stats = dict(fixtures=len(fixtures), events=0, markets=0, rows=0)
    if not fixtures:
        return stats
    events = client.call("listEvents", {"filter": {"eventTypeIds": [FOOTBALL], "marketStartTime": {
        "from": (now - timedelta(hours=1)).strftime(ISO), "to": (now + timedelta(hours=hours + 1)).strftime(ISO)}}})
    matched = match_events(db, fixtures, events)
    stats["events"] = len(matched)
    cards: dict[str, dict] = {}  # Betfair market id -> info
    seen: list[dict] = []
    ids = list(matched)
    for i in range(0, len(ids), CATALOGUE_BATCH):
        cat = client.call("listMarketCatalogue", {"filter": {"eventIds": ids[i:i + CATALOGUE_BATCH]},
                                                  "marketProjection": ["EVENT", "RUNNER_DESCRIPTION"],
                                                  "maxResults": 1000})
        if len(cat) >= 1000:
            print("Warning: listMarketCatalogue hit its 1000-market cap; some card markets may be missing")
        for mk in cat:
            ev = matched.get(str(mk.get("event", {}).get("id")))
            if ev is None or not CARDISH.search(mk.get("marketName", "")):
                continue
            runners = sorted(mk.get("runners", []), key=lambda r: r.get("sortPriority", 0))
            parsed = parse_card_market(mk["marketName"], [r["runnerName"] for r in runners], ev["home"], ev["away"])
            seen.append(dict(event=ev["name"], market_name=mk["marketName"],
                             runners=" | ".join(r["runnerName"] for r in runners),
                             parsed=f"{parsed[0]} {parsed[1]}" if parsed else ""))
            if parsed:
                sel = {RUNNER.match(r["runnerName"])["side"].lower(): r["selectionId"] for r in runners}
                cards[mk["marketId"]] = dict(ev=ev, event_id=mk["event"]["id"], name=mk["marketName"],
                                             code=parsed[0], line=parsed[1], sel=sel)
    stats["markets"] = len(cards)
    record_catalogue(seen, now, out_dir)
    path = out_dir / f"{now:%Y-%m}.csv"
    previous = last_rows(path)
    rows = []
    mids = list(cards)
    for i in range(0, len(mids), BOOK_BATCH):
        books = client.call("listMarketBook", {"marketIds": mids[i:i + BOOK_BATCH], "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"], "exBestOffersOverrides": {"bestPricesDepth": 1}}})
        for bk in books:
            info = cards.get(bk["marketId"])
            if info is None or bk.get("status") != "OPEN" or bk.get("inplay"):
                continue
            by_sel = {r["selectionId"]: r.get("ex", {}) for r in bk.get("runners", [])}
            row = dict(captured_at=now.strftime(ISO), match_id=info["ev"]["match_id"],
                       kickoff_utc=info["ev"]["kickoff_utc"], bf_event_id=info["event_id"], bf_market_id=bk["marketId"],
                       market_name=info["name"], market_code=info["code"], line=f"{info['line']:g}",
                       total_matched=f"{bk.get('totalMatched') or 0:.2f}")
            for side in ("over", "under"):
                ex = by_sel.get(info["sel"].get(side), {})
                (bp, bs), (lp, ls) = best(ex.get("availableToBack", [])), best(ex.get("availableToLay", []))
                row.update({f"back_{side}": bp or "", f"back_{side}_size": bs or "",
                            f"lay_{side}": lp or "", f"lay_{side}_size": ls or ""})
            row = {k: str(v) for k, v in row.items()}
            prev = previous.get((row["bf_market_id"], row["line"]))
            near_ko = now >= parse_iso(row["kickoff_utc"]) - CLOSING_WINDOW
            changed = prev is None or any(prev[f] != row[f] for f in PRICE_FIELDS)
            if changed or near_ko:
                rows.append(row)
    if rows:
        out_dir.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            if new:
                w.writeheader()
            w.writerows(rows)
    stats["rows"] = len(rows)
    return stats


def record_catalogue(seen: list[dict], now: datetime, out_dir: Path) -> None:
    """Every card-like market name/runner pattern once, with how it was parsed."""
    if not seen:
        return
    path = out_dir / "catalogue.csv"
    pattern = lambda s: re.sub(r"\d+(?:\.\d+)?", "#", s)
    known = set()
    if path.exists():
        with path.open(newline="") as fh:
            known = {(pattern(r["market_name"]), pattern(r["runners"])) for r in csv.DictReader(fh)}
    fresh = []
    for s in seen:
        key = (pattern(s["market_name"]), pattern(s["runners"]))
        if key not in known:
            known.add(key)
            fresh.append(dict(first_seen=now.strftime(ISO), **s))
    if fresh:
        out_dir.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["first_seen", "event", "market_name", "runners", "parsed"])
            if new:
                w.writeheader()
            w.writerows(fresh)


# ───────────────────────────── Import, closing prices and CLV ─────────────────────────────
def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, ISO).replace(tzinfo=timezone.utc)


def _f(x: str) -> float | None:
    return float(x) if x not in ("", None) and float(x) > 1 else None


def fair_over(row: dict) -> float | None:
    """Fair P(over) from best prices: back/lay midpoints (implied), normalised when both sides are quoted."""
    def mid(side: str) -> float | None:
        b, l = _f(row.get(f"back_{side}", "")), _f(row.get(f"lay_{side}", ""))
        return (1 / b + 1 / l) / 2 if b and l else None
    mo, mu = mid("over"), mid("under")
    if mo and mu:
        return mo / (mo + mu)
    if mo:
        return mo
    if mu:
        return 1 - mu
    bo, bu = _f(row.get("back_over", "")), _f(row.get("back_under", ""))
    return (1 / bo) / (1 / bo + 1 / bu) if bo and bu else None


def ensure_book(con: sqlite3.Connection) -> tuple[int, dict[str, int]]:
    """Betfair bookmaker row (sharp reference) and its card-count settlement rules (yellow 1, second
    yellow 1, straight red 1, at most 2 a player, no staff cards: Betfair's published card rules)."""
    con.execute("INSERT OR IGNORE INTO bookmakers (name, is_sharp) VALUES (?, 1)", (BOOK,))
    con.execute("UPDATE bookmakers SET is_sharp = 1 WHERE name = ?", (BOOK,))
    book = con.execute("SELECT bookmaker_id FROM bookmakers WHERE name = ?", (BOOK,)).fetchone()[0]
    markets = {}
    for code, scope in (("TOTAL_CARDS", "match"), ("HOME_CARDS", "home"), ("AWAY_CARDS", "away")):
        con.execute("INSERT OR IGNORE INTO markets (code, unit, scope, period) VALUES (?, 'cards', ?, 'FT')", (code, scope))
        mid = con.execute("SELECT market_id FROM markets WHERE code = ?", (code,)).fetchone()[0]
        con.execute("INSERT OR IGNORE INTO settlement_rules VALUES (?, ?, 1, 1, 1, 0)", (book, mid))
        markets[code] = mid
    return book, markets


def import_csvs(con: sqlite3.Connection, now: datetime, paths: list[Path]) -> dict:
    book, markets = ensure_book(con)
    # The database's kickoff wins over the logged one, in case a fixture moved after logging
    kickoff = {mid: ko for mid, ko in con.execute("SELECT match_id, kickoff_utc FROM matches "
                                                  "WHERE status IN ('scheduled', 'finished')")}
    rows = []
    for p in sorted(paths):
        with p.open(newline="") as fh:
            rows += [r for r in csv.DictReader(fh)]
    added = 0
    closing: dict[tuple, dict] = {}
    for r in rows:
        mid = int(r["match_id"])
        if mid not in kickoff:
            continue
        r = dict(r, kickoff_utc=kickoff[mid])
        if r["captured_at"] >= r["kickoff_utc"]:
            continue
        over, under = _f(r["back_over"]), _f(r["back_under"])
        if over or under:
            added += con.execute("""INSERT OR IGNORE INTO odds_snapshots (match_id, bookmaker_id, market_id, line,
                                    over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                 (mid, book, markets[r["market_code"]], float(r["line"]), over, under,
                                  r["captured_at"])).rowcount
        ko = parse_iso(r["kickoff_utc"])
        if ko <= now and parse_iso(r["captured_at"]) >= ko - CLOSING_WINDOW:
            key = (mid, r["market_code"], float(r["line"]))
            if key not in closing or r["captured_at"] > closing[key]["captured_at"]:
                closing[key] = r
    for (mid, code, line), r in closing.items():
        con.execute("""UPDATE odds_snapshots SET is_closing = (captured_at = ?)
                       WHERE match_id = ? AND bookmaker_id = ? AND market_id = ? AND line = ?""",
                    (r["captured_at"], mid, book, markets[code], line))
    clv = fill_clv(con, closing, book)
    con.commit()
    return dict(rows=len(rows), snapshots_added=added, closing=len(closing), clv_filled=clv)


def fill_clv(con: sqlite3.Connection, closing: dict[tuple, dict], book: int) -> int:
    filled = 0
    bets = con.execute("""SELECT vb.bet_id, vb.selection, vb.taken_odds, o.match_id, o.line, mk.code
                          FROM value_bets vb JOIN odds_snapshots o USING (snapshot_id) JOIN markets mk USING (market_id)
                          WHERE vb.closing_odds IS NULL AND o.bookmaker_id != ?""", (book,)).fetchall()
    for bet_id, selection, taken, match_id, line, code in bets:
        r = closing.get((match_id, code, float(line)))
        if r is None or float(line) == int(line):  # whole lines can push; no fair price from two runners
            continue
        p_over = fair_over(r)
        if p_over is None or not 0 < p_over < 1:
            continue
        fair = 1 / (p_over if selection == "over" else 1 - p_over)
        con.execute("UPDATE value_bets SET closing_odds = ?, clv = ? WHERE bet_id = ?",
                    (round(fair, 4), round(taken / fair - 1, 4), bet_id))
        filled += 1
    return filled


# ───────────────────────────── CLI ─────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["log", "import"])
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--hours", type=float, default=36, help="log fixtures kicking off within this many hours")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if args.command == "import":
        con = sqlite3.connect(args.db)
        con.execute("PRAGMA foreign_keys = ON")
        print(import_csvs(con, now, list(OUT_DIR.glob("20??-??.csv"))))
        return 0
    env = os.environ
    missing = [k for k in ("BETFAIR_APP_KEY", "BETFAIR_USERNAME", "BETFAIR_PASSWORD") if not env.get(k)]
    if missing:
        print(f"Missing secrets: {', '.join(missing)}", file=sys.stderr)
        return 2
    db = DB(args.db, None)
    if not upcoming(db.con, now, args.hours):
        print(f"No fixtures in the next {args.hours:g} hours; nothing to log")
        return 0
    client = Betfair(env["BETFAIR_APP_KEY"])
    try:
        client.login(env["BETFAIR_USERNAME"], env["BETFAIR_PASSWORD"], env.get("BETFAIR_CERT"), env.get("BETFAIR_KEY"))
    except LoginRefused as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        print(snapshot(client, db, now, args.hours))
    finally:
        client.logout()
    return 0


if __name__ == "__main__":
    sys.exit(main())
