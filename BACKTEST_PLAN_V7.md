# Backtest Plan V7 — Daily Momentum Contraction

V7 replaces the failed daily pullback-recovery family with a distinct research
hypothesis: a relative-strength winner consolidating near its 52-week high may
continue after a confirmed breakout. Weekly and monthly remain frozen at V5.

## Evidence and limits

- Cross-sectional momentum has broad empirical support, including evidence
  outside the US. See [Jegadeesh and Titman (2002)](https://doi.org/10.1093/rfs/15.1.143).
- Nearness to the 52-week high has been studied as a momentum signal. See
  [George and Hwang (2004)](https://doi.org/10.1111/j.1540-6261.2004.00695.x).
- Trading volume can affect momentum persistence, but the relationship is not
  a guarantee that high-volume daily breakouts continue. See
  [Lee and Swaminathan (2000)](https://doi.org/10.1111/0022-1082.00280).
- The 10-session contraction rule below is an engineering hypothesis for this
  IDX dataset. It is not presented as a proven academic anomaly.

## Frozen V7 daily formula

- Existing hard eligibility: price, turnover, ATR range, and
  `Close > EMA50 > EMA200`.
- Trend and momentum: `EMA20 > EMA50`, positive RS63 versus IHSG, positive
  20-session trend quality, RSI14 in `[50, 72]`, and price at least 80% of its
  rolling 52-week high.
- Contraction: the preceding 10-session high-low range is at most 12% of the
  current close, and prior ATR% is no greater than its trailing 60-session
  median.
- Anti-chase: close is no more than two ATR above EMA20.
- Active breakout: close above the preceding 10-session high, bullish candle,
  close in the top 30% of its range, and volume at least 1.2 times its 20-day
  average.
- A valid contraction without an active breakout is `Wait for Trigger` at the
  preceding 10-session high plus one IDX tick.
- Rank: 30% RS63, 25% trend quality, 25% contraction quality, and 20% nearness
  to the 52-week high.
- Exit is unchanged: 1.5 ATR stop, 2R target, and five-session time stop.

## Decision rule

The primary artifact is `recommendation_summary.csv`, with Ready, Wait, and
combined top picks reported separately. Development must have enough runs,
positive mean basket return, PF at least 1.20, positive same-window IHSG
excess, and reasonable yearly stability. Median basket and positive-run rate
must be reported because occasional users may miss rare large winners.

No V7 threshold may be adjusted after inspecting confirmation. V7 does not
replace `scripts/financial_screener.py` unless it passes and later survives
future paper trading or genuinely unseen data.
