# Card-market value betting analyzer

EPL, La Liga, Serie A, Bundesliga, Ligue 1 and Scottish Premiership yellow/red card markets
(league codes EPL, LALIGA, SERIEA, BUNDES, LIGUE1, SPL). SQLite DB: `cards.db`, schema in `card_value_schema.sql`.

## Pipeline (run in order)
1. Collect data:
   `python collect_cards.py --db cards.db --season 2026-27 --ref-url <appointments page>`
   - First run only: add `--init-schema card_value_schema.sql`
   - Loads the 2 previous seasons too (`--history N`) for lookback averages
   - Loads ESPN league fixtures for the next 10 days (`--days N`), with ESPN's bookmaker 1X2 line as
     model context until football-data publishes its market average
   - Add `--headed` if an appointments page shows a bot challenge
   - Settles pending bets automatically at the end of every run (`python settle.py --db cards.db` to run it alone)
2. Find value:
   `python value_finder.py --db cards.db --json`
   - Add `--dry-run` to evaluate without writing to `value_bets`
3. Price sheet for the next 7 days: `python price_sheet.py --db cards.db` (`--json` available)

## Saturday agent
- A scheduled routine runs `agent/SATURDAY.md` each Saturday morning: updates data, settles bets,
  prices the day's matches, researches team news and writes `reports/<date>.md` with a shortlist of
  minimum acceptable odds. It never places bets.

## Model
- Default `--model glm` (`glm_model.py`): team-level card regression with team discipline, opponent
  provocation, referee and home effects (shrunk toward the league average, 120-day recency half-life)
  plus pre-match 1X2 market odds as match context. Needs the `match_prices` table (migration 001).
- `--model pcr`: the original three-way average of home team, away team and referee.
- Backtests: `python glm_model.py --db cards.db --backtest` (GLM vs PCR, walk-forward) and
  `python backtest.py --db cards.db` (PCR diagnostics). Keep a change only if it wins on seasons it
  wasn't tuned on.

## Migrations
- `migrations/001_match_prices.sql` — applied to `cards.db` (pre-match 1X2 market averages).
- `migrations/002_team_fixtures.sql` — NOT applied. Cup/European fixtures for rest-day features
  (`glm_model.py --rest`); backtest showed no gain on match totals. The collector step is a no-op
  until it's applied.

## Rules
- Never place bets. You may record bets and stakes the user reports, via `price_sheet.py --log-bet`
  (stake defaults to 1 unit; odds decimal or fractional):
  - match total: `"Home v Away over 4.5 @ 2.20 bet365 £10"`
  - one team: `"Home v Away: Rangers over 2.5 @ 5/2 Sky Bet £10"`
  - each team: `"Home v Away: each team 2+ @ 15/8 Sky Bet £10"`
  - research-prompt line: `"Home v Away | market | over 3.5 @ 2.15 | Sky Bet | model p | adjusted p | reason"`
  - past bets: add `--placed "YYYY-MM-DD HH:MM"` (UK time); also `--adjusted-p 0.45 --reason "..."`.
  Bets that `value_finder.py` logs itself stay paper bets with stake 0.
- Track record: `python settle.py --db cards.db --report` (user's bets vs model view vs paper bets).
- Don't change the schema without asking; propose migrations as new `.sql` files.
- Don't lower the per-host throttles in `HOST_INTERVAL`.
- Fix unmatched team or referee names by inserting rows into `name_aliases`, not by editing code.
- `odds_snapshots`, `bookmakers` and `markets` have no collector yet; ask before adding an odds source.

## Reporting
After a run, summarise flagged bets: fixture, referee, bookmaker, market, line, odds,
model vs fair probability, EV, and the PCR components.
