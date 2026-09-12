# Backtest Plan V2 — IDX Multi-Factor Screener

> Objective: develop an executable, cost-aware, and out-of-sample-validated
> V2 screener. V1 and `output/backtest_v1_1/` remain immutable baseline
> artefacts; V2 must not overwrite them.

## 1. Decision from V1

V1 does **not** establish a deployable `Ready to Enter` strategy:

- Its selection-period portfolios lagged IHSG in all three timeframes, with
  large drawdowns.
- `Ready to Enter` expectancy was negative in selection for daily and monthly,
  and the weekly selection advantage did not hold in the holdout.
- A higher V1 score/rank did not reliably imply a better return.
- V1's backtest buys all candidates at the next open. That is incorrect for a
  `planned` entry: it must first trade through `trigger_price` during
  `entry_window`.

Therefore V2 begins with execution validity, not threshold, target, or weight
optimisation. Until the V2 acceptance gates pass, the live screener's output is
research-only and must not label a candidate as immediately executable.

## 2. Scope and guardrails

### In scope

- Long-only IDX equities; daily OHLCV data; daily, weekly, and monthly signals.
- Point-in-time signal formation, universe membership when available, trigger
  entry, exits, portfolio construction, costs, and walk-forward evaluation.
- A smaller, explicitly testable scoring/ranking model for each timeframe.

### Out of scope for V2

- Intraday data, shorting, news/LLM signals, ML optimisation, and production
  deployment before the acceptance gates pass.
- Altering `scripts/financial_screener.py` or
  `tests/backtest_screener_v1.py` to make historical V1 results look better.

### Anti-overfitting rules

1. Every V2 variant changes one pre-registered hypothesis at a time.
2. Select variants only with 2016-01-01–2023-12-31 data, using the walk-forward
   splits below.
3. Do not inspect V2 results for 2024-01-01–2026-08-31 until a configuration is
   locked. It is a confirmation period only; it may reject V2 but may not cause
   a parameter change.
4. The already-observed V1 holdout is not an independent V2 holdout. Preserve
   the V2 configuration and its run metadata so a later, genuinely unseen
   period can be used for final confirmation.
5. Report all tested variants, including failures. No threshold/grid sweep is
   silently discarded.

## 3. Deliverables and ownership

| Deliverable | Purpose |
|---|---|
| `tests/backtest_screener_v2.py` | New engine; V1 runner remains frozen. |
| `tests/test_backtest_screener_v2.py` | Deterministic execution, cost, and portfolio tests. |
| `scripts/financial_screener_v2.py` | V2 logic only after an execution-valid baseline exists. |
| `output/backtest_v2/<run-id>/` | Immutable outputs, metadata, config, hashes, and a report per run. |
| `BACKTEST_PLAN_V2.md` | This pre-registered research contract. |

No V2 code should share an import name or output directory with V1. The V2
metadata must record the screener hash, engine hash/version, data-cache hash,
universe source/coverage, dates, cost assumptions, and execution policy.

## 4. Phase 0 — Freeze and reconcile the baseline

1. Retain `output/backtest_v1_1/` unchanged and tag it `V1_RAW_NEXT_OPEN`.
2. Write a short reconciliation record in the first V2 report: the V1 document
   describes trigger/cost execution, but the V1.1 engine metadata confirms raw,
   next-open counterfactual execution. The source code and run metadata are the
   authoritative record for V1.1.
3. Copy no V1 results into V2 selection metrics. V1 is diagnostic evidence
   only.
4. Define V2 status names before coding:
   - `Candidate`: passes a screen but has no executable order.
   - `Pending Trigger`: a valid plan with trigger, expiry, stop, and target.
   - `Triggered`: the historical/live trigger condition has occurred.
   - `Validated Setup`: reserved for a configuration that passes §9; it is not
     a per-stock buy instruction.

## 5. Phase 1 — Build an execution-valid V2.0 baseline

V2.0 reuses the frozen V1 signal logic only to validate the simulator. It does
not change weights or thresholds.

### 5.1 Entry rules

| Signal type | V2 execution rule |
|---|---|
| `active` | Submit on the next eligible session; fill at next open plus adverse slippage. Reject if the opening price is at/below stop or at/above target. |
| `planned` | For at most `entry_window` sessions, enter only on the first bar with `High >= trigger_price`; fill at `max(Open, trigger_price)` plus adverse slippage. |
| pre-trigger invalidation | Cancel if the plan hits/opens below the stop before trigger, data is unavailable, the window expires, or the opening price makes the planned risk invalid. |

`NO_TRIGGER`, `EXPIRED`, and `PRE_TRIGGER_STOP` are no-trade outcomes. They
must be counted in trigger-rate and opportunity statistics but never converted
into a next-open trade or a zero-return trade.

### 5.2 Exit and portfolio rules

- Apply entry and exit costs on each side; model both baseline and stressed
  slippage scenarios. Start with documented IDX commission/tax assumptions,
  kept as input parameters rather than hidden constants.
- Use daily OHLC conservatively: stop first when stop and target are both
  reachable in the same candle. Gap stops fill at open; target gaps use the
  stated conservative fill policy consistently.
- Enforce 100-share lots, cash limits, maximum positions, no same-ticker
  overlap, and no order deferral after a missed entry date.
- Mark remaining positions to market at run end; report realised and open P&L
  separately.
- Use the same execution module for signal-level and portfolio tests.

### 5.3 Mandatory automated tests

