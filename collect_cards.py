#!/usr/bin/env python3
"""
EPL + SPL card-market collector -> SQLite (card_value_schema.sql)

Sources
  Results, cards, fouls, EPL referees : football-data.co.uk season CSVs  (Playwright request API)
  Upcoming fixtures                   : football-data.co.uk fixtures.csv (Playwright request API)
  Referees the CSVs lack (SPL)        : ESPN public scoreboard/summary JSON
  Upcoming referee appointments       : the leagues' own feeds, every run: Premier League API, Scottish FA,
                                        DFB data centre, Lega Serie A API, Ligue 1 API, LaLiga API;
                                        other appointment pages via --ref-url  (Playwright + stealth)

Setup
  pip install playwright playwright-stealth
  playwright install chromium

Run
  python collect_cards.py --db cards.db --init-schema card_value_schema.sql --season 2026-27 \
      --ref-url <Premier League match-officials article> --ref-url <Scottish appointments page>
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import difflib
import html
import io
import json
import logging
import os
import random
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page, Route, async_playwright
from playwright_stealth import Stealth

from settle import settle

log = logging.getLogger("collector")
UK = ZoneInfo("Europe/London")
ISO = "%Y-%m-%dT%H:%M:%SZ"

LEAGUES = {  # keys match leagues.code in the schema seed
    "EPL": dict(fd_code="E0", espn_slug="eng.1", country="England"),
    "LALIGA": dict(fd_code="SP1", espn_slug="esp.1", country="Spain"),
    "SERIEA": dict(fd_code="I1", espn_slug="ita.1", country="Italy"),
    "BUNDES": dict(fd_code="D1", espn_slug="ger.1", country="Germany"),
    "LIGUE1": dict(fd_code="F1", espn_slug="fra.1", country="France"),
    "SPL": dict(fd_code="SC0", espn_slug="sco.1", country="Scotland"),
}
FD_BASE = "https://www.football-data.co.uk"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# Min seconds between requests per host
HOST_INTERVAL = {"www.football-data.co.uk": 2.0, "site.api.espn.com": 1.5}
DEFAULT_INTERVAL = 4.0
BLOCKED_RESOURCES = {"image", "media", "font"}


# ───────────────────────────── HTTP helpers ─────────────────────────────
class HostThrottle:
    """Serialises requests per host with a minimum gap plus jitter."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        async with self._locks.setdefault(host, asyncio.Lock()):
            loop = asyncio.get_running_loop()
            gap = HOST_INTERVAL.get(host, DEFAULT_INTERVAL) + random.uniform(0.5, 2.0)
            delay = self._last.get(host, -1e9) + gap - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = loop.time()


THROTTLE = HostThrottle()


async def fetch_page(ctx: BrowserContext, url: str, wait_selector: str, retries: int = 3) -> Page:
    """Open url in a stealth page and wait for wait_selector (lets JS challenges resolve)."""
    for attempt in range(1, retries + 1):
        await THROTTLE.wait(url)
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            if resp and (resp.status == 429 or resp.status >= 500):
                raise RuntimeError(f"HTTP {resp.status}")
            await page.wait_for_selector(wait_selector, state="attached", timeout=25_000)
            return page
        except Exception as exc:
            await page.close()
            if attempt == retries:
                raise
            backoff = (60 if "429" in str(exc) else 8) * attempt
            log.warning("%s failed (%s), retry %d in %ss", url, exc, attempt, backoff)
            await asyncio.sleep(backoff)
    raise RuntimeError("unreachable")


