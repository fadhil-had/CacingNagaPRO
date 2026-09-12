# V3 Research Protocol — IDX Breakout/Swing Screener

## Decision

V1/V2 are retained as immutable controls.  They are **not deleted** and may
not be overwritten.  The V2.5 IPO-corrected proxy result is `NO_DEPLOY`: it
does not establish a portfolio that clears the pre-declared evidence, profit
factor, drawdown, and excess-return gates.

V3 is exploratory research only.  It will be implemented in a separate
screener module and output directory; the live `financial_screener.py` remains
unchanged until a locked V3 candidate has passed its tests.

## What the current evidence says

The following results use `output/backtest_v2/v2_5_ipo_proxy/`, trigger-aware
execution, 0.20% fee and 0.10% adverse slippage per side, a maximum of three
positions, and the IPO-date proxy universe.  The proxy corrects the most
obvious pre-IPO look-ahead, but it is still survivor-biased because historical
delistings are not yet included.

| Finding | Evidence | Research implication |
|---|---|---|
| Daily portfolio risk is unacceptable. | Selection max drawdown is -44.6% to -56.8% across the three daily holds. | Do not seek a daily deployment by adjusting an arbitrary score cut-off. |
| Weekly results are not stable enough. | The 4/8-bar holds have PF 1.01/0.95; the 12-bar hold has only 55 closed trades and -30.61% drawdown. | Treat weekly as a hypothesis source, not a candidate strategy. |
| Monthly observations are too few. | The 3/6/9-bar holds have 27/18/18 closed trades, below the minimum 30. | Do not infer an edge from high PF alone. |
| The score is not a reliable rank. | In weekly 8-bar top-pick completed trades, the 50–75 score quartile averaged +2.13%, while the 85–90 quartile averaged -1.07%. | A larger confluence score must not be assumed better. |
| Daily confirmed breakouts are poor in this implementation. | Daily 10-bar top-pick completed `Breakout Confirmed` trades averaged -0.56% net; their median was -2.46%. | Test setups separately; do not pool breakout, pullback, and candle patterns. |
| Regime alone cannot rescue the strategy. | A selection-only bullish regime filter left daily drawdowns near -40% to -79% and weekly evidence below the required trade count. | Do not make a bullish-only rule a V3 strategy without another independent improvement. |

These are diagnostics, not proof of a causal relationship.  They determine
which questions are worth testing next.

## Research rationale

Academic evidence documents medium-horizon momentum in cross-sectional winner
and loser portfolios, but does not validate a particular Indonesian breakout
rule, cost assumption, or scoring model.  In particular, the classic evidence
is over 3–12 month holding horizons, whereas the current daily rule uses short
lookbacks and a 5–15 session holding window.  The implementation therefore
needs its own evidence rather than borrowing an expected result from the
literature.[^1]

Backtest selection after many candidate rules have been inspected produces
overstated statistics unless multiple-testing risk is controlled.[^2]  Every
V3 experiment must consequently be named in advance, change one mechanism,
and report failures.

