# Post-freeze test plan (draft, 11 November 2026)

Fixes, before any result exists, how each candidate in `agent/POST_FREEZE.md` will be tested and what counts
as a pass. It is locked when merged. Changing a pass mark, season split or parameter grid after seeing a
result means writing a new plan with a new date; the old result then doesn't count.

**Status:** decisions made 11 November 2026 (section 8); locked once reviewed and merged. Nothing here changes projections, prices or code. Per `CLAUDE.md`,
every change that alters model results needs your explicit go-ahead; this plan only says how each would be
judged.

## 1. Gate: judge glm-v4.1 on the live log (when about 300 matches are scored)

Run `python agent/score_projections.py cards.db data/projections.csv`. Rows logged before the 10 Nov
correction are `glm-v4`, after it `glm-v4.1`; both are scored on the corrected counts.

| Result | Action |
|---|---|
| Live log-loss within the backtest's noise (backtest minus live, 95% interval includes 0) | Go on to section 3 |
| Live clearly worse than the backtest (interval above 0) | Stop. Find the cause (look-ahead, data fault, season-wide shift) before testing anything |

The gate is on log-loss only. League gaps (Premier League +0.51, La Liga +0.62 at the last check) are reported,
not gated: they are what candidate 1 is meant to fix.

At the last check (283 matches) the difference was +0.0039 (95% CI −0.0037 to +0.0111): this gate passes.

## 2. How big a gain can the data detect? (measured)

Two similar fits scored on the same 4,534 matches (2024-27) differ by about 0.08 log-loss per match
(standard deviation), so the 95% interval on a difference has a half-width of about **0.0023**. Examples
from the half-life check: 120 against 240 days +0.0030 (±0.0023); 120 against 60 days −0.0065 (±0.0024).
Per season the half-width is about 0.0035 (1,980 matches) and 0.0063 (575 matches).

- A gain below about 0.003 can't be told from luck on this data.
- With seven candidates, the interval is widened to 99% (half-width about 0.0032) so one lucky candidate
  doesn't look like a win.

## 3. Rules that apply to every candidate

1. **Order.** One at a time, in the order of section 4. A kept change becomes the base for the next one.
2. **Parameters are tuned on 2022-24 only.** Each candidate has a small fixed grid (section 4). The best
   value on 2022-24 is carried to 2024-27 untouched. No retuning on the judging seasons.
3. **Judging set: 2024-27 walk-forward** (weekly refit on earlier matches only, as `review/wf.py`). Caveat
   stated up front: these seasons were reused for earlier keep-or-drop decisions, so they are mildly
   optimistic. That is why section 5 exists.
4. **Pass (all four):**
   - match-total log-loss better than the base by **at least 0.004**, with the **99% interval above zero**
     (resampling whole matches);
   - better in **at least 2 of the 3 seasons**, and never worse by more than its own 95% half-width in the
     third;
   - **no calibration regression:** over lines 2.5-6.5 the calibration slope stays within 0.90-1.10, and
     no league's projected-against-actual gap gets worse by more than 0.1 cards;
   - the candidate's own targeted check (section 4) passes.
5. **Fail otherwise.** A candidate that is better but misses the 0.004 bar is "unproven", not kept, and can
   come back in the confirmation round if the live log supports it.
6. **Combined test once.** After all single tests, the kept set is fitted together and must still pass rule 4
   against the unchanged base, so two good ideas that cancel are caught.
7. **Versioning.** A kept change gets its own model label (`glm-v5`, `glm-v5.1`...), so the projection log
   can be scored by version. Logged rows are never rewritten.

## 4. Candidates, in order

Order is by expected gain, then effort. Each cites the evidence already in the repo.

