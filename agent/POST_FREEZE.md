# Post-freeze test list

**Freeze lifted 22 Nov 2026 (user's decision).** glm-v4 judged on 283 scored matches of `data/projections.csv`:
live log-loss -2.112 against -2.111 in the backtest for the same matches, RMSE 2.03 against 2.05, so the live
model performs as backtested; it runs 0.21 cards a match low (3.99 against 4.20). Weekend results of 21-22 Nov
were not yet on football-data; re-score when they are. glm-v4.1 stays live while the tests below run.

Each test: walk-forward on 2024-27 (seasons not tuned on), against the current model on the same matches.
Pass bar (user, 11 Nov): +0.004 log-loss per match; a pass is logged alongside glm-v4.1, not swapped in, until
a confirmation read on fresh matches.

## Results

- **Test 3, fast league level: FAIL** (22 Nov, `review/test3_league_level.py`). A 30-day, 30-match-prior
  league correction on top of the fit: +0.0017 a match on 2024-27 (95% CI -0.0000 to +0.0035), positive in
  2024-25 only (+0.0052), slightly negative in 2025-26 and 2026-27; +0.0037 on the tuned seasons. No setting
  in the sensitivity grid reaches the bar (best +0.0023). The walk-forward 2026-27 league biases are small
  (EPL +0.26, La Liga +0.12, Serie A -0.31); the live log's larger gaps (La Liga +0.58, EPL +0.53 on 50
  matches each, standard error about 0.28) are mostly noise.
- **Test 1, curved underdog term: FAIL** (22 Nov, `review/test1_curved_underdog.py`). gap x |gap| added to
  the 1X2 terms: match totals -0.0000 a match on 2024-27 (CI -0.0001 to +0.0000), team rows -0.0004; gap cubed
  the same. Heavy underdogs (own win probability under 20%, 1,619 team rows) are not over-projected: actual
  minus projected -0.03 cards, the same as every other band (-0.03 to -0.07). The shortlist caution on heavy
  underdogs' long team lines has no calibration basis in the backtest; whether to keep it is a rule question
  for the user, not a model change.
- **Test 2, team-level dispersion: FAIL** (22 Nov, `review/test2_team_dispersion.py`). A, a shared gamma
  match effect (negative multinomial, shape = the match-total NB size): joint (home, away) log-loss +0.0004 a
  match on 2024-27 (CI -0.0006 to +0.0015), team rows -0.0017. B, NB per team: identical to Poisson, because
  team rows show no overdispersion around the model in any league (method of moments finds none). What the
  calibration does show: home and away residuals correlate +0.17, so the each-team market is under-projected
  (each team 2+: 0.363 projected v 0.396 actual; 1+: 0.745 v 0.776; top fifth of each-team 2+: 0.504 v 0.539),
  while long single-team lines are over-projected (one team 4+: 0.162 v 0.141). That supports the shortlist
  cautions on long team lines, and says each-team prices are conservative. Candidate for a new, pre-specified
  test 2b: Poisson marginals with positive dependence (e.g. a common shock or a copula), which A could not give
  without widening the marginals.
  Parked, then reopened on the user's point that few each-team bets reflects where we looked, not the market.
- **Test 2b, common-shock bivariate Poisson: PASS** (22 Nov, `review/test2b_each_team.py`, form and settings
  fixed before running). Each team stays Poisson(mu); a shared Poisson part l3 = rho x sqrt(mu_h x mu_a) with
  rho per league from the training residuals (2024-27 means: EPL 0.19, La Liga 0.17, Bundesliga 0.15, SPL 0.09,
  Ligue 1 0.08, Serie A 0.06). Joint (home, away) log-loss +0.0127 a match on 2024-27 (CI +0.0090 to +0.0165),
  positive every season (+0.0159, +0.0106, +0.0090), +0.0129 on the tuned seasons. Each team 2+ gap to actual
  +0.033 -> +0.015 (halved, the second check); each team 3+ 0.118 -> 0.136 v 0.131 actual; single-team lines
  and match totals unchanged by construction. Each team 2+ probability rises 1.8 points on average (0.6-3.0).
  Per the rules: log alongside glm-v4.1, not swapped in, until a confirmation read on fresh matches.

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
