# CacingNagaPRO

Screener teknikal saham BEI yang memilih maksimal tiga kandidat untuk satu
timeframe per eksekusi. Entrypoint final berada di
`scripts/financial_screener.py`.

| Mode | Bentuk output | Horizon evaluasi |
|---|---|---:|
| `daily_swing` | Watchlist Top 3 setara | maksimal 10 sesi |
| `weekly_position` | Kandidat momentum siap entry | 40 sesi |
| `monthly_long_term` | Kandidat momentum jangka panjang | 126 sesi |

Jumlah hasil boleh 0–3. Screener tidak memasukkan saham yang gagal filter hanya
untuk memenuhi tiga slot.

## Menjalankan screener

Daily dijalankan setelah sesi bursa selesai:

```bash
python3 scripts/financial_screener.py \
  --timeframe daily_swing \
  --period 2y --workers 8 --top 3 \
  --output-dir output/screener/daily_latest
```

Weekly dijalankan setelah candle Jumat selesai:

```bash
python3 scripts/financial_screener.py \
  --timeframe weekly_position \
  --period 5y --workers 8 --top 3 \
  --output-dir output/screener/weekly_latest
```

Monthly dijalankan setelah sesi bursa terakhir bulan selesai:

```bash
python3 scripts/financial_screener.py \
  --timeframe monthly_long_term \
  --period 10y --workers 8 --top 3 \
  --output-dir output/screener/monthly_latest
```

GitHub Actions menjalankan jadwal berikut secara otomatis:

- daily: Senin–Jumat pukul 18.00 WIB;
- weekly: Jumat pukul 18.15 WIB;
- monthly: tanggal 1 pukul 18.30 WIB untuk candle bulan sebelumnya.

Menu **Run workflow** juga menyediakan pilihan `daily_swing`,
`weekly_position`, dan `monthly_long_term`. Setiap run menjalankan unit test dan
self-test backtest sebelum screening, lalu mengunggah CSV sebagai artifact
selama 90 hari.

## Laporan AI

Laporan numerik deterministik selalu dibuat lebih dahulu dan menjadi sumber
kebenaran. Bila `GEMINI_API_KEY` tersedia, Gemini memakai Google Search untuk
mencari berita terbaru setiap ticker, lalu menambahkan penjelasan market, alasan
teknikal, katalis, risiko, dan hal yang perlu dipantau. Laporan menyertakan link
sumber pencarian. AI tidak boleh mengubah ticker, ranking, status, score,
horizon, atau level dari program.

Untuk GitHub Actions, tambahkan repository secret `GEMINI_API_KEY`. Repository
variable `GEMINI_MODEL` bersifat opsional; default-nya
`gemini-2.5-flash-lite`. Jika key, dependency, atau model bermasalah, proses
tetap berhasil menggunakan laporan deterministik.

Google Search grounding dapat menambah biaya pemakaian Gemini API. Pencarian
memprioritaskan sumber primer seperti BEI, keterbukaan informasi emiten,
regulator, dan situs perusahaan; jika sumber kredibel tidak ditemukan, AI wajib
menyatakannya tanpa mengarang katalis.

Artifact berisi CSV lengkap dan `<timeframe>_report.md` yang memuat laporan
deterministik beserta analisis AI jika tersedia.

CSV lengkap berada di `<output-dir>/<timeframe>.csv`. Baris rekomendasi daily
ditandai `selected_top3=true`; baris lain dipertahankan untuk audit alasan
lolos/gagal filter.

## Kontrak daily D+10

Universe menggunakan daftar saham pada `resource/daftar-saham.xlsx` dan OHLCV
harian. Kandidat awal wajib memenuhi:

- harga minimal Rp200;
- rata-rata turnover 20 sesi sebelumnya minimal Rp10 miliar;
- median turnover 20 sesi sebelumnya minimal Rp5 miliar;
- ATR/Close 14 sesi antara 1,5%–4%;
- Close di atas SMA50 dan SMA50 lebih tinggi daripada lima sesi sebelumnya.

Di setiap tanggal, seluruh saham yang lolos filter harga, likuiditas, dan ATR
diranking silang menggunakan bobot setara:

- return harga 60 sesi;
- ATR/Close 14 sesi;
- kedekatan harga ke SMA20.

Skor minimum adalah 80/100, lalu diambil maksimal tiga skor tertinggi. Ketiga
hasil ditampilkan sebagai satu basket dengan tingkat keyakinan setara karena
backtest tidak membuktikan Rank 1 lebih baik daripada Rank 2 atau Rank 3.

Daily tidak memakai IHSG sebagai hard filter. Kondisi IHSG hanya ditampilkan
sebagai diagnosis. Output menyediakan pilihan take-profit `3% / 5% / 10%` dan
stop-loss `2% / 5%` dari harga entry aktual. Kombinasinya dipilih sendiri oleh
user; screener tidak mengklaim salah satunya sebagai aturan eksekusi yang sudah
tervalidasi. Yang tervalidasi baru kualitas pemilihan watchlist sampai D+10.

Ringkasan pengujian independen per rekomendasi (biaya transaksi diperhitungkan,
tanpa modal dan compounding):

| Periode | Sampel | Mean return D+10 | Win rate D+10 | Profit factor D+10 | Pernah +3% dalam D+10 |
|---|---:|---:|---:|---:|---:|
| Development 2020–2022 | 904 | +0,478% | 50,33% | 1,168 | 68,92% |
| Validation 2023 | 395 | +0,532% | 43,54% | 1,194 | 64,30% |
| Audit 2024–2025 | 628 | +0,276% | 49,68% | 1,095 | 66,88% |

Uplift peluang menyentuh +3% dibanding saham eligible pada tanggal yang sama
tetap positif pada ketiga periode. Hasil D+2 dan D+5 tidak stabil, sehingga
versi short-swing 2–5 sesi tidak digabungkan.

Keterbatasan utama: universe historis masih memakai daftar saham saat ini
(survivorship proxy), data berasal dari OHLCV Yahoo Finance, dan hasil historis
tidak menjamin performa berikutnya.

## Struktur kode

```text
scripts/
  financial_screener.py  # aturan strategi final dan entrypoint CLI
  screener_ranking.py    # runtime serta ranking lintas saham
  screener_core.py       # data, indikator, dan reporting bersama

tests/
  test_final_screener.py # kontrak ketiga strategi final
  backtest_engine.py     # engine backtest dan self-test
  backtest_data.py       # utilitas data backtest
```

Data, cache, dan hasil backtest dalam `resource/` dan `output/` tetap disimpan
sebagai audit lokal serta tidak menjadi bagian runtime.

## Verifikasi

```bash
pytest -q
python3 tests/backtest_engine.py --self-test
```

“Final” berarti formula dan interface dibekukan berdasarkan bukti yang tersedia,
bukan jaminan keuntungan atau rekomendasi personal.
