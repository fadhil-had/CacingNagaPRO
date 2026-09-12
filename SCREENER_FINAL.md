# Frozen Screener Entrypoint

The consolidated entrypoint routes one requested timeframe to its frozen paper
candidate. It intentionally does not expose an unvalidated daily model.

## Weekly

```bash
python3 scripts/financial_screener_final.py \
  --timeframe weekly_position \
  --period 10y \
  --workers 8 \
  --top 3 \
  --output-dir output/paper_weekly_v5/latest
```

Run after the final weekly candle. The model returns zero to three Ready
candidates and uses a 40-session outcome horizon.

## Monthly

```bash
python3 scripts/financial_screener_final.py \
  --timeframe monthly_long_term \
  --period 10y \
  --workers 8 \
  --top 3 \
  --output-dir output/paper_monthly_v1/latest
```

Run after the final monthly candle. The model returns up to three valid
candidates and uses a 126-session outcome horizon.

## Daily

`daily_swing` exits with an explicit error because V5 through V8 failed their
aggregate development gates. It will remain unavailable until a genuinely new
data-backed model is predeclared and validated.