Add fixtures covering: active entry; planned trigger at open; planned intraday
trigger; gap above trigger; no trigger; pre-trigger stop; expired plan; missing
session; entry below stop/above target; stop gap; target gap; same-bar
stop/target; time stop; costs; lot rounding; no same-ticker overlap; capacity
priority; and end-of-period marking. The full research run cannot start until
these tests and a small hand-calculated fixture pass.

## 6. Phase 2 — Data and benchmark integrity

1. Store the exact ticker universe used on every historical screen date. If
   point-in-time IDX constituent/listing data cannot yet be sourced, label the
   result `SURVIVOR_BIASED`, report first/last available dates per ticker, and
   exclude post-delisting performance claims.
2. Preserve unadjusted tradable OHLCV for fills and separately record the
   adjustment/corporate-action policy used for indicators. Never mix the two
   silently.
3. Add quality flags for suspensions, zero-volume sessions, stale quotes,
   missing OHLC, and insufficient lookback. Report their counts by timeframe.
4. Benchmark each trade and portfolio against IHSG over the identical invested
   period. Report net return and net excess return.
5. Keep full-universe runs separate from smoke-test subsets; a truncated,
   alphabetic universe cannot be used for a performance decision.

## 7. Phase 3 — V2 signal research

Only begin after Phase 1 passes. Each row below is a separate hypothesis and
creates a separate, named V2 variant.

| Order | Hypothesis | Change | Required comparison |
|---:|---|---|---|
| 1 | Correct trigger execution changes the apparent edge. | V2.0, frozen V1 logic. | Trigger rate, net expectancy, PF, excess return, and portfolio DD vs V1.1. |
| 2 | The score is over-counting gates. | Remove score credit for hard-pass volatility and mandatory setup; retain them only as filters. | Score/rank monotonicity and top-N portfolio metrics. |
| 3 | RS needs one role, not two. | Compare RS as a gate versus a score factor; do not use both. | Incremental return conditional on all other filters. |
| 4 | Setup should be timeframe-specific. | Compare breakout, pullback/reclaim, and candle-pattern setups separately. | Net result and sample size by setup, not pooled. |
| 5 | Regime should control exposure, not merely raise a score threshold. | Compare no new positions / reduced slots / unchanged exposure by regime. | Portfolio DD and excess return. |
| 6 | Ranking needs an empirical ordering. | Rank on the small set of factors that survive steps 2–5; remove score as a claim if no monotonicity exists. | Top-1/top-3 versus all eligible triggered trades. |

Do not change stop ATR, target R:R, holding period, and ranking in the same
variant. Exit-rule research begins only after a signal/ranking variant passes
the selection gates.

## 8. Walk-forward protocol

Use only the development period to select a V2 configuration:

| Fold | Fit / hypothesis design | Validation |
|---|---|---|
| WF-1 | 2016-01-01–2018-12-31 | 2019-01-01–2020-12-31 |
| WF-2 | 2016-01-01–2020-12-31 | 2021-01-01–2022-12-31 |
| WF-3 | 2016-01-01–2022-12-31 | 2023-01-01–2023-12-31 |

Lock one configuration only if its direction and risk profile are consistent
across validation folds. Then run it once, unchanged, on 2024-01-01–2026-08-31
as a confirmation report. A rejection returns research to Phase 3 with a new
future confirmation period required after the next lock.

Timeframes are independent strategies. Do not transfer daily thresholds or
weights to weekly/monthly, and do not deploy a timeframe merely because another
one passes.

## 9. Pre-declared acceptance gates

A timeframe may move from `Candidate` research to paper-trading only when the
locked configuration satisfies all of the following on validation folds and
does not fail the confirmation report:

1. Actual triggered, net-of-cost trades have positive average excess return
   versus IHSG and profit factor at least 1.20.
2. The result is positive in at least two of three validation folds, with no
   materially adverse confirmation-period result.
3. Rank is useful: top-1 or top-3 triggered selections beat the eligible
   triggered universe on net excess return; otherwise publish no ordinal
   quality score.
4. The portfolio has a documented maximum drawdown below 30% and does not
   underperform IHSG on a CAGR basis across the validation aggregate.
5. Minimum evidence: 100 triggered trades for daily, 60 for weekly, and 30 for
   monthly. Below this threshold the result is `INSUFFICIENT_SAMPLE`, not a
   pass.
6. The result remains positive under the stressed cost scenario and contains no
   unresolved execution/data-quality failure.

These are research gates, not a promise of future investment performance.

## 10. Reporting format and next decision

Every V2 run writes:

- `run_metadata.json`: version/hashes, data/universe coverage, exact config,
  costs, execution policy, and fold.
- `signals.csv`: all candidates, status transitions, trigger deadline, and
  factor diagnostics.
- `trades.csv`: audit order dan hasil simulasi. Kolom `order_status` menyimpan
  status triggered/cancelled atau alasan penolakan; order yang terpicu juga
  menyimpan gross/net fills, costs, exits, holding sessions, IHSG return, dan
  excess return.
- `summary.csv`, `breakdown.csv`, `portfolio_summary.csv`, and
  `portfolio_equity_curve.csv`: all with fold and cost scenario columns.
- `backtest_analysis.md`: failed gates, bias flags, and the explicit next
  decision (`continue research`, `paper trade`, or `reject timeframe`).

The first implementation milestone is **V2.0 execution validity**, not a new
stock-picking formula. After its report exists, choose exactly one Phase-3
hypothesis for the first logic variant.
