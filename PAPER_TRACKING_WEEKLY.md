# Weekly V5 Forward Paper Tracking

The frozen weekly model is recorded after the final weekly candle is complete,
normally after Friday's IDX close or during the weekend. Paper tracking stores
recommendations only; it does not assume that the user invests every week or
allocate portfolio capital.

The model uses V5 weekly momentum, a 40-trading-session outcome horizon, and
records only `Ready to Enter`. It returns **up to three** stocks and never fills
an empty slot with a weaker `Wait for Trigger` candidate.

## 1. Run the frozen weekly screener

```bash
python3 scripts/financial_screener_weekly.py \
  --timeframe weekly_position \
  --period 10y \
  --workers 8 \
  --top 3 \
  --output-dir output/paper_weekly_v5/latest
```

## 2. Record the top three immutably

```bash
python3 scripts/paper_track_weekly.py \
  --source output/paper_weekly_v5/latest/weekly_position.csv
```

The tracker writes:

- `output/paper_weekly_v5/picks.csv`: top-three recommendation ledger;
- `output/paper_weekly_v5/runs.csv`: one row for every weekly screen,
  including weeks with no valid pick;
- `output/paper_weekly_v5/snapshots/`: immutable dated snapshots.

Running step 2 again with the same source is idempotent. Trying to replace an
already-recorded date/model with a different source is rejected.

## 3. Resolve outcomes when enough sessions exist

```bash
python3 scripts/paper_resolve.py \
  --ledger output/paper_weekly_v5/picks.csv \
  --download
```

This command is safe to run repeatedly. Picks stay `PENDING` until their
40-session path exists. Mature results are written back to the ledger and to
`summary.csv` and `paper_report.md` beside it.

## Review contract

- Only `Ready to Enter` is recorded as a paper recommendation. `Wait for
  Trigger` remains visible in the raw screener CSV but is not a paper pick.
- The weekly outcome horizon remains frozen at 40 trading sessions.
- `PENDING` rows are resolved only after enough future daily candles exist.
- Review absolute return, median return, positive-run rate, profit factor, and
  same-window IHSG excess. Capital CAGR is secondary because the screener is
  used occasionally.
