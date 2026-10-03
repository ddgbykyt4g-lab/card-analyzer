# Card-market value betting analyzer

EPL, La Liga, Serie A, Bundesliga, Ligue 1 and Scottish Premiership yellow/red card markets
(league codes EPL, LALIGA, SERIEA, BUNDES, LIGUE1, SPL). SQLite DB: `cards.db`, schema in `card_value_schema.sql`.

## Pipeline (run in order)
1. Collect data:
   `python collect_cards.py --db cards.db --season 2026-27 --ref-url <appointments page>`
   - First run only: add `--init-schema card_value_schema.sql`
   - Loads the 2 previous seasons too (`--history N`) for lookback averages
   - Add `--headed` if an appointments page shows a bot challenge
   - Settles pending bets automatically at the end of every run (`python settle.py --db cards.db` to run it alone)
2. Find value:
   `python value_finder.py --db cards.db --json`
   - Add `--dry-run` to evaluate without writing to `value_bets`
3. Price sheet for the next 7 days: `python price_sheet.py --db cards.db` (`--json` available)

## Rules
- Never place bets. You may record bets and stakes the user reports, via
  `price_sheet.py --log-bet "Home v Away over 4.5 @ 2.20 bet365 £10"` (stake defaults to 1 unit).
  Bets that `value_finder.py` logs itself stay paper bets with stake 0.
- Don't change the schema without asking; propose migrations as new `.sql` files.
- Don't lower the per-host throttles in `HOST_INTERVAL`.
- Fix unmatched team or referee names by inserting rows into `name_aliases`, not by editing code.
- `odds_snapshots`, `bookmakers` and `markets` have no collector yet; ask before adding an odds source.

## Reporting
After a run, summarise flagged bets: fixture, referee, bookmaker, market, line, odds,
model vs fair probability, EV, and the PCR components.
