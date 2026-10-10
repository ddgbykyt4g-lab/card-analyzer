# Card-market value betting analyzer

EPL, La Liga, Serie A, Bundesliga, Ligue 1 and Scottish Premiership yellow/red card markets
(league codes EPL, LALIGA, SERIEA, BUNDES, LIGUE1, SPL). SQLite DB: `cards.db`, schema in `card_value_schema.sql`.

## Pipeline (run in order)
1. Collect data:
   `python collect_cards.py --db cards.db --season 2026-27 --ref-url <appointments page>`
   - First run only: add `--init-schema card_value_schema.sql`
   - Loads the 2 previous seasons too (`--history N`) for lookback averages
   - Loads ESPN league fixtures for the next 10 days (`--days N`), with ESPN's bookmaker 1X2 line as
     model context until football-data publishes its market average. Fixtures are matched on ESPN's
     event id (`external_id` 'espn-fx:<id>', 'espn:<id>' once referee-checked), so a moved fixture
     updates its row
   - Loads appointed referees for upcoming fixtures every run from each league's own feed (ESPN lists
     officials only after the match): Premier League API, Scottish FA appointments API, DFB data centre
     referee schedule, Lega Serie A API, Ligue 1 API, LaLiga API. Usually published days ahead; La Liga's
     the day before each match (by 16:00 Madrid). Until then a match uses the league-average referee
   - Add `--headed` if an appointments page shows a bot challenge
   - Splits red cards into second yellows and straight reds from ESPN match events (football-data counts
     a two-yellow dismissal as one red and drops its first yellow); `yellows_1h`/`reds_1h` mark checked teams
   - Settles pending bets automatically at the end of every run, voiding bets on matches not played
     within 3 days of the original kickoff date (`python settle.py --db cards.db` to run it alone)
2. Find value:
   `python value_finder.py --db cards.db --json`
   - Add `--dry-run` to evaluate without writing to `value_bets`
3. Price sheet for the next 7 days: `python price_sheet.py --db cards.db` (`--json` available)

## Saturday agent
- A scheduled routine runs `agent/SATURDAY.md` each Saturday morning: updates data, settles bets,
  prices the day's matches, researches team news and writes `reports/<date>.md` with a shortlist of
  minimum acceptable odds. It never places bets.
  It then sends a phone alert with `agent/notify.sh` (ntfy.sh; private topic in the `NTFY_TOPIC`
  environment variable, never written to files).

## Free Saturday run (GitHub Actions)
- `.github/workflows/saturday.yml` runs each Saturday at 07:17 UK (06:17 in winter) without Claude:
  collector, settle, price sheet saved to `reports/<date>-prices.md`, push, then a phone alert built by
  `agent/free_summary.py` (overs at +8% EV). No team-news research. Needs the `NTFY_TOPIC` repo secret;
  run it by hand from the Actions tab (`workflow_dispatch`). It also writes the phone dashboard
  `reports/latest.md` (`agent/dashboard.py`).
- `.github/workflows/log-bet.yml`: logs a bet from the GitHub app (inputs: bet, placed, adjusted_p,
  reason), loading appointed referees first (`collect_cards.py --referees-only`) so the bet is priced with them, refreshes `reports/latest.md`, pushes and confirms by phone alert. Both workflows share the
  `cards-db` concurrency group so they never push `cards.db` at the same time.

- `.github/workflows/projections.yml` (Sunday-Friday 09:47 UK) and the Saturday run append the model's
  pre-kickoff projection for every match in the next 2 days to `data/projections.csv` (`agent/log_projections.py`:
  model version, card weights, expected cards, pmf 0-20). It pushes that file, and cards.db only when a bet settled. Score it with
  `python agent/score_projections.py cards.db data/projections.csv` (latest projection before kickoff per match).

- `agent/health.py` checks the data after each Saturday collect (results missing for recent matches,
  source errors in the collector log, bets stuck pending, matches about to kick off with no referee: 48 hours ahead, La Liga 18). Any problem changes the phone alert title to
  "DATA PROBLEM, check first" and puts a warning at the top of `reports/latest.md`.

- Referees: football-data gives them for EPL only; SPL and the other four leagues get them from ESPN match
  summaries (available from about April 2022, `ESPN_FIRST_MONTH`). The 2022-26 gap was backfilled once
  with `agent/backfill_referees.py` (CSV kept on the `referee-backfill` branch); out-of-sample it improved
  every untuned season (all-league RMSE 2.221 -> 2.206). Bets priced after this carry model `glm-v3`; with fouls, `glm-v4`.

## Tests
- `python -m pytest -q tests` (bet parsing, odds/push maths, settlement, void rules); runs on GitHub on
  every push that touches Python. Add a test with any change to parsing, pricing or settlement.

## Model
- **Model freeze (from 2026-10-06):** no model changes (features, priors, half-life, dispersion, units) until
  `data/projections.csv` has a few hundred scored matches (about 300) and glm-v4 has been judged on them with
  `agent/score_projections.py`. Bug fixes in data collection are fine; anything that changes projections waits.
- Default `--model glm` (`glm_model.py`): team-level card regression with team discipline, opponent
  provocation, referee and home effects (shrunk toward the league average, 120-day recency half-life)
  plus pre-match 1X2 market odds as match context, and team foul rates (fouls committed and drawn,
  pre-match, shrunk toward the league; `--no-fouls` to drop). Needs the `match_prices` table (migration 001).
- Tried and rejected out-of-sample: rest days, referee foul rate, interaction terms (league x home,
  league x odds, referee x venue, referee x closeness, team x venue, provocation x venue).
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
- Sky Bet card markets (total, home, away, each team) settle with `settlement_rules` yellow 1, second
  yellow 1, straight red 1, no staff cards (two yellows = 2, yellow then straight red = 2). Pricing refits
  the GLM in the bookmaker's card units; `price_sheet.py` prices for Sky Bet by default (`--book NAME`).
- Track record: `python settle.py --db cards.db --report` (user's bets vs model view vs paper bets).
- Don't change the schema without asking; propose migrations as new `.sql` files.
- Don't lower the per-host throttles in `HOST_INTERVAL`.
- Fix unmatched team or referee names by inserting rows into `name_aliases`, not by editing code.
- `odds_snapshots`, `bookmakers` and `markets` have no collector yet; ask before adding an odds source.

## Reporting
After a run, summarise flagged bets: fixture, referee, bookmaker, market, line, odds,
model vs fair probability, EV, and the PCR components.
