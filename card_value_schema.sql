-- Card-market value betting analyzer — SQLite schema
-- Requires SQLite >= 3.37 (STRICT tables)
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ─────────────────────────── Reference ───────────────────────────
CREATE TABLE leagues (
    league_id   INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    country     TEXT NOT NULL
) STRICT;

CREATE TABLE seasons (
    season_id   INTEGER PRIMARY KEY,
    league_id   INTEGER NOT NULL REFERENCES leagues(league_id),
    label       TEXT NOT NULL,                      -- '2026-27'
    start_date  TEXT NOT NULL,
    end_date    TEXT,
    UNIQUE (league_id, label)
) STRICT;

-- No league_id on teams: promotion/relegation means league comes from the match's season
CREATE TABLE teams (
    team_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    country     TEXT NOT NULL,
    UNIQUE (name, country)
) STRICT;

CREATE TABLE referees (
    referee_id  INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    country     TEXT NOT NULL,
    UNIQUE (name, country)
) STRICT;

CREATE TABLE bookmakers (
    bookmaker_id INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    is_sharp     INTEGER NOT NULL DEFAULT 0 CHECK (is_sharp IN (0,1))  -- reference price for CLV
) STRICT;

-- Maps each feed's / bookmaker's spelling to canonical IDs ("Man Utd" vs "Manchester United")
CREATE TABLE name_aliases (
    source      TEXT NOT NULL,                      -- 'fbref', 'bet365', 'oddsapi', ...
    entity_type TEXT NOT NULL CHECK (entity_type IN ('team','referee')),
    alias       TEXT NOT NULL,
    entity_id   INTEGER NOT NULL,
    PRIMARY KEY (source, entity_type, alias)
) STRICT;

-- ─────────────────────────── Matches ───────────────────────────
CREATE TABLE matches (
    match_id      INTEGER PRIMARY KEY,
    season_id     INTEGER NOT NULL REFERENCES seasons(season_id),
    matchweek     INTEGER,
    kickoff_utc   TEXT NOT NULL,                    -- ISO-8601 UTC
    home_team_id  INTEGER NOT NULL REFERENCES teams(team_id),
    away_team_id  INTEGER NOT NULL REFERENCES teams(team_id),
    referee_id    INTEGER REFERENCES referees(referee_id),  -- NULL until appointment announced
    status        TEXT NOT NULL DEFAULT 'scheduled'
                  CHECK (status IN ('scheduled','live','finished','postponed','abandoned')),
    home_goals    INTEGER,
    away_goals    INTEGER,
    is_derby      INTEGER NOT NULL DEFAULT 0 CHECK (is_derby IN (0,1)),
    external_id   TEXT,
    CHECK (home_team_id <> away_team_id),
    -- kickoff in key: SPL pairs can meet twice at the same venue in one season
    UNIQUE (season_id, home_team_id, away_team_id, kickoff_utc)
) STRICT;
CREATE INDEX ix_matches_referee ON matches(referee_id, kickoff_utc);
CREATE INDEX ix_matches_kickoff ON matches(kickoff_utc);

-- One row per team per match. Raw components so any bookmaker's settlement rule can be derived.
CREATE TABLE match_team_cards (
    match_id        INTEGER NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    is_home         INTEGER NOT NULL CHECK (is_home IN (0,1)),
    yellows         INTEGER NOT NULL DEFAULT 0,     -- first yellows (incl. first of a 2YC)
    second_yellows  INTEGER NOT NULL DEFAULT 0,     -- second yellow -> sending off
    straight_reds   INTEGER NOT NULL DEFAULT 0,
    staff_cards     INTEGER NOT NULL DEFAULT 0,     -- manager/bench; usually excluded from settlement
    yellows_1h      INTEGER,                        -- for first-half markets
    reds_1h         INTEGER,
    fouls           INTEGER,
    PRIMARY KEY (match_id, team_id)
) STRICT;

-- ─────────────────────── Referee averages ───────────────────────
-- Point-in-time snapshots. Backtests for a match on date D must only use rows with as_of_date < D.
CREATE TABLE referee_stats (
    stat_id           INTEGER PRIMARY KEY,
    referee_id        INTEGER NOT NULL REFERENCES referees(referee_id),
    league_id         INTEGER REFERENCES leagues(league_id),   -- NULL = all competitions
    season_id         INTEGER REFERENCES seasons(season_id),   -- NULL = career / rolling
    stat_window       TEXT NOT NULL CHECK (stat_window IN ('season','career','last10','last20')),
    as_of_date        TEXT NOT NULL,
    n_matches         INTEGER NOT NULL,
    avg_yellows       REAL NOT NULL,
    avg_reds          REAL NOT NULL,
    avg_home_yellows  REAL,
    avg_away_yellows  REAL,
    avg_booking_pts   REAL,
    avg_fouls         REAL,
    source            TEXT NOT NULL                -- 'computed', 'fbref', ...
) STRICT;
CREATE UNIQUE INDEX ux_referee_stats ON referee_stats(
    referee_id, COALESCE(league_id, 0), COALESCE(season_id, 0), stat_window, as_of_date, source);

