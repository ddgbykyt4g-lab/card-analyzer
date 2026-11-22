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
   - Splits red cards into second yellows and straight reds from ESPN match events; `yellows_1h`/`reds_1h`
     mark checked teams. football-data counts a two-yellow dismissal as one red, and its yellow count holds
     neither of that player's yellows (England to 2025-26), the first only, or both (the four continental
     leagues; England from 2026-27). `two_yellow_split` picks whichever matches ESPN's yellow events, so
     stored yellows always include the first yellow once. `--resplit` (with `--history N`) redoes every split
   - Writes ESPN's player-card counts for matches finished in the last 14 days to `data/espn_cards.csv`
     (keyed on ESPN's event id; staff cards counted apart). football-data is the model's card source, but
     where its count for a match in that file differs from ESPN's, ESPN's is stored (`apply_espn_counts`, every
     run after football-data reloads): not for a difference its two-yellow counting explains, nor while a staked
     bet on the match is pending (that stays a `CHECK` until confirmed)
   - Settles pending bets automatically at the end of every run, voiding bets on matches not played
     within 3 days of the original kickoff date (`python settle.py --db cards.db` to run it alone)
2. Find value:
   `python value_finder.py --db cards.db --json`
   - Add `--dry-run` to evaluate without writing to `value_bets`
3. Price sheet for the next 7 days: `python price_sheet.py --db cards.db` (`--json` available)

## Two card sources
football-data and ESPN disagree on about 3% of team card counts, and either can be wrong (football-data had
the wrong yellows for all four Premier League matches of 7 Nov 2026, which settled a winning bet as lost).
On 7 Nov 2026 football-data's card columns slid one match down in 18 matches across five leagues (fouls were
right); ESPN was right in all 18, so from 2026-11-11 the database takes ESPN's count where the two differ.
- `settle.py` checks every bet with a stake against `data/espn_cards.csv`: same result from both sources,
  it settles; different results, the bet stays pending and is listed as `CHECK` until the user says what the
  bookmaker paid (`python settle.py --db cards.db --confirm BET_ID won|lost|push|void`); only ESPN has the
  match yet (football-data lags a day or two), it settles on ESPN's count and is listed later if
  football-data differs. Paper bets settle on football-data alone. A confirmed bet is never touched again.
- `agent/health.py` reports those bets, and any league where the sources differ in 4 or more matches from
  the last 10 days (one or two is normal). The daily workflow sends a "Bet needs checking" phone alert.
- `python agent/espn_match.py "Home v Away" [--lineups]`: ESPN's line-ups and running player-card count
  (a booked manager is listed apart: it doesn't count for Sky Bet). Read-only, for checking a bet by hand.

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
  reason), loading appointed referees first (`collect_cards.py --referees-only`) so the bet is priced with them, refreshes `reports/latest.md`, pushes and confirms by phone alert.
  The bet is first committed to `data/bet_queue/` by a job outside the lock, then logged by one inside it
  (`agent/bet_queue.py`): GitHub cancels a waiting run when a newer one arrives, and the newer run logs both.
  With no placed time, the time it was queued is used. Both workflows share the
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
  `glm-v4.1` (from 2026-11-10) is the same model on corrected card data: two-yellow dismissals had been stored
  2 cards too high outside England (and in England from 2026-27), about 6% of matches. Existing rows are only
  corrected by a one-off `collect_cards.py --history 5 --resplit`; the normal collector keeps rows it has split.

## Tests
- `python -m pytest -q tests` (bet parsing, odds/push maths, settlement, void rules, the two card sources,
  price-check verdicts); runs on GitHub on
  every push that touches Python. Add a test with any change to parsing, pricing or settlement.

## Model
- **Model freeze lifted 22 Nov 2026** (it ran from 2026-10-06): glm-v4 judged on 283 scored matches, live
  log-loss equal to the backtest's. Candidate changes run in order from `agent/POST_FREEZE.md`, each walk-forward
  on 2024-27 against the current model; keep one only at +0.004 log-loss a match or better, logged alongside
  the live model until a confirmation read. Results are recorded there.
