# Final Monthly Backtest Plan

This run evaluates the existing V4/V5 monthly momentum model with the current
recommendation-centric engine. The formula and 126-trading-session horizon are
frozen before reading the new top-three report.

## Frozen model

- Eligibility: existing minimum price, turnover and monthly ATR range, plus
  daily close above a rising daily SMA200.
- Setup: positive 12-minus-1-month stock momentum, positive relative strength
  versus IHSG, and price at least 75% of its rolling 12-month high.
- Ranking: 50% 12-minus-1-month relative strength, 30% nearness to the
  12-month high, and 20% risk-adjusted six-month return.
- Entry: active at the next daily open after the completed monthly screen.
- Exit: existing monthly 2.5 ATR stop, 3R target, and 126-trading-session time
  stop.
- Output: at most three valid recommendations. Never add an ineligible stock
  merely to reach three names.

## Data boundary

- Development: 2016-01-01 through 2023-12-31.
- 2024 through August 2026 is an inspected audit, not fresh confirmation.
- The IPO-date proxy remains in force; historical delisting membership is
  still incomplete and must be disclosed.

## Decision gate

Development must have at least 30 completed top-pick trades, positive mean
basket return, individual profit factor at least 1.20, positive same-window
IHSG excess, and reasonable yearly stability. Median basket return and
positive-run rate must be reported even if the aggregate gates pass.

Passing development makes the model a frozen forward-paper candidate, not a
risk-free production strategy. No threshold may be changed using the inspected
2024-2026 audit.
