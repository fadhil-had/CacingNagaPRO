# Backtest Plan V8 — Weekly Selection, Daily Timing

V8 tests a different daily architecture. The stock must first satisfy the
frozen V5 weekly momentum model; daily data is then used only to find a
controlled pullback and recovery entry. Weekly and monthly outputs remain the
V5 models.

## Data boundaries

- Development: 2016-01-01 through 2023-12-31.
- The existing 2024-2026 history is already inspected. It may be reported as
  an audit, but it is not a fresh confirmation set and cannot graduate V8.
- Graduation requires forward paper observations collected after the formula
  is frozen.
- Weekly context uses only a weekly candle whose `W-FRI` label is no later than
  the daily screen date. A Monday-Thursday screen therefore sees the preceding
  completed week, never a partial current week.

## Frozen daily formula

### Weekly selection gate

- At least 52 completed weekly observations.
- Weekly `Close > EMA20 > EMA50`.
- Weekly 13- and 26-week excess returns versus IHSG are both positive.
- Price is at least 80% of its rolling 52-week high.
- Weekly RSI14 is between 50 and 72.
- Weekly close is no more than two weekly ATR above weekly EMA20.

### Daily timing gate

- Existing V4 hard eligibility remains active: daily
  `Close > EMA50 > EMA200`, minimum price and turnover, and the configured
  daily ATR range.
- During the preceding five sessions, price must pull back to within 0.5 daily
  ATR above EMA20 without any close falling below EMA50.
- Current RSI14 must be between 45 and 68, and current close must be no more
  than 1.5 daily ATR above EMA20.
- `Ready to Enter` requires a bullish candle, close above the preceding daily
  high and EMA9, close in the top 30% of its range, and volume at least its
  20-session average.
- Otherwise a valid setup is `Wait for Trigger` at the preceding daily high
  plus one IDX tick.

### Ranking and exit

- 35% weekly RS26, 20% weekly RS13, 15% weekly risk-adjusted 26-week return,
  20% daily recovery strength, and 10% daily entry quality.
- Stop and target remain the existing daily contract: 1.5 ATR stop and 2R
  target.
- Time stop is frozen at ten trading sessions.

## Decision rule

Development must have at least 100 executed top-pick trades, positive mean
basket return, individual profit factor at least 1.20, positive same-window
IHSG excess, and reasonable yearly stability. Median basket return and
positive-run rate are mandatory diagnostics.

Do not change thresholds after inspecting the audit period. V8 does not
replace `scripts/financial_screener.py` unless it passes development and later
survives forward paper tracking.
