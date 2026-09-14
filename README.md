# CacingNagaPRO

Screener teknikal saham BEI untuk memilih maksimal tiga kandidat pada dua
timeframe yang sudah dibekukan:

| Mode | Status | Horizon evaluasi |
|---|---|---:|
| `weekly_position` | Kandidat final paper/forward test | 40 sesi |
| `monthly_long_term` | Lulus gate development; kandidat final paper | 126 sesi |

Daily sengaja dikeluarkan dari kode final karena hasil terakhir belum lulus.
Riset daily berikutnya akan dimulai terpisah agar tidak mengubah formula final
weekly dan monthly.

## Menjalankan screener

Weekly, setelah candle Jumat selesai:

```bash
python3 scripts/financial_screener.py \
  --timeframe weekly_position \
  --period 10y --workers 8 --top 3 \
  --output-dir output/screener/weekly_latest
```

Monthly, setelah sesi bursa terakhir bulan tersebut selesai:

```bash
python3 scripts/financial_screener.py \
  --timeframe monthly_long_term \
  --period 10y --workers 8 --top 3 \
  --output-dir output/screener/monthly_latest
```

Output berada di `<output-dir>/<timeframe>.csv`. Jumlah rekomendasi dapat 0–3;
screener tidak memasukkan saham lemah hanya untuk memenuhi tiga slot.

## Struktur kode final

```text
scripts/
  financial_screener.py  # strategi dan CLI final weekly/monthly
  screener_ranking.py    # ranking lintas saham
  screener_core.py       # data, indikator, dan reporting bersama
```

## Verifikasi

```bash
pytest -q
python3 tests/backtest_engine.py --self-test
```

Data, cache, dan hasil backtest lama dalam `resource/` serta `output/` tetap
disimpan sebagai audit. “Final” berarti formula dan interface dibekukan, bukan
jaminan keuntungan atau bebas risiko.