async def fetch_text(ctx: BrowserContext, url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        await THROTTLE.wait(url)
        try:
            resp = await ctx.request.get(url, timeout=30_000)
        except Exception as exc:  # timeouts, resets: retry rather than abort the whole step
            log.warning("%s failed (%s), attempt %d", url, str(exc).splitlines()[0], attempt)
            await asyncio.sleep(8 * attempt)
            continue
        if resp.status == 404:
            return None
        if resp.ok:
            body = await resp.body()
            try:
                return body.decode("utf-8-sig")
            except UnicodeDecodeError:
                return body.decode("latin-1")
        log.warning("%s -> HTTP %d (attempt %d)", url, resp.status, attempt)
        await asyncio.sleep(8 * attempt)
    return None


async def fetch_json(ctx: BrowserContext, url: str) -> dict | None:
    text = await fetch_text(ctx, url)
    return json.loads(text) if text else None


async def _block_heavy(route: Route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


# ───────────────────────────── Name matching ─────────────────────────────
TOKEN_MAP = {"utd": "united", "man": "manchester", "nottm": "nottingham", "nottham": "nottingham"}
NICKNAMES = {
    "wolves": "wolverhampton wanderers",
    "spurs": "tottenham hotspur",
    "hearts": "heart of midlothian",
    "hibs": "hibernian",
    "qpr": "queens park rangers",
}
DROP = {"fc", "afc", "the"}


def fold(name: str) -> str:
    """Accent- and case-free form for comparing person names."""
    return " ".join(unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().split())


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("'", "").replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = " ".join(TOKEN_MAP.get(t, t) for t in s.split() if t not in DROP)
    return NICKNAMES.get(s, s)


class TeamResolver:
    """Maps any source spelling to a canonical team_id within a candidate set.
    Every resolution is written to name_aliases; edit that table to correct a bad match."""

    def __init__(self, db: DB, team_ids: set[int]):
        self.db = db
        self.by_norm: dict[str, int] = {}
        if team_ids:
            marks = ",".join("?" * len(team_ids))
            rows = db.q(f"SELECT team_id, name FROM teams WHERE team_id IN ({marks})", *team_ids)
            self.by_norm = {norm(n): tid for tid, n in rows}

    def resolve(self, raw: str, source: str, warn: bool = True) -> int | None:
        raw = raw.strip()
        if (hit := self.db.alias_lookup(source, "team", raw)) is not None:
            return hit
        key = norm(raw)
        tid = self.by_norm.get(key)
        if tid is None:  # token-subset match, must be unique ("Newcastle" ~ "Newcastle Utd")
            toks = set(key.split())
            subset = {t for k, t in self.by_norm.items()
                      if toks and (toks <= set(k.split()) or set(k.split()) <= toks)}
            tid = subset.pop() if len(subset) == 1 else None
        if tid is None:
            close = difflib.get_close_matches(key, list(self.by_norm), n=2, cutoff=0.85)
            tid = self.by_norm[close[0]] if len(close) == 1 else None
        if tid is None:
            if warn:
                log.warning("Unmatched team %r from %s; add it to name_aliases", raw, source)
            return None
        self.db.alias_save(source, "team", raw, tid)
        return tid


# ───────────────────────────── Database ─────────────────────────────
class DB:
    def __init__(self, path: str, schema: str | None):
        self.con = sqlite3.connect(path)
        self.con.execute("PRAGMA foreign_keys = ON")
        if schema and not self._has_schema():
            self.con.executescript(Path(schema).read_text())
        if not self._has_schema():
            raise SystemExit("Schema missing: pass --init-schema card_value_schema.sql")
        self._has_prices: bool | None = None

    def _has_schema(self) -> bool:
        return bool(self.q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leagues'"))

    def q(self, sql: str, *args):
        return self.con.execute(sql, args).fetchall()

    def one(self, sql: str, *args):
        row = self.con.execute(sql, args).fetchone()
        return row[0] if row else None

    def commit(self) -> None:
        self.con.commit()

    def alias_lookup(self, source: str, etype: str, alias: str) -> int | None:
        return self.one("SELECT entity_id FROM name_aliases WHERE source=? AND entity_type=? AND alias=?",
                        source, etype, alias)

    def alias_save(self, source: str, etype: str, alias: str, eid: int) -> None:
        self.con.execute("INSERT OR IGNORE INTO name_aliases (source, entity_type, alias, entity_id) "
                         "VALUES (?,?,?,?)", (source, etype, alias, eid))

    def league_id(self, code: str) -> int:
        lid = self.one("SELECT league_id FROM leagues WHERE code=?", code)
        if lid is None:
            raise SystemExit(f"League {code} not seeded in leagues table")
        return lid

    def season_id(self, league_id: int, label: str, start: str, end: str) -> int:
        self.con.execute(
            """INSERT INTO seasons (league_id, label, start_date, end_date) VALUES (?,?,?,?)
               ON CONFLICT(league_id, label) DO UPDATE SET
                 start_date = min(start_date, excluded.start_date),
                 end_date   = max(coalesce(end_date, excluded.end_date), excluded.end_date)""",
            (league_id, label, start, end))
        return self.one("SELECT season_id FROM seasons WHERE league_id=? AND label=?", league_id, label)

    def team_id(self, name: str, country: str) -> int:
        self.con.execute("INSERT OR IGNORE INTO teams (name, country) VALUES (?,?)", (name, country))
        return self.one("SELECT team_id FROM teams WHERE name=? AND country=?", name, country)

    def referee_id(self, name: str, country: str, source: str) -> int:
        if (hit := self.alias_lookup(source, "referee", name)) is not None:
            return hit
        rid = self.one("SELECT referee_id FROM referees WHERE name=? AND country=?", name, country)
        if rid is None:  # accents and case: "Jérémy STINAT" is "Jeremy Stinat"
            known = self.q("SELECT referee_id, name FROM referees WHERE country=?", country)
            same = [r for r, n in known if fold(n) == fold(name)]
            rid = same[0] if len(same) == 1 else None
            if rid is None and len(parts := fold(name).split()) >= 2:
                # "Willie Collum" vs "William Collum": same first initial + surname, if unique
                cands = [r for r, n in known if (f := fold(n).split()) and f[-1] == parts[-1] and f[0][0] == parts[0][0]]
                if len(cands) == 1:
                    rid = cands[0]
        if rid is None:
            rid = self.con.execute("INSERT INTO referees (name, country) VALUES (?,?)",
                                   (name, country)).lastrowid
            log.info("New referee: %s (%s, via %s)", name, country, source)
        self.alias_save(source, "referee", name, rid)
        return rid

    def find_match(self, season_id: int, home: int, away: int, day: str, tol_days: int = 1) -> int | None:
        return self.one(
            """SELECT match_id FROM matches
               WHERE season_id=? AND home_team_id=? AND away_team_id=?
                 AND abs(julianday(date(kickoff_utc)) - julianday(?)) <= ?
               ORDER BY abs(julianday(date(kickoff_utc)) - julianday(?)) LIMIT 1""",
            season_id, home, away, day, tol_days, day)

    def upsert_match(self, m: dict) -> int:
        mid = None
        if m["external_id"]:
            mid = self.one("SELECT match_id FROM matches WHERE external_id=?", m["external_id"])
        if mid is None and m["matchweek"] is not None:  # survives TV kickoff moves
            mid = self.one("SELECT match_id FROM matches WHERE season_id=? AND home_team_id=? "
                           "AND away_team_id=? AND matchweek=?",
                           m["season_id"], m["home"], m["away"], m["matchweek"])
        if mid is None:
            mid = self.find_match(m["season_id"], m["home"], m["away"], m["kickoff_utc"][:10], 0)
        if mid is None:
            return self.con.execute(
                """INSERT INTO matches (season_id, matchweek, kickoff_utc, home_team_id, away_team_id,
                       referee_id, status, home_goals, away_goals, external_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (m["season_id"], m["matchweek"], m["kickoff_utc"], m["home"], m["away"],
                 m["referee_id"], m["status"], m["hg"], m["ag"], m["external_id"])).lastrowid
        self.con.execute(
            """UPDATE matches SET kickoff_utc=?, status=?, home_goals=?, away_goals=?,
                   matchweek=COALESCE(?, matchweek), referee_id=COALESCE(?, referee_id),
                   external_id=COALESCE(?, external_id)
               WHERE match_id=?""",
            (m["kickoff_utc"], m["status"], m["hg"], m["ag"], m["matchweek"],
             m["referee_id"], m["external_id"], mid))
        return mid

    def match_by_espn(self, event_id: str) -> int | None:
        """'espn-fx:<id>' is set by the fixture loader; backfill_referees rewrites it to 'espn:<id>' once checked."""
        return self.one("SELECT match_id FROM matches WHERE external_id IN (?, ?)",
                        f"espn:{event_id}", f"espn-fx:{event_id}")

    def merge_match(self, keep: int, dup: int) -> None:
        """Fold a duplicate scheduled row into `keep`: bets/odds move over, football-data prices win over
        ESPN's, a known referee is kept."""
        con = self.con
        con.execute("UPDATE odds_snapshots SET match_id=? WHERE match_id=?", (keep, dup))
        if _has_table(self, "match_prices") and self.one("SELECT 1 FROM match_prices WHERE match_id=?", dup):
            if self.one("SELECT source FROM match_prices WHERE match_id=?", dup) == "football-data:avg" \
                    or not self.one("SELECT 1 FROM match_prices WHERE match_id=?", keep):
                con.execute("DELETE FROM match_prices WHERE match_id=?", (keep,))
                con.execute("UPDATE match_prices SET match_id=? WHERE match_id=?", (keep, dup))
        con.execute("""UPDATE matches SET referee_id=COALESCE(referee_id, (SELECT referee_id FROM matches
                       WHERE match_id=?)) WHERE match_id=?""", (dup, keep))
        con.execute("DELETE FROM matches WHERE match_id=?", (dup,))

    def upsert_prices(self, match_id: int, row: dict) -> None:
        """Pre-match 1X2 market averages (migration 001); no-op until that table exists."""
        if self._has_prices is None:
            self._has_prices = bool(self.q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='match_prices'"))
        try:
            odds = [float(row.get(k) or 0) for k in ("AvgH", "AvgD", "AvgA")]
        except ValueError:
            return
        self.store_prices(match_id, odds, "football-data:avg", overwrite=True)

    def store_prices(self, match_id: int, odds: list[float], source: str, overwrite: bool) -> int:
        """De-margined 1X2 probabilities. football-data's market average (what the model is trained on)
        overwrites; other sources only fill matches that have no prices yet."""
        if self._has_prices is None:
            self._has_prices = bool(self.q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='match_prices'"))
        if not self._has_prices or len(odds) != 3 or min(odds) <= 1:
            return 0
        inv = [1 / o for o in odds]
        p = [x / sum(inv) for x in inv]
        conflict = ("DO UPDATE SET source=excluded.source, home_odds=excluded.home_odds, draw_odds=excluded.draw_odds, "
                    "away_odds=excluded.away_odds, p_home=excluded.p_home, p_draw=excluded.p_draw, "
                    "p_away=excluded.p_away") if overwrite else "DO NOTHING"
        return self.con.execute(
            f"""INSERT INTO match_prices (match_id, source, home_odds, draw_odds, away_odds, p_home, p_draw, p_away)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(match_id) {conflict}""",
            (match_id, source, *odds, *p)).rowcount

    def upsert_cards(self, match_id: int, team_id: int, is_home: int, yellows: int,
                     second_yellows: int, straight_reds: int, fouls: int | None) -> None:
        """football-data rows: yellows exclude the first yellow of a two-yellow dismissal and all reds come
        as straight_reds. split_reds corrects that from ESPN events (marking reds_1h); a reload keeps the
        split while football-data's totals still agree with it, otherwise resets it for a fresh check."""
        old = self.con.execute("""SELECT yellows, second_yellows, straight_reds, reds_1h FROM match_team_cards
                                  WHERE match_id=? AND team_id=?""", (match_id, team_id)).fetchone()
        if old and old[3] is not None and second_yellows == 0 \
                and (old[0] - old[1], old[1] + old[2]) == (yellows, straight_reds):
            self.con.execute("UPDATE match_team_cards SET fouls=COALESCE(?, fouls) WHERE match_id=? AND team_id=?",
                             (fouls, match_id, team_id))
            return
        self.con.execute(
            """INSERT INTO match_team_cards
                   (match_id, team_id, is_home, yellows, second_yellows, straight_reds, fouls)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(match_id, team_id) DO UPDATE SET
                   yellows=excluded.yellows, second_yellows=excluded.second_yellows,
                   straight_reds=excluded.straight_reds, fouls=COALESCE(excluded.fouls, fouls),
                   yellows_1h=NULL, reds_1h=NULL""",
            (match_id, team_id, is_home, yellows, second_yellows, straight_reds, fouls))


def fd_season(label: str) -> str:
    """'2026-27' -> '2627' (football-data folder name)"""
    y = int(label[:4])
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def shift_season(label: str, n: int) -> str:
    """('2026-27', -1) -> '2025-26'"""
    y = int(label[:4]) + n
    return f"{y}-{(y + 1) % 100:02d}"


# ───────────────────────────── Source 1: football-data results, cards & fouls ─────────────────────────────
def _int(row: dict, key: str) -> int | None:
    v = (row.get(key) or "").strip()
    return int(float(v)) if v else None


def fd_kickoff(row: dict) -> str:
    """football-data Date (dd/mm/yy[yy]) + Time (UK local, may be blank) -> UTC ISO"""
    raw = row["Date"].strip()
    day = datetime.strptime(raw, "%d/%m/%Y" if len(raw) == 10 else "%d/%m/%y")
    hm = re.match(r"(\d{1,2}):(\d{2})", (row.get("Time") or "").strip())
    local = day.replace(hour=int(hm[1]) if hm else 15, minute=int(hm[2]) if hm else 0, tzinfo=UK)
    return local.astimezone(timezone.utc).strftime(ISO)


async def load_results(ctx: BrowserContext, db: DB, code: str, season: str) -> None:
    cfg = LEAGUES[code]
    url = f"{FD_BASE}/mmz4281/{fd_season(season)}/{cfg['fd_code']}.csv"
    text = await fetch_text(ctx, url)
    rows = [r for r in csv.DictReader(io.StringIO(text or "")) if r.get("HomeTeam") and r.get("Date")]
    if not rows:
        log.warning("football-data %s %s: no rows at %s", code, season, url)
        return
    kickoffs = [fd_kickoff(r) for r in rows]
    days = sorted(k[:10] for k in kickoffs)
    sid = db.season_id(db.league_id(code), season, days[0], days[-1])

    with_cards = 0
    for r, ko in zip(rows, kickoffs):
        home = db.team_id(r["HomeTeam"].strip(), cfg["country"])
        away = db.team_id(r["AwayTeam"].strip(), cfg["country"])
        hg, ag = _int(r, "FTHG"), _int(r, "FTAG")
        ref = (r.get("Referee") or "").strip()  # E0 only; SPL filled from ESPN
        mid = db.upsert_match(dict(
            season_id=sid, matchweek=None, kickoff_utc=ko, home=home, away=away,
            status="finished" if hg is not None else "scheduled", hg=hg, ag=ag,
            referee_id=db.referee_id(ref, cfg["country"], "football-data") if ref else None,
            external_id=None,
        ))
        db.upsert_prices(mid, r)
        if _int(r, "HY") is None:
            continue
        # football-data gives total reds without the second-yellow split -> all into straight_reds
        for tid, is_home, side in ((home, 1, "H"), (away, 0, "A")):
            db.upsert_cards(mid, tid, is_home, yellows=_int(r, f"{side}Y") or 0, second_yellows=0,
                            straight_reds=_int(r, f"{side}R") or 0, fouls=_int(r, f"{side}F"))
        with_cards += 1
    db.commit()
    log.info("football-data %s %s: %d matches, %d with cards/fouls", code, season, len(rows), with_cards)


# ───────────────────────────── Source 2: football-data upcoming fixtures ─────────────────────────────
async def load_upcoming(ctx: BrowserContext, db: DB, codes: list[str], season: str) -> None:
    text = await fetch_text(ctx, f"{FD_BASE}/fixtures.csv")
    if not text:
        log.warning("football-data fixtures.csv unavailable")
        return
    by_div = {LEAGUES[c]["fd_code"]: c for c in codes}
    added = 0
    for r in csv.DictReader(io.StringIO(text)):
        code = by_div.get((r.get("Div") or "").strip())
        if not code or not r.get("HomeTeam") or not r.get("Date"):
            continue
        cfg = LEAGUES[code]
        ko = fd_kickoff(r)
        sid = db.season_id(db.league_id(code), season, ko[:10], ko[:10])
        home = db.team_id(r["HomeTeam"].strip(), cfg["country"])
        away = db.team_id(r["AwayTeam"].strip(), cfg["country"])
        mid = db.find_match(sid, home, away, ko[:10], 0)
        if mid and db.one("SELECT status FROM matches WHERE match_id=?", mid) == "finished":
            continue  # fixtures.csv can lag behind results
        ref = (r.get("Referee") or "").strip()
        mid = db.upsert_match(dict(season_id=sid, matchweek=None, kickoff_utc=ko, home=home, away=away,
                                   status="scheduled", hg=None, ag=None, external_id=None,
                                   referee_id=db.referee_id(ref, cfg["country"], "football-data") if ref else None))
        db.upsert_prices(mid, r)
        added += 1
    db.commit()
    log.info("football-data fixtures: %d upcoming %s matches", added, "/".join(codes))


def mark_stale(db: DB, days: int = 4) -> None:
    """Scheduled matches with no result days after kickoff were postponed; a played one
    gets set back to finished when its result row arrives (same date -> same match)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(ISO)
    n = db.con.execute("UPDATE matches SET status='postponed' WHERE status='scheduled' AND kickoff_utc < ?",
                       (cutoff,)).rowcount
    db.commit()
    if n:
        log.info("%d past scheduled matches without a result marked postponed", n)


# ───────────────────────────── Source 3: ESPN referees ─────────────────────────────
ESPN_PROBE = 10  # give up on a league if this many summaries in a row have no referee
ESPN_FIRST_MONTH = "2022-04"  # ESPN match summaries list officials from about April 2022; earlier months have none


def espn_referee(summary: dict) -> str | None:
    officials = (summary.get("gameInfo") or {}).get("officials") or []
    def pos(o: dict) -> str:
        p = o.get("position") or {}
        return (p.get("name") or p.get("displayName") or "").strip().lower()
    pick = (next((o for o in officials if pos(o) == "referee"), None)
            or next((o for o in officials if not pos(o) and o.get("order") == 1), None)
            or (officials[0] if len(officials) == 1 and not pos(officials[0]) else None))
    name = (pick or {}).get("fullName") or (pick or {}).get("displayName")
    return name.strip() if name else None


async def backfill_referees(ctx: BrowserContext, db: DB, code: str, season: str) -> None:
    """Fill referee_id on finished matches the CSVs left blank. Checked matches get
    external_id 'espn:<event id>' so later runs don't re-request them."""
    cfg = LEAGUES[code]
    sid = db.one("SELECT season_id FROM seasons WHERE league_id=? AND label=?", db.league_id(code), season)
    if sid is None:
        return
    todo = {mid: ko for mid, ko in db.q(
        """SELECT match_id, kickoff_utc FROM matches
           WHERE season_id=? AND status='finished' AND referee_id IS NULL
             AND (external_id IS NULL OR external_id NOT LIKE 'espn:%')""", sid)}
    todo = {mid: ko for mid, ko in todo.items() if ko[:7] >= ESPN_FIRST_MONTH}
    if not todo:
        return
    team_ids = {t for (t,) in db.q("SELECT home_team_id FROM matches WHERE season_id=? UNION "
                                   "SELECT away_team_id FROM matches WHERE season_id=?", sid, sid)}
    resolver = TeamResolver(db, team_ids)
    base = f"{ESPN_BASE}/{cfg['espn_slug']}"

    found = checked = 0
    for ym in sorted({ko[:7] for ko in todo.values()}):
        # ESPN rejects day ranges (HTTP 400); a YYYYMM month returns the whole month
        board = await fetch_json(ctx, f"{base}/scoreboard?dates={ym.replace('-', '')}&limit=200")
        for ev in (board or {}).get("events", []):
            try:
                sides = {c["homeAway"]: c["team"]["displayName"] for c in ev["competitions"][0]["competitors"]}
                h = resolver.resolve(sides["home"], "espn")
                a = resolver.resolve(sides["away"], "espn")
            except (KeyError, IndexError, TypeError):
                continue
            mid = db.find_match(sid, h, a, ev["date"][:10]) if h and a else None
            if mid not in todo:
                continue
            summary = await fetch_json(ctx, f"{base}/summary?event={ev['id']}")
            if summary is None:
                continue
            checked += 1
            if ref := espn_referee(summary):
                db.con.execute("UPDATE matches SET referee_id=? WHERE match_id=?",
                               (db.referee_id(ref, cfg["country"], "espn"), mid))
                found += 1
            db.con.execute("UPDATE matches SET external_id=? WHERE match_id=?", (f"espn:{ev['id']}", mid))
            del todo[mid]
            if checked >= ESPN_PROBE and not found:
                db.commit()
                log.warning("ESPN %s %s: no referee in %d summaries, giving up", code, season, checked)
                return
        db.commit()
    log.info("ESPN %s %s: referees for %d of %d checked matches (%d not found on ESPN)",
             code, season, found, checked, len(todo))


# ───────────────────────────── Source 3b: ESPN red-card split ─────────────────────────────
SECOND_YELLOW = re.compile(r"^second yellow", re.I)


async def split_reds(ctx: BrowserContext, db: DB, code: str, season: str) -> None:
    """football-data counts a two-yellow dismissal as one red and drops its first yellow. For finished
    matches with reds, ESPN's card events say which reds were second yellows: those move to
    second_yellows and their first yellow is added back. Also fills yellows_1h/reds_1h, which mark the
    match as checked. A team whose ESPN red count differs from football-data's is left as it is."""
    cfg = LEAGUES[code]
    sid = db.one("SELECT season_id FROM seasons WHERE league_id=? AND label=?", db.league_id(code), season)
    if sid is None:
        return
    todo = {mid: ko for mid, ko in db.q(
        """SELECT m.match_id, m.kickoff_utc FROM matches m WHERE m.season_id=? AND m.status='finished'
             AND EXISTS (SELECT 1 FROM match_team_cards c WHERE c.match_id=m.match_id
                         AND c.straight_reds > 0 AND c.reds_1h IS NULL)""", sid)}
    if not todo:
        return
    by_espn = {a: tid for a, tid in db.q("SELECT alias, entity_id FROM name_aliases "
                                          "WHERE source='espn-id' AND entity_type='team'")}
    team_ids = {t for (t,) in db.q("SELECT home_team_id FROM matches WHERE season_id=? UNION "
                                   "SELECT away_team_id FROM matches WHERE season_id=?", sid, sid)}
    resolver = TeamResolver(db, team_ids)
    base = f"{ESPN_BASE}/{cfg['espn_slug']}"
    split = checked = mismatch = 0
    for ym in sorted({ko[:7] for ko in todo.values()}):
        board = await fetch_json(ctx, f"{base}/scoreboard?dates={ym.replace('-', '')}&limit=200")
        for ev in (board or {}).get("events", []):
            try:
                side = {str(c["team"]["id"]): (by_espn.get(str(c["team"]["id"]))
                                               or resolver.resolve(c["team"]["displayName"], "espn"), c["homeAway"])
                        for c in ev["competitions"][0]["competitors"]}
            except (KeyError, IndexError, TypeError):
                continue
            ours = {ha: tid for tid, ha in side.values()}
            if not ours.get("home") or not ours.get("away"):
                continue
            mid = db.find_match(sid, ours["home"], ours["away"], ev["date"][:10])
            if mid not in todo:
                continue
            summary = await fetch_json(ctx, f"{base}/summary?event={ev['id']}")
            if summary is None:
                continue
            checked += 1
            del todo[mid]
            tally = {tid: dict(y1=0, r=0, r1=0, two=0) for tid, _ in side.values()}
            for k in summary.get("keyEvents", []):
                kind = (k.get("type") or {}).get("type")
                if kind not in ("yellow-card", "red-card"):
                    continue
                tid = side.get(str((k.get("team") or {}).get("id")), (None,))[0]
                if tid not in tally:
                    continue
                first_half = (k.get("period") or {}).get("number") == 1
                t = tally[tid]
                if kind == "yellow-card":
                    t["y1"] += first_half
                else:
                    t["r"] += 1
                    t["r1"] += first_half
                    if SECOND_YELLOW.match(k.get("text") or ""):  # its first yellow is a yellow-card event
                        t["two"] += 1
            for tid, t in tally.items():
                row = db.con.execute("""SELECT yellows, second_yellows, straight_reds FROM match_team_cards
                                        WHERE match_id=? AND team_id=?""", (mid, tid)).fetchone()
                if row is None or row[1]:
                    continue
                if t["r"] != row[2]:
                    mismatch += 1
                    log.debug("ESPN %s: match %d team %d has %d reds, football-data %d", code, mid, tid, t["r"], row[2])
                    continue
                db.con.execute("""UPDATE match_team_cards SET yellows=?, second_yellows=?, straight_reds=?,
                                      yellows_1h=?, reds_1h=? WHERE match_id=? AND team_id=?""",
                               (row[0] + t["two"], t["two"], row[2] - t["two"], t["y1"], t["r1"],
                                mid, tid))
                split += t["two"]
        db.commit()
    log.info("ESPN %s %s: %d two-yellow dismissals split out in %d checked matches (%d team red counts disagreed, "
             "%d matches not found)", code, season, split, checked, mismatch, len(todo))


# ───────────────────────────── Source 3a: ESPN week-ahead league fixtures ─────────────────────────────
def american_to_decimal(ml) -> float | None:
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    return 1 + (ml / 100 if ml > 0 else 100 / -ml) if ml else None


async def load_espn_fixtures(ctx: BrowserContext, db: DB, code: str, season: str, days: int = 10) -> None:
    """Scheduled league matches for the next `days` from ESPN (fixtures.csv only looks a few days ahead).
    Teams matched by ESPN id. Fills 1X2 prices from ESPN's bookmaker line where football-data has none yet."""
    cfg = LEAGUES[code]
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    sid = db.one("SELECT season_id FROM seasons WHERE league_id=? AND label=?", db.league_id(code), season)
    if sid is None:
        return
    by_espn = {a: tid for a, tid in db.q("SELECT alias, entity_id FROM name_aliases "
                                          "WHERE source='espn-id' AND entity_type='team'")}
    team_ids = {t for (t,) in db.q("SELECT home_team_id FROM matches WHERE season_id=? UNION "
                                   "SELECT away_team_id FROM matches WHERE season_id=?", sid, sid)}
    resolver = TeamResolver(db, team_ids)
    added = priced = 0
    for ym in sorted({now.strftime("%Y%m"), horizon.strftime("%Y%m")}):
        board = await fetch_json(ctx, f"{ESPN_BASE}/{cfg['espn_slug']}/scoreboard?dates={ym}&limit=200")
        for ev in (board or {}).get("events", []):
            try:
                comp = ev["competitions"][0]
                state = ev["status"]["type"]
                ko = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            except (KeyError, IndexError, ValueError):
                continue
            eid = str(ev.get("id") or "")
            known = db.match_by_espn(eid) if eid else None
            if state.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED"):
                if known:  # settle voids its bets if no new date within 3 days
                    db.con.execute("UPDATE matches SET status='postponed' WHERE match_id=? AND status='scheduled'",
                                   (known,))
                continue
            if state.get("completed"):
                continue
            # a known fixture is followed wherever it moves; new ones only inside the window
            if ko <= now - timedelta(hours=3) or (known is None and ko > horizon):
                continue
            side = {}
            for c in comp.get("competitors", []):
                espn_id, name = str(c["team"]["id"]), c["team"]["displayName"]
                tid = by_espn.get(espn_id) or resolver.resolve(name, "espn")
                if tid is not None and espn_id not in by_espn:
                    db.alias_save("espn-id", "team", espn_id, tid)
                    by_espn[espn_id] = tid
                side[c.get("homeAway")] = tid
            home, away = side.get("home"), side.get("away")
            if not home or not away:
                continue
            ko_s = ko.strftime(ISO)
            mid = known or db.find_match(sid, home, away, ko_s[:10], 0) or db.one(
                # rows stored before event ids were kept, possibly at a date the fixture has since left
                """SELECT match_id FROM matches WHERE season_id=? AND home_team_id=? AND away_team_id=?
                     AND status IN ('scheduled','postponed') AND external_id IS NULL
                     AND abs(julianday(kickoff_utc) - julianday(?)) <= 14
                   ORDER BY abs(julianday(kickoff_utc) - julianday(?)) LIMIT 1""", sid, home, away, ko_s, ko_s)
            if mid and db.one("SELECT status FROM matches WHERE match_id=?", mid) == "finished":
                continue
            if mid is None:
                mid = db.upsert_match(dict(season_id=sid, matchweek=None, kickoff_utc=ko_s, home=home, away=away,
                                           status="scheduled", hg=None, ag=None, external_id=f"espn-fx:{eid}",
                                           referee_id=None))
            else:
                # football-data's fixtures.csv may already hold the new date as a separate row
                for (dup,) in db.q("""SELECT match_id FROM matches WHERE season_id=? AND home_team_id=?
                                        AND away_team_id=? AND match_id<>? AND status='scheduled'
                                        AND external_id IS NULL
                                        AND abs(julianday(date(kickoff_utc)) - julianday(date(?))) <= 1""",
                                   sid, home, away, mid, ko_s):
                    db.merge_match(mid, dup)
                old_ko = db.one("SELECT kickoff_utc FROM matches WHERE match_id=?", mid)
                if old_ko != ko_s:
                    log.info("ESPN %s: %s moved %s -> %s", code, eid, old_ko, ko_s)
                db.con.execute("""UPDATE matches SET kickoff_utc=?, status='scheduled',
                                      external_id=COALESCE(external_id, ?) WHERE match_id=?""",
                               (ko_s, f"espn-fx:{eid}", mid))
            added += 1
            odds = (comp.get("odds") or [{}])[0]
            ml = odds.get("moneyline") or {}
            dec = [american_to_decimal(((ml.get(k) or {}).get("close") or {}).get("odds")
                                       or ((ml.get(k) or {}).get("open") or {}).get("odds"))
                   for k in ("home", "draw", "away")]
            if all(dec):
                provider = ((odds.get("provider") or {}).get("name") or "espn").lower()
                priced += db.store_prices(mid, dec, f"espn:{provider}", overwrite=False)
    db.commit()
    log.info("ESPN fixtures %s: %d upcoming matches in the next %d days (%d priced from ESPN)", code, added, days, priced)


# ───────────────────────────── Source 3b: cup and European fixtures (rest days) ─────────────────────────────
# Non-league competitions our teams play, by ESPN slug. Teams are matched by ESPN team id, learned from
# each league's team list, so cup/European opponents with similar names can't be mismatched.
OTHER_COMPS = (
    "uefa.champions", "uefa.champions_qual", "uefa.europa", "uefa.europa_qual",
    "uefa.europa.conf", "uefa.europa.conf_qual", "uefa.super_cup",
    "eng.fa", "eng.league_cup", "esp.copa_del_rey", "esp.super_cup", "ita.coppa_italia", "ita.super_cup",
    "ger.dfb_pokal", "ger.super_cup", "fra.coupe_de_france", "fra.super_cup", "sco.tennents", "sco.cis",
)


def _has_table(db: DB, name: str) -> bool:
    return bool(db.q("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", name))


async def load_espn_team_ids(ctx: BrowserContext, db: DB, code: str, season: str) -> None:
    """Map ESPN team ids to our teams for one league-season (name_aliases source 'espn-id').
    Uses the league's October and March scoreboards: ESPN's team list endpoint ignores the
    season and only returns the current members."""
    cfg = LEAGUES[code]
    sid = db.one("SELECT season_id FROM seasons WHERE league_id=? AND label=?", db.league_id(code), season)
    if sid is None:
        return
    team_ids = {t for (t,) in db.q("SELECT home_team_id FROM matches WHERE season_id=? UNION "
                                   "SELECT away_team_id FROM matches WHERE season_id=?", sid, sid)}
    mapped = {tid for (tid,) in db.q("SELECT entity_id FROM name_aliases WHERE source='espn-id' AND entity_type='team'")}
    if team_ids <= mapped:
        return
    resolver = TeamResolver(db, team_ids)
    y = int(season[:4])
    for ym in (f"{y}10", f"{y + 1}03"):
        board = await fetch_json(ctx, f"{ESPN_BASE}/{cfg['espn_slug']}/scoreboard?dates={ym}&limit=200")
        for ev in (board or {}).get("events", []):
            for c in ev.get("competitions", [{}])[0].get("competitors", []):
                espn_id, name = str(c["team"]["id"]), c["team"]["displayName"]
                if db.alias_lookup("espn-id", "team", espn_id) is None:
                    if (tid := resolver.resolve(name, "espn")) is not None:
                        db.alias_save("espn-id", "team", espn_id, tid)
        db.commit()


async def load_other_fixtures(ctx: BrowserContext, db: DB, seasons: list[str], current: str) -> None:
    """Cup/European/super-cup fixtures for tracked teams into team_fixtures (migration 002)."""
    if not _has_table(db, "team_fixtures"):
        log.info("team_fixtures table missing (migration 002); skipping cup/European fixtures")
        return
    by_espn = {a: tid for a, tid in db.q("SELECT alias, entity_id FROM name_aliases "
                                          "WHERE source='espn-id' AND entity_type='team'")}
    done = {(c, s) for c, s in db.q("SELECT DISTINCT competition, season FROM team_fixtures")}
    horizon = (datetime.now(timezone.utc) + timedelta(days=31)).strftime("%Y-%m")
    added = 0
    for season in seasons:
        y = int(season[:4])
        months = [f"{y + (m < 7)}{m:02d}" for m in (7, 8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6)]
        months = [m for m in months if f"{m[:4]}-{m[4:]}" <= horizon]
        for comp in OTHER_COMPS:
            if season != current and (comp, season) in done:
                continue  # past seasons don't change
            for ym in months:
                board = await fetch_json(ctx, f"{ESPN_BASE}/{comp}/scoreboard?dates={ym}&limit=500")
                for ev in (board or {}).get("events", []):
                    try:
                        comps = ev["competitions"][0]["competitors"]
                        state = ev["status"]["type"]
                    except (KeyError, IndexError, TypeError):
                        continue
                    if state.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_ABANDONED"):
                        continue
                    status = "finished" if state.get("completed") else "scheduled"
                    ko = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).strftime(ISO)
                    for c in comps:
                        tid = by_espn.get(str(c["team"]["id"]))
                        if tid is None:
                            continue
                        opp = next((o["team"]["displayName"] for o in comps if o is not c), "")
                        db.con.execute(
                            """INSERT INTO team_fixtures (team_id, kickoff_utc, competition, season, is_home,
                                   opponent, status, espn_event) VALUES (?,?,?,?,?,?,?,?)
                               ON CONFLICT(team_id, kickoff_utc) DO UPDATE SET status=excluded.status,
                                   competition=excluded.competition, opponent=excluded.opponent""",
                            (tid, ko, comp, season, int(c.get("homeAway") == "home"), opp, status, str(ev["id"])))
                        added += 1
            db.commit()
    log.info("Cup/European fixtures: %d team rows written", added)


# ───────────────────────────── Source 4: referee appointments ─────────────────────────────
# Handles "Home v Away - Referee" (SPFL style) and "Home v Away" + "Referee: Name" on a following line (PL style)
FIXTURE_RE = re.compile(
    r"^(?P<home>[A-Z][\w.'& ]{1,40}?)\s+v(?:s\.?)?\s+(?P<away>[A-Z][\w.'& ]{1,40}?)"
    r"\s*(?:[-–—]\s*(?P<ref>.+))?$")
REF_LINE_RE = re.compile(r"^Referee\s*:\s*(?P<ref>.+)$", re.I)
REF_STOP_RE = re.compile(r"\s*[(\[.,;:]|\s+(?:Assistants?|Fourth|VAR|AVAR|live|kick)\b", re.I)


def parse_appointments(text: str) -> list[tuple[str, str, str]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = []
    for i, line in enumerate(lines):
        m = FIXTURE_RE.match(line) if len(line) <= 120 else None
        if not m:
            continue
        ref = m["ref"]
        if not ref:
            for nxt in lines[i + 1:i + 4]:
                if FIXTURE_RE.match(nxt):
                    break
                if r := REF_LINE_RE.match(nxt):
                    ref = r["ref"]
                    break
        if ref:
            ref = REF_STOP_RE.split(ref, maxsplit=1)[0].strip()
            if 2 <= len(ref.split()) <= 4:
                out.append((m["home"].strip(), m["away"].strip(), ref))
    return out


async def scrape_appointments(ctx: BrowserContext, db: DB, url: str, horizon_days: int = 14) -> None:
    page = await fetch_page(ctx, url, "body")
    try:
        text = await page.inner_text("body")
    finally:
        await page.close()
    found = parse_appointments(text)

    now = datetime.now(timezone.utc)
    upcoming = db.q(
        """SELECT m.match_id, m.home_team_id, m.away_team_id, l.country
           FROM matches m JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
           WHERE m.status = 'scheduled' AND m.kickoff_utc BETWEEN ? AND ?""",
        (now - timedelta(hours=12)).strftime(ISO), (now + timedelta(days=horizon_days)).strftime(ISO))
    by_pair = {(h, a): (mid, country) for mid, h, a, country in upcoming}
    resolver = TeamResolver(db, {t for _, h, a, _ in upcoming for t in (h, a)})
    source = urlparse(url).netloc

    matched = 0
    for home, away, ref in found:  # lines from other divisions simply fail to resolve
        h = resolver.resolve(home, source, warn=False)
        a = resolver.resolve(away, source, warn=False)
        if (hit := by_pair.get((h, a))) is None:
            continue
        mid, country = hit
        db.con.execute("UPDATE matches SET referee_id=? WHERE match_id=?",
                       (db.referee_id(ref, country, source), mid))
        matched += 1
    db.commit()
    log.info("Appointments %s: %d of %d parsed lines matched upcoming fixtures", source, matched, len(found))


# ───────────────────────────── Source 5: league referee feeds ─────────────────────────────
# Referees named before kickoff, read from the league bodies' own JSON every run (no weekly URL to find).
# ESPN only lists officials after the match, so without these every upcoming fixture used the league average.
PL_API = "https://sdp-prem-prod.premier-league-prod.pulselive.com/api"
SFA_API = "https://www.scottishfa.co.uk/api/comet/referee-assignments"
SFA_SKIP = re.compile(r"Development|Women|Reserve|Play-?off|\bU\d\d\b", re.I)
CLUB_SUFFIX = re.compile(r"\s+F\.?\s?C\.?$", re.I)


def assign_referees(db: DB, code: str, rows: list[tuple[str, str, datetime, str]], source: str,
                    now: datetime, days: int) -> int:
    """Set the appointed referee on scheduled fixtures. rows: (home, away, kickoff UTC, referee).
    A fixture is found by its two teams, or by kickoff time plus one team; in the second case the other
    team's spelling is saved to name_aliases so later runs resolve it directly."""
    lo, hi = now - timedelta(hours=12), now + timedelta(days=days)
    upcoming = db.q(
        """SELECT m.match_id, m.home_team_id, m.away_team_id, m.kickoff_utc FROM matches m
           JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
           WHERE l.code = ? AND m.status = 'scheduled' AND m.kickoff_utc BETWEEN ? AND ?""",
        code, (lo - timedelta(days=1)).strftime(ISO), (hi + timedelta(days=1)).strftime(ISO))
    by_pair = {(h, a): mid for mid, h, a, _ in upcoming}
    league_teams = db.q("""SELECT m.home_team_id, m.away_team_id FROM matches m JOIN seasons s USING (season_id)
                           JOIN leagues l USING (league_id) WHERE l.code = ? AND m.kickoff_utc > ?""",
                        code, (now - timedelta(days=200)).strftime(ISO))
    resolver = TeamResolver(db, {t for pair in league_teams for t in pair})  # postponed games still resolve
    matched = 0
    for home, away, ko, ref in rows:
        if not lo <= ko <= hi:
            continue
        home, away = CLUB_SUFFIX.sub("", home), CLUB_SUFFIX.sub("", away)
        h, a = resolver.resolve(home, source, warn=False), resolver.resolve(away, source, warn=False)
        mid = by_pair.get((h, a))
        if mid is None and (h or a):
            near = [(m, mh, ma) for m, mh, ma, k in upcoming
                    if abs((datetime.strptime(k, ISO).replace(tzinfo=timezone.utc) - ko).total_seconds()) <= 900
                    and (mh == h or ma == a)]
            if len(near) == 1:
                mid, mh, ma = near[0]
                for raw, got, tid in ((home, h, mh), (away, a, ma)):
                    if got is None:
                        db.alias_save(source, "team", raw, tid)
        if mid is None:
            if h is None or a is None:
                log.warning("Unmatched team %r from %s; add it to name_aliases", home if h is None else away, source)
            continue
        db.con.execute("UPDATE matches SET referee_id=? WHERE match_id=?",
                       (db.referee_id(ref, LEAGUES[code]["country"], source), mid))
        matched += 1
    db.commit()
    log.info("%s referees from %s: %d upcoming fixtures assigned", code, source, matched)
    return matched


def pl_referee(officials: dict | None) -> str | None:
    return next((o["official"]["name"] for o in (officials or {}).get("matchOfficials") or []
                 if o.get("type") == "Referee" and (o.get("official") or {}).get("name")), None)


async def load_pl_referees(ctx: BrowserContext, db: DB, season: str, days: int) -> None:
    now = datetime.now(timezone.utc)
    hi = now + timedelta(days=days)
    todo, cursor = [], ""
    while True:  # matches come sorted by kickoff; stop paging once past the horizon
        page = await fetch_json(ctx, f"{PL_API}/v2/matches?competition=8&season={season[:4]}&_limit=100"
                                     + (f"&_next={cursor}" if cursor else ""))
        if not page:
            raise RuntimeError("Premier League match list unavailable")
        data = page["data"]
        for m in data:
            ko = (datetime.strptime(m["kickoff"], "%Y-%m-%d %H:%M:%S")
                  .replace(tzinfo=ZoneInfo(m.get("kickoffTimezoneString") or "Europe/London")).astimezone(timezone.utc))
            if m.get("period") == "PreMatch" and ko <= hi:
                todo.append((m, ko))
        cursor = page["pagination"].get("_next")
        if not cursor or not data or ko > hi:
            break
    rows = []
    for m, ko in todo:
        if ref := pl_referee(await fetch_json(ctx, f"{PL_API}/v1/matches/{m['matchId']}/officials")):
            rows.append((m["homeTeam"]["name"], m["awayTeam"]["name"], ko, ref))
    assign_referees(db, "EPL", rows, "premierleague.com", now, days)


def sfa_competition_ids(comps: list[dict], season: str) -> list[int]:
    """Scottish Premiership competitions for a season, e.g. 'William Hill Premiership 2026/27 Regular Season'."""
    label = f"{season[:4]}/{season[5:]}"
    return [c["competitionId"] for c in comps
            if "Premiership" in c["competitionName"] and label in c["competitionName"]
            and not SFA_SKIP.search(c["competitionName"])]


def sfa_rows(results: list[dict]) -> list[tuple[str, str, datetime, str]]:
    rows = []
    for r in results:
        ref = next((o["personName"] for o in r.get("officials") or []
                    if o.get("role") == "Referee" and o.get("roleStatus", "CONFIRMED") == "CONFIRMED"), None)
        if ref:
            rows.append((r["homeTeamName"], r["awayTeamName"],
                         datetime.fromisoformat(r["matchDateUtc"]).astimezone(timezone.utc), ref))
    return rows


async def load_sfa_referees(ctx: BrowserContext, db: DB, season: str, days: int) -> None:
    comps = await fetch_json(ctx, f"{SFA_API}/competitions")
    ids = sfa_competition_ids(comps or [], season)
    if not ids:
        raise RuntimeError(f"no Scottish Premiership {season} competition in the Scottish FA appointments list")
    rows = []
    for cid in ids:
        page = 1
        while (d := await fetch_json(ctx, f"{SFA_API}?competitionId={cid}&page={page}&pageSize=100")):
            rows += sfa_rows(d["results"])
            if not d.get("hasNextPage"):
                break
            page += 1
    assign_referees(db, "SPL", rows, "scottishfa.co.uk", datetime.now(timezone.utc), days)


DFB_SCHEDULE = "https://datencenter.dfb.de/global_referee_schedule"
DFB_WHEN = re.compile(r"(\d\d)\.(\d\d)\.(\d{4})\s+(\d\d):(\d\d)")
TITLE_RE = re.compile(r"^(?:Dr\.|Prof\.)\s+")


def _text_lines(fragment: str) -> list[str]:
    text = re.sub(r"<img[^>]*>", "", fragment)
    return [ln for ln in (html.unescape(x).strip() for x in re.sub(r"<[^>]+>", "\n", text).splitlines()) if ln]


def parse_dfb_schedule(page: str) -> list[tuple[str, str, datetime, str]]:
    """Bundesliga rows from the DFB data centre's referee schedule (it also lists the lower leagues)."""
    rows = []
    for block in page.split('class="c-MatchTable"')[1:]:
        title = re.search(r'c-MatchTable-title">\s*([^<]+?)\s*<', block)
        if not title or html.unescape(title.group(1)).strip() != "Bundesliga":
            continue
        for row in block.split('class="c-MatchTable-row"')[1:]:
            lines = _text_lines(row.split('class="c-MatchTable-row"')[0])
            when = next((DFB_WHEN.search(ln) for ln in lines if DFB_WHEN.search(ln)), None)
            ref = next((ln[:-5] for ln in lines if ln.endswith(" (SR)")), None)
            score = next((i for i, ln in enumerate(lines) if re.fullmatch(r"[-\d]+\s*:\s*[-\d]+", ln)), None)
            if not (when and ref and score):
                continue
            d, mo, y, hh, mi = map(int, when.groups())
            ko = datetime(y, mo, d, hh, mi, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(timezone.utc)
            rows.append((lines[score - 1], lines[score + 1], ko, TITLE_RE.sub("", ref)))
    return rows


async def load_dfb_referees(ctx: BrowserContext, db: DB, season: str, days: int) -> None:
    page = await fetch_text(ctx, DFB_SCHEDULE)
    if not page:
        raise RuntimeError("DFB referee schedule unavailable")
    assign_referees(db, "BUNDES", parse_dfb_schedule(page), "dfb.de", datetime.now(timezone.utc), days)


SERIEA_API = "https://api-sdp.legaseriea.it/v1/serie-a/football"
SERIEA_COMPETITION = "serie-a::Football_Competition::ec93b94f74294dc98ab5bcfd67fc0d88"


def seriea_referee(facts: dict | None) -> str | None:
    for r in (facts or {}).get("referees") or []:
        if r.get("role") == "Referee":
            return r.get("shortName") or f"{r.get('mediaFirstName', '')} {r.get('mediaLastName', '')}".strip() or None
    return None


async def load_seriea_referees(ctx: BrowserContext, db: DB, season: str, days: int) -> None:
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(hours=12), now + timedelta(days=days)
    q = lambda s: quote(s, safe="")  # noqa: E731
    seasons = await fetch_json(ctx, f"{SERIEA_API}/competitions/{q(SERIEA_COMPETITION)}/seasons?locale=en-GB")
    name = f"{season[:4]}/{int(season[:4]) + 1}"
    sid = next((s["seasonId"] for s in (seasons or {}).get("seasons", []) if s.get("seasonName") == name), None)
    if sid is None:
        raise RuntimeError(f"no Serie A season {name} in the Lega Serie A API")
    days_ = await fetch_json(ctx, f"{SERIEA_API}/seasons/{q(sid)}/matchdays?locale=en-GB")
    rows = []
    for md in (days_ or {}).get("matchdays", []):
        start, end = md.get("startDateUtc"), md.get("endDateUtc")
        if not start or not end or datetime.fromisoformat(start.replace("Z", "+00:00")) > hi \
                or datetime.fromisoformat(end.replace("Z", "+00:00")) + timedelta(days=1) < lo:
            continue
        listing = await fetch_json(ctx, f"{SERIEA_API}/seasons/{q(sid)}/matches?matchDayId={q(md['matchSetId'])}"
                                        "&locale=en-GB")
        for m in (listing or {}).get("matches", []):
            ko = datetime.fromisoformat(m["matchDateUtc"].replace("Z", "+00:00"))
            if not lo <= ko <= hi or m.get("status") != "UPCOMING":
                continue
            facts = await fetch_json(ctx, f"{SERIEA_API}/seasons/{q(sid)}/match/{q(m['matchId'])}/matchfacts"
                                          "?locale=en-GB")
            if ref := seriea_referee(facts):
                rows.append((m["home"]["mediaName"], m["away"]["mediaName"], ko, ref))
    assign_referees(db, "SERIEA", rows, "legaseriea.it", now, days)


LIGUE1_API = "https://ma-api.ligue1.fr"


def ligue1_referee(match: dict | None) -> str | None:
    """Officials type 1 is the referee; the feed capitalises surnames ("Jérémy STINAT")."""
    name = next((o.get("name") for o in (match or {}).get("officials") or [] if o.get("type") == 1), None)
    return " ".join(w.capitalize() if w.isupper() else w for w in name.split()) if name else None


async def load_ligue1_referees(ctx: BrowserContext, db: DB, season: str, days: int) -> None:
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(hours=12), now + timedelta(days=days)
    played = db.one("""SELECT COUNT(*) FROM matches m JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
                       WHERE l.code = 'LIGUE1' AND s.label = ? AND m.status = 'finished'""", season) or 0
    rows = []
    for week in range(max(1, played // 9), max(1, played // 9) + 4):  # 9 matches a round; stops past the horizon
        listing = await fetch_json(ctx, f"{LIGUE1_API}/championship-matches/championship/1/game-week/{week}")
        matches = (listing or {}).get("matches", [])
        for m in matches:
            ko = datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
            if lo <= ko <= hi and m.get("period") == "preMatch":
                if ref := ligue1_referee(await fetch_json(ctx, f"{LIGUE1_API}/championship-match/{m['matchId']}")):
                    rows.append((m["home"]["clubIdentity"]["officialName"], m["away"]["clubIdentity"]["officialName"],
                                 ko, ref))
        if matches and min(datetime.fromisoformat(m["date"].replace("Z", "+00:00")) for m in matches) > hi:
            break
    assign_referees(db, "LIGUE1", rows, "ligue1.fr", now, days)


LALIGA_API = "https://apim.laliga.com/public-service/api/v1"
LALIGA_KEY = "c13c3a8e2f6b46da9c5c425cf61fab3e"  # public key laliga.com itself sends with every page request


def laliga_referee(match: dict | None) -> str | None:
    """persons_role role 5 is the referee. The RFEF names them the day before the match."""
    return next((p["person"]["name"] for p in (match or {}).get("persons_role") or []
                 if (p.get("role") or {}).get("id") == 5 and (p.get("person") or {}).get("name")), None)


async def load_laliga_referees(ctx: BrowserContext, db: DB, season: str, days: int) -> None:
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(hours=12), now + timedelta(days=days)
    played = db.one("""SELECT COUNT(*) FROM matches m JOIN seasons s USING (season_id) JOIN leagues l USING (league_id)
                       WHERE l.code = 'LALIGA' AND s.label = ? AND m.status = 'finished'""", season) or 0
    key = f"contentLanguage=en&countryCode=GB&subscription-key={LALIGA_KEY}"
    rows = []
    for week in range(max(1, played // 10), max(1, played // 10) + 4):  # 10 matches a round
        listing = await fetch_json(ctx, f"{LALIGA_API}/matches?subscriptionSlug=laliga-easports-{season[:4]}"
                                        f"&week={week}&limit=20&{key}")
        matches = (listing or {}).get("matches", [])
        for m in matches:
            ko = datetime.fromisoformat(m["date"])
            if lo <= ko <= hi and m.get("status") == "PreMatch":
                detail = await fetch_json(ctx, f"{LALIGA_API}/matches/{m['slug']}?{key}")
                if ref := laliga_referee((detail or {}).get("match")):
                    rows.append((m["home_team"]["nickname"], m["away_team"]["nickname"], ko, ref))
        if matches and min(datetime.fromisoformat(m["date"]) for m in matches) > hi:
            break
    assign_referees(db, "LALIGA", rows, "laliga.com", now, days)


# ───────────────────────────── Orchestration ─────────────────────────────
async def _gather_logged(label: str, coros) -> None:
    for res in await asyncio.gather(*coros, return_exceptions=True):
        if isinstance(res, Exception):
            log.error("%s step failed: %r", label, res)


async def run(args: argparse.Namespace) -> None:
    db = DB(args.db, args.init_schema)
    seasons = [shift_season(args.season, -n) for n in range(args.history, -1, -1)]
    async with Stealth().use_async(async_playwright()) as p:
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        browser = await p.chromium.launch(headless=not args.headed,
                                          proxy={"server": proxy} if proxy else None)
        ctx = await browser.new_context(locale="en-GB", timezone_id="Europe/London",
                                        viewport={"width": 1440, "height": 900})
        await ctx.route("**/*", _block_heavy)
        try:
            # Order matters: results and fixtures create the matches the other sources attach to.
            await _gather_logged("football-data",
                                 (load_results(ctx, db, c, s) for c in args.leagues for s in seasons))
            await _gather_logged("Fixtures", [load_upcoming(ctx, db, args.leagues, args.season)])
            mark_stale(db)
            if not args.no_espn:
                await _gather_logged("ESPN",
                                     (backfill_referees(ctx, db, c, s) for c in args.leagues for s in seasons))
            if not args.no_espn:
                await _gather_logged("ESPN red cards",
                                     (split_reds(ctx, db, c, s) for c in args.leagues for s in seasons))
                await _gather_logged("ESPN team ids",
                                     (load_espn_team_ids(ctx, db, c, s) for c in args.leagues for s in seasons))
                await _gather_logged("ESPN fixtures",
                                     (load_espn_fixtures(ctx, db, c, args.season, args.days) for c in args.leagues))
            if not args.no_espn and not args.no_cups:
                await _gather_logged("Cup/European fixtures",
                                     [load_other_fixtures(ctx, db, seasons, args.season)])
            feeds = {"EPL": load_pl_referees, "SPL": load_sfa_referees, "BUNDES": load_dfb_referees,
                     "SERIEA": load_seriea_referees, "LIGUE1": load_ligue1_referees, "LALIGA": load_laliga_referees}
            await _gather_logged("Referee feeds", (feeds[c](ctx, db, args.season, args.days)
                                                   for c in args.leagues if c in feeds))
            if args.ref_url:
                await _gather_logged("Appointments", (scrape_appointments(ctx, db, u) for u in args.ref_url))
            settle(db.con)  # pending bets on matches whose cards just arrived
        finally:
            await browser.close()
            db.con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect EPL/SPL fixtures, cards, fouls and referees")
    ap.add_argument("--db", default="cards.db")
    ap.add_argument("--init-schema", metavar="SQL_FILE", help="create tables if the DB is empty")
    ap.add_argument("--season", required=True, help="e.g. 2026-27")
    ap.add_argument("--history", type=int, default=2, metavar="N",
                    help="also load the N previous seasons (lookback for team/referee averages)")
    ap.add_argument("--no-espn", action="store_true", help="skip all ESPN steps (referees, cup/European fixtures)")
    ap.add_argument("--no-cups", action="store_true", help="skip the cup/European fixture step")
    ap.add_argument("--days", type=int, default=10, help="how far ahead to load ESPN league fixtures")
    ap.add_argument("--leagues", nargs="+", default=list(LEAGUES), choices=list(LEAGUES))
    ap.add_argument("--ref-url", action="append", default=[], help="referee appointment page (repeatable)")
    ap.add_argument("--headed", action="store_true", help="visible browser; helps clear JS challenges")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
