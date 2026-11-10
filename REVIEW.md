# Model review: is there a real, exploitable edge?

Reviewed 10 November 2026 against `main` after PR #2 (card data corrected, model label `glm-v4.1`).
Nothing in the model, the data or the existing code was changed. The two scripts that produced every number
below are in `review/`; their full output is in `review/out/`.

**Reading the labels:** *Measured* = a number a script printed. *Inferred* = my reading of those numbers.
*Not verifiable* = the repo doesn't hold what would be needed.

## Verdict: inconclusive

**An edge over Sky Bet is not evidenced. It is also not ruled out.** The model is a sound, honestly validated
forecaster of card counts. What is missing is any sign that it forecasts better than the bookmaker.

| Question | Answer | Numbers (measured) |
|---|---|---|
| Does the model predict cards better than a naive league average? | **Yes** | Log-loss better by 0.035 per match (95% CI 0.027 to 0.042) on 4,534 walk-forward matches, 2024-27. RMSE 2.054 against 2.122. |
| Is it free of look-ahead? | **Yes** | Scrambling all future results, cards, fouls and prices changed no prediction (largest change 1.7e-13 cards). |
| Is it calibrated? | **In the backtest, yes. Live this season, it runs low.** | Backtest calibration slopes 0.93 to 1.06. Live log (283 matches): 3.99 projected against 4.19 actual; slope 0.73 at over 4.5. |
| Does it beat Sky Bet's own prices? | **No evidence** | 194 two-sided lines on 29 matches: log-loss model −0.594, de-vigged market −0.584. Difference −0.010 (95% CI −0.066 to +0.034). Best blend weight on the model: 0 (CI 0 to 1). |
| Does it beat the closing line? | **Not verifiable** | No closing price is stored for any bet. Three lines were captured twice; two moved the model's way. |
| Were the 15 real bets luck? | **Not pure luck at fair prices, but far better than the model itself predicts** | 13 of 15 won, ROI +90.7% (bootstrap 95% CI +43% to +133%). Chance of 13+ wins: 0.26% if prices were fair, 0.10% with Sky Bet's margin, and only 0.61% even if the model were exactly right. The model expected +£12.32 of the £140.34 profit. |
| Do the betting rules work mechanically? | **Too few bets to say** | Applied to every logged price: 7 bets, 5 won, ROI +41% (95% CI −26% to +104%). |

**What I infer from this.** The profit is real money, but it cannot be credited to the model. Three things
point the same way:
1. On the same lines, Sky Bet's prices scored at least as well as the model's.
2. The 15 bets won far more often than the model's own probabilities allow (8.0 wins expected, 13 landed),
   so most of the surplus is variance, or your judgement on line-ups, not the model's edge.
