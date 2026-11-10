# Post-freeze test list

The model is frozen (glm-v4) until `data/projections.csv` has about 300 scored matches (early December 2026)
and glm-v4 has been judged on them with `agent/score_projections.py`. Nothing here changes projections before
then. After that, test each item in order with `python glm_model.py --db cards.db --backtest` and keep it only
if it wins on seasons it wasn't tuned on (CLAUDE.md).

Already tried and rejected out-of-sample: rest days, referee foul rate, the interaction terms listed in
CLAUDE.md.

## Evidence so far

- **Live log level (to 17 Oct):** 53 scored matches averaged 4.28 cards against 3.90 projected. The season
  started low (Aug–Oct 2026 3.86 a match against 4.29 a year earlier), October rebounded to 4.28; the model
  lags both moves by about a month (120-day half-life on every term).
- **Bet #10, Nott'm Forest 2+ v Arsenal (18 Oct, lost):** model 2.48 Forest cards, Sky Bet about 1.7. Forest
  committed 14 fouls (as the model's foul term expected) with 26% possession but got 1 card (referee M Oliver).
  Card-prone Sangaré and Yates were benched; the price drifted 6/10 → 4/6 after the line-ups. Forest's 2.5 rested
  on six 2026-27 games (1.58 a match in 2025-26).
- **Bet #9, Sevilla 3+ at Real Madrid (18 Oct, lost):** model 3.22 Sevilla cards, Sky Bet about 2.7. Sevilla
  committed 14 fouls, got 2 cards; 0–3 down by 63' (lost 1–4), so the game was dead for the last half-hour.
- **17 Oct shortlist:** Sky Bet priced all four shortlisted matches 0.5–1.4 cards above the model; actual
  cards across 11 finished matches averaged 5.1 against 4.0 projected.

- **Card data fix (10 Nov, glm-v4.1):** two-yellow dismissals were stored 2 cards too high in La Liga, Serie A,
  Bundesliga and Ligue 1 (every season) and in the EPL from 2026-27: football-data's yellow count already
  holds both yellows there, and the collector added one back. 582 team rows, about 6% of matches; three-season
  averages fall 0.11-0.15 a match in the four continental leagues. A data bug fix, not a model change.
  Walk-forward backtest (2024-27), both fits scored against the corrected outcomes: RMSE 2.062 -> 2.054,
  log-loss -2.1153 -> -2.1109, a small real gain. (Scored against their own data the figures are 2.202 -> 2.054,
  but most of that drop is the outcomes losing their +2 outliers, not a better model; don't quote it as a gain.)
  The `glm-v4.1` label is only true once `collect_cards.py --resplit` has run on main: the normal collector
  keeps rows it has already split, so merging the code alone fixes nothing in `cards.db`. Rows logged before
  the resplit carry `glm-v4`, after it `glm-v4.1`; both are scored against the corrected counts.
- **Live log by league (to 9 Nov, 283 scored matches, corrected counts):** 4.19 cards against 3.99 projected,
  but not evenly: La Liga +0.62 a match (50 matches), EPL +0.51 (50), SPL +0.26, Bundesliga and Ligue 1 +0.12,
  Serie A -0.29 (60). Those projections were fitted on the inflated continental data, so from glm-v4.1 the
  continental projections drop about 0.1-0.15 and their gaps widen by as much (Serie A's narrows). By projected
  level the gap sits in the 3.5-4.5 band; under 3.5 is about right. `agent/score_projections.py` now prints
  both tables. Points at test 3 (fast league level), not a flat lift.
- **Two card sources:** football-data and ESPN differ on about 3% of team counts. Where they differ the model
  still trains on football-data. Whether to prefer ESPN's count on those rows is a question for the review:
  compare the two against Sky Bet's settlements in `value_bets` notes ("confirmed by user").
- **Bet #13, Liverpool v Arsenal over 4.5 (1 Nov, won):** no cards in the first half on 8 fouls, 5 after it.
  A referee's leniency in top-of-the-table games isn't in the model (only his 108-match average).
- **Prices move toward the model:** Sunderland v Chelsea over 4.5 was 21/20 at 08:09 on 8 Nov (+9%) and 20/23
  by 13:00 (-1%); the match had 9 cards. Sky Bet's two-sided card markets otherwise sit on or above the model
  on Sundays and for derbies (Athletic v Real Sociedad priced 1.4 cards above it).

## Tests, in order

1. **Curved underdog term.** The win-probability gap enters linearly, so big underdogs get ever more cards.
   Add a curvature term (gap × |gap|, or a spline on own win probability) and check whether heavy underdogs
   (own win probability < 20%) are over-projected in the backtest. Data: already in `cards.db`.
2. **Team-level dispersion.** Team and each-team lines are priced with independent Poisson counts. Fit a
   negative binomial per team row (and a shared match effect for the each-team market) and compare log-loss
   and calibration on team lines 1.5–3.5. Data: already in `cards.db`.
3. **Fast league level.** A separate short half-life (about 30 days) on the league intercepts so season-wide
   shifts are picked up within weeks, while team and referee effects keep 120 days. Judge on the live log's
   mean bias as well as the backtest. Data: already in `cards.db`.
4. **Early-season shrinkage.** Stronger pull of team effects toward the long-run level in the first ~10 matches
   of a season (or a season-boundary prior), so six games can't move a team by most of a card.
5. **Referee card-per-foul conversion.** Each referee's cards per foul (shrunk), interacting with the teams'
   foul rates. Distinct from the rejected referee foul-rate term. Data: fouls already stored.
6. **Derby flag and match stakes.** A small fixed derby list; stakes from league tables built from stored
   results (relegation, title, Europe, final 8 rounds).
7. **Player level.** Card propensity per player summed over the confirmed line-up. Needs a player and
   line-up source; ask before adding any new data source.

## Betting cautions until then

Applied in `agent/SATURDAY.md` step 5: be wary of heavy underdogs' card lines, check team lines' card-prone
players start, treat team-line edges above about +20% on 3+ lines as a warning, pass on overs the model gives
under 40%, and back no unders while the live log runs above the model. `agent/check_prices.py` applies them.
