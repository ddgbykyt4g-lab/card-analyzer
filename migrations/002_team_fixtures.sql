-- Proposed migration 002: non-league fixtures (cups, European competitions, super cups) for
-- tracked teams, so rest days and fixture congestion can be computed across all competitions.
-- League matches stay in `matches`; this only holds the rest. Source: ESPN competition scoreboards,
-- matched to teams by ESPN team id (name_aliases rows with source 'espn-id').
-- Apply with: sqlite3 cards.db < migrations/002_team_fixtures.sql
CREATE TABLE IF NOT EXISTS team_fixtures (
    team_id      INTEGER NOT NULL REFERENCES teams(team_id),
    kickoff_utc  TEXT NOT NULL,                -- ISO-8601 UTC
    competition  TEXT NOT NULL,                -- ESPN slug, e.g. 'uefa.champions', 'eng.fa'
    season       TEXT NOT NULL,                -- '2026-27'
    is_home      INTEGER NOT NULL CHECK (is_home IN (0,1)),
    opponent     TEXT NOT NULL,                -- as ESPN names it
    status       TEXT NOT NULL,                -- 'finished' or 'scheduled'
    espn_event   TEXT NOT NULL,
    PRIMARY KEY (team_id, kickoff_utc)
) STRICT;
CREATE INDEX IF NOT EXISTS ix_team_fixtures_kickoff ON team_fixtures(kickoff_utc);
