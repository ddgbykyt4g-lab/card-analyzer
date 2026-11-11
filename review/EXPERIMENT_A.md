# Experiment A: blend the model with the bookmaker's own price

Written 10 November 2026, before any of the data it will be judged on existed. Nothing below may be changed
after seeing results; a change means a new experiment with a new start date.

## Question

`REVIEW.md` found no evidence that the model's probabilities beat Sky Bet's. Does a half-and-half blend of
the two beat Sky Bet's price by enough to bet on?

## What is fixed

| Item | Value | Where |
|---|---|---|
| Blend | `logit(p) = 0.5 × logit(model) + 0.5 × logit(Sky Bet, margin removed)` | `agent/check_prices.py` `BLEND_W`, `blend` |
| De-vig | Proportional: 1/over ÷ (1/over + 1/under). Lines with no under price are excluded from scoring. | `market_prob` |
| Model probability | The projection logged before the price was captured (`data/projections.csv`) | `review/experiment_a.py` `load_lines` |
| Start date | Prices captured from 2026-11-10. Earlier prices designed the test and don't count. | `START` |
| Benchmark matches | A fixture counts if a hash of its ESPN id falls in one bucket of four (about 12 a weekend). Chosen without the model, the same whenever it is checked. | `is_benchmark` |
| Lines scored | The latest pre-kickoff two-sided price of each match, market and line that Sky Bet puts between 10% and 90% | `summarise` |
| Outcome count | Sky Bet's card count; ESPN's figure where it differs from football-data | `load_lines` |
| Blend betting rule | Shortest over with blended value of +2% or more, model probability 40% or more, no long lines on heavy underdogs. +2% is break-even if a third of the model's disagreement with Sky Bet is real. | `BLEND_EV`, `verdicts` |

## Each week

1. `python review/experiment_a.py pick` lists the benchmark matches of the next 7 days.
2. Screenshot Sky Bet's **Total cards over/under** tab for each one, whether or not the model likes it.
   Other matches can still be sent; they are scored separately and don't count toward the test.
3. Prices are logged as usual (`agent/log_prices.py`). `agent/check_prices.py` prints both verdicts.

## Pass marks

Run `python review/experiment_a.py score`. The test is the **benchmark matches** only.

| Outcome | Condition |
|---|---|
| **Pass: a usable edge** | 150 or more benchmark matches priced and settled; blend log-loss better than Sky Bet's with the 95% interval (resampling whole matches) above zero; **and** mean closing-line value on real bets against Betfair is positive. |
| **Keep collecting** | Fewer than 150 matches, or the interval still includes zero below 300 matches. |
| **Stop: no usable edge at Sky Bet's margin** | 300 matches and the interval still includes zero. |

The weight that would have scored best is printed for information. It may replace 0.5 only in a new
experiment, with a new start date, after the first verdict.

## What does not count as evidence

- Profit on real bets (about 700 bets are needed at these odds).
- Paper-bet returns under either rule (reported, too few to judge).
- Any price captured before the start date. On those, the blend scored −0.003 a line against Sky Bet
  (95% CI −0.029 to +0.019; 194 lines, 29 matches), and the best weight on the model was 0.

## Real money during the experiment

The experiment needs prices, not bets. Which rule to stake on is the user's decision; `check_prices.py`
shows the current rule and the blend rule side by side.
