# Monthly Forward Paper Tracking

Run after the final completed IDX session of each calendar month.

## 1. Run the frozen monthly screener

```bash
python3 scripts/financial_screener_monthly.py \
  --timeframe monthly_long_term \
  --period 10y \
  --workers 8 \
  --top 3 \
  --output-dir output/paper_monthly_v1/latest
```

## 2. Record the recommendations

```bash
python3 scripts/paper_track_monthly.py \
  --source output/paper_monthly_v1/latest/monthly_long_term.csv
```

The tracker writes the ledger to `output/paper_monthly_v1/picks.csv`, records
every run in `runs.csv`, and stores immutable dated snapshots. Repeating the
same source is idempotent; an attempt to overwrite a date/model with different
source data is rejected.

Rows remain `PENDING` until 126 future trading sessions are available. The
paper result must report top-three basket mean and median, positive-run rate,
profit factor, and same-window IHSG excess.

## 3. Resolve outcomes when enough sessions exist

```bash
python3 scripts/paper_resolve.py \
  --ledger output/paper_monthly_v1/picks.csv \
  --download
```

The resolver uses the same fees, slippage, conservative same-bar policy,
next-open entry, stop, target, and time-stop implementation as the backtest.
Running it before maturity leaves the picks as `PENDING`.