3. Overs in general landed more often than either the model or Sky Bet expected in the matches you
   screenshotted (17 of 29, against 13.9 and 14.2 expected). That is not significant (16% chance under Sky
   Bet's prices), but backing overs blindly there would also have shown a profit.

At an 8% true edge and these odds, ROI alone needs about 700 bets before a 95% interval excludes zero
(about 1,400 for 80% power). At 15 bets a season-and-a-bit, profit will not settle this. Closing-line value
and probability scoring against the market will, in roughly 100 to 150 priced matches.

## Phase 1: what the model does

**In one paragraph.** For each team in each match the model predicts a number of cards. It is a Poisson
regression with a log link: the expected count is a league baseline, multiplied up or down by a home/away
factor, how card-prone the team is, how many cards its opponent tends to draw, the referee, how one-sided
the bookmakers think the match is, and how much the two teams foul. The two teams' expected counts are added
for the match total. Probabilities for "over 4.5" and so on are read off the count distribution, and a bet is
flagged when the bookmaker's price is longer than the model's fair price by a set margin.

| Part | What the repo does | Where |
|---|---|---|
| Data | football-data.co.uk season CSVs: results, yellow and red cards, fouls, Premier League referees, 1X2 market-average odds. ESPN public JSON: fixtures, other leagues' referees, which reds were second yellows, a second card count for cross-checks. Each league's referee appointment feed. | `collect_cards.py` |
| Scope | 6 leagues, 2021-22 to 2026-27, 10,623 finished matches. Cards counted the way Sky Bet settles (yellow 1, second yellow 1, straight red 1). | `cards.db` |
| Odds for betting | Sky Bet card prices typed in from your screenshots: 415 pre-kickoff price rows on 48 matches. No automatic feed, no historical card odds. | `agent/log_prices.py`, `odds_snapshots` |
| Model family | Poisson, log link, one row per team per match. Fitted by penalised maximum likelihood (Newton steps). | `glm_model.py` `CardGLM` |
| Features | League intercept; home indicator; a "discipline" effect per team; a "provocation" effect per opponent; an effect per referee; three from the 1X2 odds (win-probability gap, its absolute value, draw probability); two from fouls (the team's fouls committed and the opponent's fouls drawn, each relative to the league, from earlier matches only). | `glm_model.py` `Data`, `foul_features` |
| Regularisation | Ridge penalty pulling team effects (strength 60) and referee effects (100) toward the league average. | `GLMConfig` |
| Time decay | Each past match is weighted by 0.5^(age / 120 days). | `GLMConfig.half_life_days` |
| Match total | Sum of the two team means, priced as negative binomial with a per-league dispersion fitted by moments. In practice it is Poisson in five of six leagues (no excess variance found). | `CardGLM.fit` |
| Team lines | Each team's count priced as an independent Poisson. | `price_sheet.py` |
| Training and validation | Refit on every match before the prediction date. Backtest is walk-forward: refit each week on earlier matches only. Half-life and penalties were tuned on 2022-24; 2024-27 is reported as out-of-sample. | `glm_model.py` `walk_forward` |
| Live record | Every projection is written to a log before kickoff and scored afterwards. | `data/projections.csv`, `agent/score_projections.py` |
| Bet decision | Minimum acceptable odds: overs need +8% expected value, unders +5%; plus cautions (no tail bets under 40%, long team lines, heavy underdogs, no unders for now). You decide and place. | `agent/SATURDAY.md`, `agent/check_prices.py` |
| Staking | Flat £10 by hand. A Kelly fraction is computed in `value_finder.py` but nothing uses it. | `value_finder.py` |
| Betting backtest | **There isn't one.** The backtests score probabilities against results. No historical card odds exist in the repo, so no historical profit can be simulated. | — |

## Phase 2: audit

### 1. Leakage and look-ahead: none found (measured)

- **Scramble test.** For four random weeks I replaced every later result, card count, foul count and 1X2
  price with noise and re-ran the walk-forward for that week. The projections did not move (largest change
  1.7e-13 cards, the same as an unscrambled refit). A second variant scrambled the test week's own cards and
  fouls; first-day matches did not move either.
- **1X2 odds.** The model uses football-data's `AvgH/AvgD/AvgA`, the pre-match market average, not the
  closing columns (`AvgCH...`). Live projections made before football-data posts a match use ESPN's line.
- **Live against backtest, same 283 matches.** Projections correlate 0.990. Log-loss: live −2.1323,
  backtest −2.1283; difference +0.0039 (95% CI −0.0037 to +0.0111). A leak would make the backtest clearly
  better; it doesn't.
- **No scaling fitted on all data.** Foul rates use earlier days only; the dispersion comes from training
  residuals.

*Not verifiable:* what time of day football-data samples its "average" odds. If it is close to kickoff, the
backtest's odds features are slightly better informed than a morning-of-match projection.

### 2. Validation: chronological and walk-forward, but the test seasons have been reused (measured + inferred)

| Period | Matches | Log-loss | League-average baseline | RMSE | Projected v actual |
|---|---|---|---|---|---|
| 2022-24 (tuned on) | 4,034 | −2.1519 | −2.1930 | 2.135 | 4.33 v 4.31 |
| 2024-27 (reported out-of-sample) | 4,534 | −2.1109 | −2.1457 | 2.054 | 4.14 v 4.06 |

- The split is strictly chronological, with a weekly rolling origin.
- The half-life was not lucky: 60, 120, 240 and 480 days give 2024-27 log-loss −2.1174, −2.1109, −2.1079,
  −2.1095. The curve is flat.
- **Inferred weakness:** the foul features, the referee backfill and the rejected interaction terms were all
  kept or dropped on their 2024-27 results (`CLAUDE.md`, `glm_model.py` comments). After roughly ten such
  decisions those seasons are no longer untouched. The only clean sample is the live log (283 matches).
  The model's gain over the baseline shrinks from +0.037 (2024-25) to +0.024 (2026-27, 575 matches).

### 3. Model specification: adequate for match totals, weaker for team lines (measured)

- **Dispersion, 2024-27 out-of-sample.** Match totals: 1.017 (1.00 is Poisson), so Poisson is fine there.
  Per team: 0.878, *under*-dispersed. The two teams' residuals in a match correlate +0.166
  (95% CI +0.138 to +0.194): a heated match books both sides.
- **Consequence for team lines.** Independent Poisson spreads each team's count too wide and ignores the
  link between the teams:
  - Team over 3.5 ("4 or more"): model 16.2%, observed 14.1%. The model overrates long team lines.
  - Each team 2+: model 36.3%, observed 39.6%. The product rule underrates it.
  - Team over 1.5: model 60.1%, observed 61.1%, calibration slope 1.10 (slightly under-confident).
- **Match-total tails.** Zero-card matches: 3.0% observed, 2.0% predicted. Over 5.5: 24.4% predicted, 22.8%
  observed; over 6.5: 13.7% against 12.5%. The model's upper tail is a point or so too fat in the backtest.
- **Home advantage.** Home teams get 0.931 times the cards, other things equal. Sensible.
- **Shrinkage is heavy.** Team multipliers span only 0.93 to 1.07 (5th to 95th percentile), referees 0.96
  to 1.04. Most of the model's discrimination comes from league, odds and fouls.
- **Multicollinearity.** `|gap|` and draw probability correlate −0.91 (VIF 5.8 and 5.9). This doesn't hurt
  predictions, but those two coefficients can't be read separately.
- **Dixon-Coles-style correction.** The analogue here is the positive home/away dependence above. It
  matters for team and each-team markets, not for totals.

### 4. Calibration (measured)

Chart: `review/out/reliability.png`.

| Sample | Log-loss | Brier (lines 2.5-6.5) | Over 4.5: predicted v observed | Slope at 4.5 |
|---|---|---|---|---|
| Walk-forward 2024-27 (4,534) | −2.1109 | 0.1811 | 39.5% v 38.6% | 0.98 |
| Live log (283) | −2.1323 | 0.1916 | 36.7% v 40.6% | 0.73 |

The backtest is well calibrated. The live log runs about 0.2 cards low this season, concentrated in the
Premier League and La Liga (`agent/POST_FREEZE.md`). With 283 matches the live slope is noisy, but the level
shortfall is consistent across lines 2.5 to 6.5.

### 5. Against the market (measured)

- **De-vigging method:** proportional (each side's 1/odds divided by their sum). The power method is shown
  as a check and scores worse on the full sample. Sky Bet's margin on two-sided card lines is 8.9% (220 lines).
- **Sample:** the latest pre-kickoff price of each match/market/line, settled, with both prices and a market
  probability between 10% and 90%. Model probability is the projection logged before the price was captured
  (394 of 399 lines; 5 reconstructed). Outcomes use ESPN's count where it differs from football-data
  (20 matches, mostly football-data's faulty 7 November rows).

| Lines | n lines / matches | Model log-loss | Market log-loss | Model minus market (95% CI) |
|---|---|---|---|---|
| All | 194 / 29 | −0.5943 | −0.5839 | −0.0104 (−0.066 to +0.034) |
| Match totals | 94 / 16 | −0.6457 | −0.6218 | −0.0239 (−0.115 to +0.048) |
| Team lines | 100 / 20 | −0.5461 | −0.5483 | +0.0022 (−0.027 to +0.036) |
| One line per match and market | 39 / 29 | −0.6700 | −0.6519 | −0.0181 (−0.072 to +0.031) |

- **Closing-line value: not verifiable.** `value_bets.closing_odds` is empty for all 15 bets. The Betfair
  logger that would fill it (PR #1) is not live.
- **Caveat (inferred):** you screenshotted matches the model already liked, so this sample is tilted toward
  disagreements. It is also small: the interval is wide enough to hide a modest edge either way.

### 6. Backtest realism (measured, with limits)

- **Real bets.** 15 bets, 13 won, £154.81 staked, +£140.34, ROI +90.7%. Average odds 2.14. Max drawdown £20.
  Every price was the one on your screen, so it was available.
- **Luck test.** See the verdict table. The result is very unlikely under fair prices, and almost as
  unlikely under the model's own probabilities.
- **Rules applied mechanically to all logged prices:**

| Rule | Bets / matches | Won | ROI (95% CI, resampling matches) |
|---|---|---|---|
| Full rule set (`check_prices.py`) | 7 / 7 | 5 | +41% (−26% to +104%) |
| Every over showing +8% (no cautions) | 69 / 18 | 18 | −3% (−67% to +66%) |
| Every under showing +5% | 18 / 7 | 3 | −60% (−100% to +22%) |
| Main over line of each match, model ignored | 29 / 29 | 17 | +11% (−25% to +46%) |

- **Inferred:** the cautions matter. The model's largest "edges" sit on long lines, and those lost; the
  no-unders rule also looks right. Neither is statistically established.
- *Not verifiable:* stake limits, account restrictions, whether a price survived the minutes between
  screenshot and bet, and whether every bet you placed was reported.

### 7. Staking (measured on 7 rule bets; the growth table is inferred)

- **Current method:** flat £10. On a balance of £100 to £134 that is 7% to 10% of the bank per bet. The
  Kelly fractions the model implied on the 15 real bets average 10%, so the stakes are already close to
  full Kelly on the model's own claims. That is large for an edge that isn't yet evidenced.
- **Kelly on the model's probabilities** would have staked up to 27% of the bank on a real bet and 33% on a
  rule bet.

Expected growth per bet (%, log scale), by how much of the model's claimed edge is real:

| Staking | 0% real | 25% | 50% | 100% |
|---|---|---|---|---|
| Flat 2% of bank | −0.18 | −0.07 | +0.04 | +0.26 |
| Full Kelly, model probability | −2.84 | −1.78 | −0.72 | +1.39 |
| Half Kelly | −1.02 | −0.51 | 0.00 | +1.03 |
| Quarter Kelly | −0.42 | −0.17 | +0.09 | +0.59 |
| Half Kelly on a 50/50 model-market blend | −0.18 | −0.05 | +0.07 | +0.32 |

Full or half Kelly loses money unless at least half the claimed edge is real. Small flat stakes, or Kelly on
a blend with the market, lose little if the edge is absent and still grow if it is present.

## Issues, ranked by impact on the verdict

| # | Issue | Fix | Effort | Expected effect |
|---|---|---|---|---|
| 1 | **No market benchmark of useful size and no closing prices.** The verdict can't move without them. | Finish PR #1 (Betfair key and UK runner) so every bet gets a closing price. Screenshot Sky Bet for a fixed list of matches each week, including ones the model doesn't like. | You: 1-2 hours setup, then 10 minutes a week. Me: none. | Decides the question within about 100-150 priced matches (one to two months). |
| 2 | **Profit is the wrong yardstick at this sample size.** 15 bets; about 700 needed. | Judge on closing-line value and on log-loss against the de-vigged price, both reported monthly by `review/audit_market.py`. | None: the script exists. | Stops a hot or cold run being mistaken for evidence. |
| 3 | **The model ignores the card market, which is at least as accurate.** Its +8% "value" may be mostly its own error. | Shrink the model's probability toward the de-vigged price before computing value (experiment A). | Small: a few lines in `check_prices.py`. Changes which bets are flagged, so it needs your approval. | Fewer bets, with the survivors more likely to be real disagreements. |
| 4 | **Level drift.** Live projections run 0.2 cards low (Premier League +0.5, La Liga +0.6, Serie A −0.3). | Fast-moving league level (already test 3 in `agent/POST_FREEZE.md`; experiment B). | Medium. Changes projections: after the freeze, with your approval. | Removes most of the live shortfall if it holds out-of-sample. |
| 5 | **Team lines use independent Poisson.** Long team lines are overrated (4+: 16.2% v 14.1%); each-team is underrated (36.3% v 39.6%). | Per-team dispersion and a shared match effect (experiment B). | Medium. Changes team-line prices: needs approval. | Corrects exactly the lines where the model shows its biggest apparent edges. |
| 6 | **Test seasons reused for feature selection.** The 2024-27 backtest is mildly optimistic. | Pre-register each future test (hypothesis, seasons, pass mark) before running it; treat the live log as the only clean sample. | Small: a template in `agent/POST_FREEZE.md`. | Honest effect sizes from here on. |
| 7 | **Stakes are large for an unproven edge** (7-10% of bank). | Flat 2% of bank, or Kelly on a model-market blend, until issue 1 is answered. | None. Your decision. | Cuts drawdown risk sharply; costs little if the edge is real. |
| 8 | **Bets involve your judgement**, so the record can't validate the model alone. | Keep logging every rule-qualified price as a paper bet (`check_prices.py` verdict), whether or not you take it. | Small. | A mechanical record that grows about three times faster than the real one. |
| 9 | **One card source can be wrong** (18 matches on 7 November). | Done in PR #2 for settlement. Using ESPN's count in training where sources differ is still open. | Small. Changes training data: needs approval. | Removes known-bad rows from the fit. |
| 10 | `\|gap\|` and draw probability are collinear (VIF 5.8). | Drop one, or leave. | Trivial. | None on predictions. |

## Proposed experiments

Each one states its pass mark before it is run. None has been started.

**A. Blend with the market (most likely to help).**
- *What:* value is computed on `w × model + (1 − w) × de-vigged Sky Bet` (on the logit scale), with `w`
  fixed in advance at 0.5, then re-estimated once 150 priced matches exist.
- *Why:* today's best estimate of `w` is 0, with an interval of 0 to 1. The market carries information the
  model lacks (line-ups, derbies, the current season's card level).
- *Success:* on at least 150 new priced matches, the blend's log-loss beats the market's with a 95% interval
  (resampling matches) above zero, **and** mean closing-line value against Betfair is positive. If the
  interval still spans zero at 300 matches, conclude there is no usable edge at Sky Bet's margin.

**B. Fast league level plus dependent team counts.**
- *What:* a short half-life (about 30 days) on the league intercepts only, and a shared match effect with
  per-team dispersion for team and each-team lines.
- *Why:* issues 4 and 5. Both are measured mis-specifications, not guesses.
- *Success:* walk-forward 2024-27 log-loss better by at least 0.005 per match with the interval above zero;
  live-log bias within ±0.1 cards; team over 3.5 and each-team 2+ within one point of observed. Judged on
  seasons not used to choose the half-life.

**C. Gradient-boosted comparison (a check, not a replacement).**
- *What:* a boosted-tree Poisson model on the same inputs, same walk-forward.
- *Why:* tests whether the linear form is leaving signal behind. My expectation is a small gain or none,
  since every interaction term tried so far was rejected.
- *Success:* log-loss better than the GLM by 0.005 or more on 2024-27 with the interval above zero. If not,
  stop looking for gains in model form and put the effort into A.
- *Needs:* one new Python dependency (LightGBM or scikit-learn). I'd ask before adding it.

## Reproducing this

```
python review/audit_model.py  cards.db data/projections.csv
python review/audit_market.py cards.db data/projections.csv data/espn_cards.csv
```
Both only read. The first takes about two minutes, the second a few seconds.
