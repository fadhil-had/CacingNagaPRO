# IDX Backtest — 1HARI

- Signals: **412**
- Triggered trades: **375**
- Entry rate: **91.0%**
- Win rate: **44.8%**
- Target hit rate: **36.0%**
- Expectancy: **0.14R/trade**
- Average win: **1.51R**
- Average loss: **-0.98R**
- Profit factor: **1.26**
- Trade-sequence max drawdown: **-21.28R**
- Average hold: **8.9 sessions**

## Assumptions

- Source of strategy rules: `scripts.financial_screener`
- Top picks per signal date: 3
- Entry window: 3 sessions
- Max hold: 20 sessions
- Same-bar policy: stop
- Buy fee: 0.150%
- Sell fee: 0.250%
- Slippage: 0.0 bps per side
- Same ticker overlap: blocked until resolved

## Notes

- Universe berasal dari Excel yang sama dengan screener.
- Data historis Yahoo dari universe hari ini dapat memiliki survivorship bias.
- Max drawdown di atas adalah trade-sequence drawdown, bukan portfolio equity drawdown dengan alokasi modal simultan.
