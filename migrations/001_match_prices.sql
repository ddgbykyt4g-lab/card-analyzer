-- Proposed migration 001: pre-match 1X2 prices per match (model feature, not a card-odds source)
-- Source: football-data.co.uk market-average odds (AvgH/AvgD/AvgA), captured before closing,
-- in both the season CSVs and fixtures.csv. Probabilities are normalised to remove the margin.
-- Apply with: sqlite3 cards.db < migrations/001_match_prices.sql
CREATE TABLE IF NOT EXISTS match_prices (
    match_id    INTEGER PRIMARY KEY REFERENCES matches(match_id) ON DELETE CASCADE,
    source      TEXT NOT NULL,                 -- 'football-data:avg'
    home_odds   REAL NOT NULL CHECK (home_odds > 1),
    draw_odds   REAL NOT NULL CHECK (draw_odds > 1),
    away_odds   REAL NOT NULL CHECK (away_odds > 1),
    p_home      REAL NOT NULL,                 -- 1/odds normalised to sum to 1
    p_draw      REAL NOT NULL,
    p_away      REAL NOT NULL
) STRICT;