- **Each-team link (post-freeze test 2b, passed 22 Nov 2026; in shadow):** home and away cards move together
  (residual correlation about +0.17). `glm_model.team_link` estimates each league's link (rho) at fit time,
  `each_team_over` prices "each team N+" with a common-shock bivariate Poisson (each team still Poisson(mu)).
  Logged as the `rho` column of `data/projections.csv`; `check_prices.py` shows the 2b price and verdict beside
  the live (independent) one for `each` blocks; `score_projections.py` reports the confirmation read (pass:
  300+ matches with rho, joint log-loss gain above 0, each team 2+ gap to actual narrower). Live prices,
  `--log-bet` and settlement are unchanged until it passes and the user agrees.
- Default `--model glm` (`glm_model.py`): team-level card regression with team discipline, opponent
  provocation, referee and home effects (shrunk toward the league average, 120-day recency half-life)
  plus pre-match 1X2 market odds as match context, and team foul rates (fouls committed and drawn,
  pre-match, shrunk toward the league; `--no-fouls` to drop). Needs the `match_prices` table (migration 001).
- Tried and rejected out-of-sample: rest days, referee foul rate, interaction terms (league x home,
  league x odds, referee x venue, referee x closeness, team x venue, provocation x venue); and after the freeze
  (22 Nov 2026, `agent/POST_FREEZE.md`, scripts in `review/test*.py`): a fast league level (test 3), a curved
  underdog term (1), team-level overdispersion (2), referee card-per-foul conversion x team fouls (5), match
  stakes as features (6b; dead rubbers run low, a possible April caution). The derby flag (6a) is parked on the
  `test6-derby` branch: derbies are judged on the day.
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
- Real money (user's decision, 11 Nov 2026): £10 flat per bet, following the current-rule verdict of
  `check_prices.py` (model value +8%); the blend rule is scored as paper bets for experiment A. Revisit if the
  experiment passes or fails.
- Sky Bet card markets (total, home, away, each team) settle with `settlement_rules` yellow 1, second
  yellow 1, straight red 1, no staff cards (two yellows = 2, yellow then straight red = 2). Pricing refits
  the GLM in the bookmaker's card units; `price_sheet.py` prices for Sky Bet by default (`--book NAME`).
- Track record: `python settle.py --db cards.db --report` (user's bets vs model view vs paper bets).
- Checking logged prices: `python agent/check_prices.py --db cards.db data/prices/<file>` prints value, the
  minimum odds and a verdict per line for matches not yet kicked off, applying the shortlist rules of
  `agent/SATURDAY.md` step 5 (overs +8%, no tail bets under 40%, long team lines, heavy underdogs, no unders
  for now). It reads only; the user decides and places.
- Experiment A (from 2026-11-10, plan and pass marks in `review/EXPERIMENT_A.md`; background in `REVIEW.md`):
  `check_prices.py` also prints the model blended 50/50 with Sky Bet's de-vigged price and a second "blend
  rule" verdict (blended value +2%). The current-rule verdict is unchanged. `python review/experiment_a.py pick`
  lists the week's benchmark matches (about a quarter of fixtures, fixed by a hash of the ESPN id, chosen
  without the model); `... score` reports blend against Sky Bet. Don't change `BLEND_W`, `BLEND_EV`, the
  start date or the benchmark rule while it runs: a change is a new experiment.
  `... remind` lists screenshots due in the next 48 hours (benchmark matches with no two-sided Sky Bet total price,
  closing screenshots for pending bets); the daily run sends it as a "Screenshots needed" alert when non-empty, the
  Saturday alert appends it, and each "Bet logged" alert reminds about the closing screenshot.
  Amended 11 Nov 2026, before any benchmark match was scored: closing-line value is measured against Sky Bet's
  own closing price (a screenshot of the bet's line in the 30 minutes before kickoff, `sky_clv`), not Betfair's;
  the Betfair logger (PR #1) is shelved until an edge is shown.
- Don't change the schema without asking; propose migrations as new `.sql` files.
- Don't lower the per-host throttles in `HOST_INTERVAL`.
- Fix unmatched team or referee names by inserting rows into `name_aliases`, not by editing code.
- `odds_snapshots`, `bookmakers` and `markets` have no automatic collector; ask before adding an odds source.
  Prices the user sends as screenshots are transcribed into `data/prices/<YYYY-MM>.txt` (blocks `total`, `home`,
  `away`, or `each` for "each team N+" screens, rows like `2+ 15/8`) and loaded with
  `python agent/log_prices.py --db cards.db data/prices/<file>` (UK times; re-running is harmless).

## Reporting
After a run, summarise flagged bets: fixture, referee, bookmaker, market, line, odds,
model vs fair probability, EV, and the PCR components.
