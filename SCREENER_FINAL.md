# Kontrak Screener Final

Hanya dua strategi yang masuk kode final:

- `weekly_position`: momentum ranking, hanya `Ready to Enter`, horizon 40 sesi.
- `monthly_long_term`: momentum ranking final, horizon 126 sesi.

Keduanya dijalankan melalui satu file:

```bash
python3 scripts/financial_screener.py --timeframe weekly_position \
  --period 10y --workers 8 --top 3 \
  --output-dir output/screener/weekly_latest

python3 scripts/financial_screener.py --timeframe monthly_long_term \
  --period 10y --workers 8 --top 3 \
  --output-dir output/screener/monthly_latest
```

Aturan final:

- Formula hasil backtest tidak diubah saat konsolidasi.
- Maksimal tiga rekomendasi; nol rekomendasi adalah hasil valid.
- Weekly tidak mengisi slot kosong dengan kandidat `Wait for Trigger`.
- Hasil lama di `output/` dipertahankan sebagai audit.
- Daily tidak tersedia pada entrypoint final dan akan diriset kembali secara
  terpisah.
