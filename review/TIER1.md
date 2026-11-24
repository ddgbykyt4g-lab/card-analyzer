# Tier 1 leagues: backtest result (24 Nov 2026)

`python review/tier1_backtest.py tier1.db`. Walk-forward on 2023-27, the live model and settings fitted on the
five leagues together, no referees (as these leagues would be priced live: ESPN lists no officials from Oct 2026).
Gate fixed before running: +0.02 log-loss a match over the league-average baseline, 95% interval above 0.

| League | Matches | Gain | 95% CI | By season (23-24 .. 26-27) | Verdict |
|---|---|---|---|---|---|
| Greece (GREEK) | 784 | +0.0356 | +0.016 to +0.056 | +0.050 +0.030 +0.032 +0.021 | PASS |
| Belgium (BELG) | 1,042 | +0.0233 | +0.010 to +0.037 | +0.021 +0.027 +0.030 +0.001 | PASS |
| Portugal (PRIM) | 1,016 | +0.0185 | +0.004 to +0.033 | +0.026 +0.015 +0.008 +0.036 | fail (just short) |
| Netherlands (ERED) | 1,035 | +0.0114 | -0.000 to +0.024 | +0.000 +0.012 +0.016 +0.026 | fail |
| Turkey (SUPERLIG) | 1,134 | +0.0101 | +0.001 to +0.021 | +0.008 +0.010 +0.010 +0.018 | fail |

For scale: the six live leagues gain +0.036 together (with referees); the Championship +0.008. Calibration is
close in all five (over 3.5 projected v actual within 1.1 points). Caveat: ESPN marks no second yellows in Belgium,
Greece or Turkey, so two-yellow dismissals there count as one straight red (at most about 0.07 cards a match low;
model and baseline see the same data). Next: Sky Bet must offer card markets for Greece and Belgium, then a few
weeks of price checks against `tier1.db` before any bet.