| # | Candidate | Fixed grid (tuned on 2022-24) | Targeted check, on top of rule 4 | What it changes in practice |
|---|---|---|---|---|
| 1 | **Fast league level.** League intercepts use their own short half-life; team and referee effects keep 120 days. | League half-life 20, 30, 60 days | Live-log bias by league (Premier League and La Liga now +0.5 to +0.6 cards, Serie A −0.3) moves toward zero in a walk-forward replay of 2026-27, with no league flipping sign past 0.3 | All projections track the current season's card level |
| 2 | **Dependent team counts.** A shared match effect (or per-team dispersion) so the two teams' cards correlate. Measured residual correlation +0.166. | Match-effect variance fitted by moments, plus one alternative: negative binomial per team | Team over 3.5 predicted within 1 point of observed (now 16.2% v 14.1%); each-team 2+ within 1 point (now 36.3% v 39.6%); match-total log-loss no worse | Team and each-team line prices only. Match totals are unchanged by design |
| 3 | **Curved underdog term.** Add gap × \|gap\| (or one spline knot). | Linear + square; spline knot at 20% | Teams with under 20% win probability: projected within 0.1 cards of actual (the Forest and Sevilla losses motivated this) | Underdog team lines, the "2+ only" caution |
| 4 | **Early-season shrinkage.** Stronger team penalty in a team's first 10 matches of a season. | Extra penalty ×1, ×2, ×4 | Improvement concentrated in the first 10 weeks, no loss after | August-September prices |
| 5 | **Referee cards-per-foul conversion.** | One shrinkage strength, 3 values | Gain not explained by the existing referee effect (partial test: refit with referee effect removed must lose more) | Referee-heavy matches |
| 6 | **Derby flag and stakes.** Only if at least 150 flagged matches exist in 2022-27 (counted first). | A fixed derby list written down before counting; stakes from league position | Flagged matches' calibration; no reuse of the list after seeing results | Derby prices, where Sky Bet currently disagrees most |
| 7 | **Player level.** Not in this round: needs a new line-up and player data source (`CLAUDE.md`: ask before adding one). | — | — | — |

Reasons for the order: candidates 1 and 2 fix mis-specifications the review measured (`REVIEW.md` sections
3 and 4); 3 has two live losses behind it; 4 and 5 are smaller; 6 may not have the sample; 7 needs a new
source.

## 5. Confirmation: protecting against reused test seasons

Passing section 3 means **screened in**, not proven. Confirmation uses only matches played **after the plan's
date (11 November 2026)**, which no earlier decision has seen.

- Replay the screened-in model walk-forward over those matches, alongside the unchanged base.
- Judge at the end of the season (about May 2027, roughly 1,300 matches) or earlier if the half-width has
  shrunk to 0.0035: **keep if** the improvement over the base is above zero (95% interval, whole matches)
  and at least half the size seen in screening.
- Until then, a screened-in change may be used **in the projection log only** (a second model label,
  scored beside the live one) and not for the prices you bet on. That costs nothing and starts the clock.
  Prices switch only if you approve after seeing the first confirmation read.

## 6. Interaction with experiment A

Experiment A (`review/EXPERIMENT_A.md`) scores the 50/50 blend against Sky Bet. Changing the model under
it changes what it measures. So: the experiment keeps scoring the **projection logged at the time**, and its
parameters don't move. A kept change shows up inside experiment A only through newly logged projections,
which carry the new model label and are scored separately by version. No re-scoring of past rows.

## 7. Who does what, and what it costs

- **Runner (Sonnet).** `review/post_freeze_runner.py`: one command per candidate that runs the walk-forward,
  applies rule 4 and prints pass or fail with the numbers. It must reproduce the figures in section 2
  before it is trusted. Written from this plan; no judgement calls.
- **Plan review (a different, stronger model, once).** Before this plan is merged: try to find a way a
  candidate could pass by luck, a missing check, or a pass mark that contradicts section 2. Output: a list.
- **Candidate implementations (Sonnet),** one branch each, tests included, no effect on projections until
  you approve the merge.
- **Results review (stronger model, once, after the single and combined tests).** Independent reading of
  the runner's output and any claim made from it, in the way `REVIEW.md` was checked.
- **Estimated effort:** the runner is a few hours of work; each candidate is a half day. Compute is small
  (a walk-forward takes about two minutes).

## 8. Decisions (made 11 November 2026)

1. **Pass bar: 0.004** log-loss per match, with the 99% interval above zero (section 3, rule 4). About 1.7 times
   the measured half-width; small real gains below it are "unproven", not kept.
2. **Rollout: log only until confirmed.** A screened-in change runs beside the live model in the projection log,
   under its own label, and does not set betting prices until you approve the confirmation read (section 5).
3. **Candidate 7 (players) stays out of this round,** as drafted: it needs a new data source.
4. **Candidate 6's derby list:** drafted and approved by you before any matches are counted, as drafted.

Remaining before the plan is locked: the one-off review by a different, stronger model (section 7), then merge.
