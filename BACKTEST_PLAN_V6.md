# Backtest Plan V6 — Occasional Top-3 Recommendations

V6 evaluates the screener as an occasional recommendation tool, not as an
always-invested portfolio. The V5 production baseline remains unchanged until
V6 passes this plan.

## Frozen changes

- Daily: a confirmed V5 recovery candle creates a planned order above that
  candle's high for the existing three-session entry window. It is not bought
  automatically at the next open.
- Daily: after entry, a close below the recovery candle's low exits the trade
  early. The existing 1.5 ATR hard stop and 2R target remain unchanged.
- Daily: only the five-session holding horizon is tested.
- Weekly: the V5 momentum formula and ranking remain unchanged. Candidates
  whose planned hard-stop distance exceeds 25% are rejected.
- Weekly: only the 40-session holding horizon is tested.
- Monthly: unchanged and outside the current decision.

## Primary evaluation

`recommendation_summary.csv` is the primary result. Ready and Wait signals are
reported separately. Each screen date is also treated as one recommendation
run, using the equal-weight mean outcome of the top picks that actually
triggered.

V6 is promising only when development results have sufficient recommendation
runs, positive net expectancy, profit factor of at least 1.20, positive mean
same-window excess return versus IHSG, and no obvious dependence on one year.
Median basket return and positive-run rate must be reported because occasional
users may miss rare large winners. Confirmation data is reported but must not
be used for another threshold adjustment.

Portfolio CAGR and portfolio drawdown remain secondary diagnostics. They are
not V6 recommendation-quality gates.

## Validation commands

```bash
pytest -q
python3 tests/backtest_screener_v2.py --self-test
```

Smoke tests must precede full-universe daily and weekly runs.
