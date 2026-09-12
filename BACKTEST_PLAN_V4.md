# Backtest Plan V4 — Timeframe-Specific Ranking

V4 is a research screener for the original objective: return at most three IDX
candidates independently for daily swing, weekly position, and monthly
long-term horizons. It does not replace the live baseline until the tests below
pass.

## Design contract

Every model applies three separate stages:

1. **Eligibility** — price, median daily turnover, structural trend, ATR range,
   and the timeframe-specific IHSG trend must pass.
2. **Ranking** — eligible setup candidates receive cross-sectional percentile
   ranks. Mandatory filters receive no duplicate score credit.
3. **Trigger** — `Ready to Enter` means the timeframe trigger is active;
   otherwise a valid setup is `Wait for Trigger`. A missing setup is skipped.

Daily and weekly `Ready`/`Wait` candidates share one global ranking. Therefore
`--top 3` means at most three total candidates, not three per status.

## Frozen starting formulas

### Daily swing — 5–15 sessions

- Trend: `Close > EMA50 > EMA200`.
- Setup: positive 63-session excess return versus IHSG plus a recent pullback
  with five-session minimum `Z20` in `[-1.75, -0.25]` and RSI5 below 45.
- Trigger: close above the preceding daily high or an EMA9 reclaim.
- Rank: 60% RS63 percentile, 25% 20-session log-trend-quality percentile,
  15% median daily turnover percentile.

### Weekly position — 4–12 weeks

- Trend: `Close > EMA20 > EMA50` on completed weekly candles.
- Setup: positive RS13 and RS26, price at least 80% of its 52-week closing
  high, and extension from EMA20 no greater than two weekly ATR.
- Trigger: weekly close above EMA10 and the preceding four-week high.
- Rank: 40% RS13, 25% RS26, 20% 52-week-high position, and 15%
  risk-adjusted 26-week return percentiles.

### Monthly long-term — 3–9 months

- Trend: daily close above a rising daily SMA200.
- Setup: positive stock and excess 12–1 momentum, with price at least 75% of
  its 52-week closing high.
- Trigger: completed monthly rebalance; entry is evaluated on the next session.
- Rank: 50% RS12–1, 30% 52-week-high position, and 20% risk-adjusted six-month
  return percentiles.

## Evaluation rules

- Use the IPO-proxy manifest and retain the documented delisting limitation.
- Include fees, slippage, IDX lots, trigger cancellation, position capacity,
  and force-close rules from the V2 engine.
- Report top-1 and top-3 results, rank monotonicity, trade count, trigger rate,
  profit factor, excess CAGR versus IHSG, and maximum drawdown by timeframe.
- Do not tune several thresholds after inspecting the same result. Any change
  becomes a separately named experiment.
- Historical 2024–2026 results have already been observed during V1–V3 work;
  they are comparison data, not a fresh independent confirmation set.
- Graduation still requires future paper trading or genuinely unseen data.

## Commands

Deterministic validation:

```bash
pytest -q
python3 tests/backtest_screener_v2.py --self-test
```

Small V4 smoke test:

```bash
python3 tests/backtest_screener_v2.py \
  --screener-file scripts/financial_screener_v4.py \
  --timeframe daily_swing \
  --tickers BBCA,BBRI,TLKM \
  --start 2022-01-01 \
  --holdout-start 2024-01-01 \
  --end 2026-08-31 \
  --workers 3 \
  --output-dir output/backtest_v4/smoke_daily
```

Do not start a full-universe run until the smoke output has been inspected for
candidate counts, statuses, entry types, and rank component columns.