-- ─────────────────────── Markets & odds ───────────────────────
CREATE TABLE markets (
    market_id   INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,              -- 'TOTAL_CARDS', 'TOTAL_BP', 'HOME_CARDS', '1H_CARDS'
    unit        TEXT NOT NULL CHECK (unit IN ('cards','booking_points')),
    scope       TEXT NOT NULL CHECK (scope IN ('match','home','away')),
    period      TEXT NOT NULL DEFAULT 'FT' CHECK (period IN ('FT','1H','2H'))
) STRICT;

-- Bookmakers count cards differently; weights let you settle each book's market from raw components
CREATE TABLE settlement_rules (
    bookmaker_id     INTEGER NOT NULL REFERENCES bookmakers(bookmaker_id),
    market_id        INTEGER NOT NULL REFERENCES markets(market_id),
    w_yellow         REAL NOT NULL,                -- e.g. 1 (cards) or 10 (points)
    w_second_yellow  REAL NOT NULL,                -- value of the 2YC dismissal itself
    w_straight_red   REAL NOT NULL,                -- e.g. 2 or 25
    counts_staff     INTEGER NOT NULL DEFAULT 0 CHECK (counts_staff IN (0,1)),
    PRIMARY KEY (bookmaker_id, market_id)
) STRICT;

-- Append-only line history; insert a row only when line or price changes
CREATE TABLE odds_snapshots (
    snapshot_id   INTEGER PRIMARY KEY,
    match_id      INTEGER NOT NULL REFERENCES matches(match_id),
    bookmaker_id  INTEGER NOT NULL REFERENCES bookmakers(bookmaker_id),
    market_id     INTEGER NOT NULL REFERENCES markets(market_id),
    line          REAL NOT NULL,                   -- 4.5, 45.5, 4.25 ...
    over_odds     REAL CHECK (over_odds > 1),
    under_odds    REAL CHECK (under_odds > 1),
    captured_at   TEXT NOT NULL,                   -- ISO-8601 UTC
    is_closing    INTEGER NOT NULL DEFAULT 0 CHECK (is_closing IN (0,1)),
    UNIQUE (match_id, bookmaker_id, market_id, line, captured_at)
) STRICT;
CREATE INDEX ix_odds_lookup ON odds_snapshots(match_id, market_id, bookmaker_id, captured_at);

-- ─────────────────────────── Value bets ───────────────────────────
CREATE TABLE value_bets (
    bet_id          INTEGER PRIMARY KEY,
    snapshot_id     INTEGER NOT NULL REFERENCES odds_snapshots(snapshot_id),  -- match/book/market/line
    selection       TEXT NOT NULL CHECK (selection IN ('over','under')),
    model_version   TEXT NOT NULL,
    expected_mean   REAL,                          -- model's projected cards / points
    model_prob      REAL NOT NULL CHECK (model_prob BETWEEN 0 AND 1),
    model_push_prob REAL NOT NULL DEFAULT 0,       -- non-zero on whole-number lines
    taken_odds      REAL NOT NULL CHECK (taken_odds > 1),
    edge            REAL GENERATED ALWAYS AS (model_prob * taken_odds + model_push_prob - 1) VIRTUAL,
    stake           REAL NOT NULL DEFAULT 0,       -- 0 = paper bet
    kelly_fraction  REAL,
    placed_at       TEXT NOT NULL,
    closing_odds    REAL,
    clv             REAL,                          -- taken_odds / no-vig closing odds - 1
    result          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (result IN ('pending','won','lost','push','half_won','half_lost','void')),
    settled_value   REAL,                          -- actual total under this book's rule
    profit          REAL,
    notes           TEXT
) STRICT;
CREATE INDEX ix_bets_result ON value_bets(result, placed_at);

-- ─────────────────────────── Views ───────────────────────────
CREATE VIEW v_match_totals AS
SELECT m.match_id, m.kickoff_utc, m.referee_id, s.league_id,
       SUM(c.yellows + c.second_yellows)                          AS yellows_shown,
       SUM(c.second_yellows + c.straight_reds)                    AS reds,
       SUM(10*c.yellows + 25*(c.second_yellows + c.straight_reds)) AS booking_pts_std
FROM matches m
JOIN seasons s USING (season_id)
JOIN match_team_cards c USING (match_id)
WHERE m.status = 'finished'
GROUP BY m.match_id;

CREATE VIEW v_referee_current AS
SELECT referee_id, league_id, COUNT(*) AS n_matches,
       AVG(yellows_shown) AS avg_yellows, AVG(reds) AS avg_reds, AVG(booking_pts_std) AS avg_booking_pts
FROM v_match_totals
WHERE referee_id IS NOT NULL
GROUP BY referee_id, league_id;

-- ─────────────────────────── Seed ───────────────────────────
INSERT INTO leagues (code, name, country) VALUES
    ('EPL',    'Premier League',       'England'),
    ('LALIGA', 'La Liga',              'Spain'),
    ('SERIEA', 'Serie A',              'Italy'),
    ('BUNDES', 'Bundesliga',           'Germany'),
    ('LIGUE1', 'Ligue 1',              'France'),
    ('SPL',    'Scottish Premiership', 'Scotland');