[^1]: N. Jegadeesh and S. Titman, ["Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency"](https://doi.org/10.1111/j.1540-6261.1993.tb04702.x), *The Journal of Finance* 48(1), 1993. The paper reports positive winner-minus-loser results over 3–12 month formation/holding horizons; it is a motivation to test momentum, not evidence that this screener works.
[^2]: C. R. Harvey and Y. Liu, ["Evaluating Trading Strategies"](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID2474755_code87814.pdf?abstractid=2474755&mirid=1), 2014. The paper describes why strategy-selection statistics are overstated after many tests and proposes multiple-testing adjustments.

## The implementation issue to correct first

The current score gives 10 points to `volatility`, although valid volatility is
already required by `hard_pass`; a candidate that reaches `Ready to Enter`
also must pass `setup`, while the score gives it another 15 points.  These
credits are not independent ranking information.  Relative strength is both a
percentile/gate and a 20-point score factor.  This creates a confluence number
that is difficult to interpret as an empirical ordering.

V3 must separate **eligibility** (must-pass risk and tradability filters) from
**ranking** (a small number of features with demonstrated incremental value).
It must not change stops, targets, holds, and ranking in the same experiment.

## Pre-registered experiment sequence

### V3-A — Score/rank ablation

**Hypothesis:** score components that duplicate eligibility do not improve
top-N selection.

- Keep all existing signal, trigger, stop, target, holding, cost, and portfolio
  rules unchanged.
- Remove `volatility` and mandatory `setup` from the ranking score only; they
  remain hard filters.
- Compare score/rank monotonicity, top-1/top-3 net excess, portfolio PF and
  drawdown against V2.5.
- Do not select a new weight from 2024–2026.

This is a diagnostic simplification.  It may fail or produce an equivalent
ordering; either outcome is useful.

### V3-B — Setup-family ablation

**Hypothesis:** pooled setup types obscure different return distributions.

- Starting with a single timeframe, run separate variants for: confirmed
  breakout, pullback/reclaim, and candle pattern.
- Keep the V3-A ranking, trigger, stop, target, and hold unchanged.
- Report trade count, trigger rate, net excess, PF, stop rate, and constrained
  portfolio result for every family.  No family with insufficient evidence may
  graduate.

The daily result above makes "confirmed breakout" a rejection candidate, not
a default preferred family.  Weekly setup families are a better initial
diagnostic because their individual outcomes are less uniformly negative, but
they must still clear portfolio-level gates.

### V3-C — Relative-strength role ablation

**Hypothesis:** RS should be either an eligibility gate or a ranking feature,
not both.

- Compare RS gate-only versus rank-only after V3-A/B identify a viable setup
  family.
- Keep lookback fixed; do not sweep lookbacks and RS thresholds together.

### V3-D — Exposure by regime

**Hypothesis:** regime is useful as an exposure control only after a setup has
shown a positive signal-level result.

- Compare unchanged exposure versus no new positions in neutral/bearish
  conditions.
- Evaluate drawdown and missed opportunity at portfolio level.
- Bullish-only alone has already failed the V2.5 selection diagnostic and is
  not a standalone V3 candidate.

### V3-E — Exit/risk research

Only begin after one signal/ranking variant passes V3-A through V3-D.  Change
one of stop distance, target, time stop, position sizing, or correlation cap
at a time.  Changing exits first would hide whether the signal itself has an
edge.

## Evaluation protocol

1. Use the existing 2016–2023 folds for exploratory comparison only.
2. Report every experiment in a variant ledger: variant ID, one hypothesis,
   source hash, inputs, and result.
3. Apply existing gates: daily 100, weekly 60, monthly 30 closed trades; PF at
   least 1.20; maximum drawdown no worse than -30%; positive excess CAGR; and
   positive validation direction in at least two folds.
4. The already-inspected 2024–2026 period cannot be used to tune V3 or claimed
   as independent confirmation.  A V3 configuration that survives exploratory
   research requires future paper-trading or a newly reserved unseen period.
5. Retain stressed-cost and IPO-proxy runs.  Full historical delisting coverage
   remains a requirement before any production claim.

## First deliverable

Build a factor/setup ablation report from frozen V2.5 trade outcomes before
changing screener logic.  It must report results by fold and use the same
top-N, slot, cash, duplicate-ticker, trigger, and cost rules as the portfolio
engine.  The report selects at most one V3-A implementation hypothesis; it
does not optimize a grid of thresholds.

## Sources

1. N. Jegadeesh and S. Titman, ["Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency"](https://doi.org/10.1111/j.1540-6261.1993.tb04702.x), *The Journal of Finance*, 1993.
2. C. R. Harvey and Y. Liu, ["Evaluating Trading Strategies"](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID2474755_code87814.pdf?abstractid=2474755&mirid=1), 2014.
