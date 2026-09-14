# Kontrak Screener Final

Satu entrypoint menyediakan tiga strategi yang dibekukan:

- `daily_swing`: watchlist Top 3 setara, skor minimal 80, maksimal D+10;
- `weekly_position`: momentum ranking, hanya `Ready to Enter`, horizon 40 sesi;
- `monthly_long_term`: momentum ranking jangka panjang, horizon 126 sesi.

Jalankan dengan:

```bash
python3 scripts/financial_screener.py --timeframe daily_swing \
  --period 10y --workers 8 --top 3 \
  --output-dir output/screener/daily_latest

python3 scripts/financial_screener.py --timeframe weekly_position \
  --period 10y --workers 8 --top 3 \
  --output-dir output/screener/weekly_latest

python3 scripts/financial_screener.py --timeframe monthly_long_term \
  --period 10y --workers 8 --top 3 \
  --output-dir output/screener/monthly_latest
```

Aturan final:

- Maksimal tiga hasil; nol hasil adalah valid.
- Weekly tidak mengisi slot kosong dengan kandidat `Wait for Trigger`.
- Daily adalah watchlist D+10, bukan sinyal transaksi otomatis.
- Daily menyediakan pilihan TP `3% / 5% / 10%` dan SL `2% / 5%` dari harga
  entry aktual; user memilih sendiri kombinasinya.
- Pilihan TP/SL tersebut adalah opsi risk management, bukan klaim bahwa aturan
  eksekusinya sudah tervalidasi.
- IHSG pada daily bersifat diagnostik dan tidak memblokir kandidat.
- Eksperimen daily 2–5 sesi ditolak dan tidak masuk kode final.
- AI hanya menjelaskan output yang sudah dikunci dan tidak ikut menghitung atau
  mengubah rekomendasi. Tanpa `GEMINI_API_KEY`, laporan deterministik tetap ada.
- Pencarian berita AI memakai Google Search grounding dan menyertakan sumber;
  klaim berita tanpa sumber kredibel tidak boleh dibuat.
- Hasil lama di `output/` dipertahankan sebagai audit lokal.

Detail formula, bukti backtest, keterbatasan data, dan command verifikasi ada di
`README.md`.
