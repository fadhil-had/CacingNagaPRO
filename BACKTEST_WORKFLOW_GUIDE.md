# 🧪 Backtest Workflow Guide

GitHub Actions workflow untuk menjalankan comprehensive backtest pada IDX stock screener V2.

---

## 📋 Quick Start

1. Go to **Actions** tab di repository
2. Pilih **"Backtest IDX Stock Screener"** workflow
3. Click **"Run workflow"** button
4. Isi parameter (atau gunakan default)
5. Click **"Run workflow"** hijau
6. Tunggu hasil di **Summary** tab

---

## ⚙️ Input Parameters

### Timeframe (required)
- **1hari**: Backtest day trading setups (1-20 sessions hold)
- **1minggu**: Swing trading setups (1-65 sessions hold)
- **1bulan**: Position trading setups (1-252 sessions hold)
- **all**: Run backtest untuk ketiga timeframe sekaligus

### Start Date (optional)
Default: `2025-01-01`
Format: `YYYY-MM-DD`
Contoh: `2025-06-01`

### End Date (optional)
Default: kosong (hari ini)
Format: `YYYY-MM-DD`
Contoh: `2025-08-31`

### Signal Step (optional)
Default: `1` (process semua signal dates)
Opsi: `1`, `5`, `10`, etc.
Fungsi: Untuk mempercepat backtest, proses setiap N signal dates
Contoh: `5` = process setiap 5 hari saja

### Top Picks (optional)
Default: `3`
Opsi: `1`, `2`, `3`, `5`, `10`
Fungsi: Jumlah top candidates per signal date yang di-trade

### Allow Overlap (optional)
Default: `false` (block posisi overlapping pada ticker sama)
Opsi: `true` atau `false`
Fungsi: Jika `false`, ticker tidak boleh memiliki 2+ posisi aktif bersamaan

---

## 📊 Output Files

Hasil backtest tersimpan di folder `output/backtest/`:

### Main Reports
- `BACKTEST_REPORT_1hari.md` — Summary report timeframe daily
- `BACKTEST_REPORT_1minggu.md` — Summary report timeframe weekly
- `BACKTEST_REPORT_1bulan.md` — Summary report timeframe monthly

### Detailed Data (CSV)
- `signals_1hari.csv` — Semua signals + trade outcomes (daily)
- `trades_1hari.csv` — Hanya trades yang triggered
- `signals_1minggu.csv` — Weekly signals
- `signals_1bulan.csv` — Monthly signals
- `signals_all.csv` — Kombinasi semua timeframe

### Grouped Metrics (CSV)
Per timeframe tersedia breakdown by:
- `summary_regime_1hari.csv` — Metrics grouped by market regime (BULLISH/NEUTRAL/BEARISH)
- `summary_setup_1hari.csv` — Metrics grouped by setup type (Breakout/Pullback/Other)
- `summary_score_1hari.csv` — Metrics grouped by quality score bucket
- `summary_rs_1hari.csv` — Metrics grouped by RS percentile bucket

---

## 📈 Report Metrics

Setiap report menampilkan:

| Metrik | Arti |
| :--- | :--- |
| **Signals** | Total signal dates yang di-analyze |
| **Triggered Trades** | Berapa signals yang berhasil entry |
| **Entry Rate** | Triggered / Total signals |
| **Win Rate** | Trades yang profitable / Total trades |
| **Target Hit Rate** | Trades yang hit target / Total trades |
| **Expectancy** | Average R per trade (R = risk unit) |
| **Avg Win** | Average return saat winning trade |
| **Avg Loss** | Average return saat losing trade |
| **Profit Factor** | Total wins / Absolute total losses (>1 = profitable) |
| **Max Drawdown** | Trade-sequence max cumulative loss |
| **Avg Hold** | Rata-rata sessions dipegang per trade |

---

## 🔧 Advanced Usage

### Scenario 1: Quick Test Baru Strategy
```
- Trend: 1hari
- Start: 2025-08-01
- End: 2025-08-18
- Signal Step: 5
- Top Picks: 3
```
→ Backtest 2 minggu terakhir, setiap 5 hari, cepat & informatif

### Scenario 2: Full V2 Validation (6 bulan)
```
- Trend: all
- Start: 2025-01-01
- End: 2025-08-18
- Signal Step: 1
- Top Picks: 3
```
→ Comprehensive backtest V2 vs V1 comparison
⏱️ Runtime: ~30-45 menit

### Scenario 3: Single Timeframe Deep Dive
```
- Trend: 1minggu
- Start: 2024-01-01
- End: 2025-08-18
- Signal Step: 1
- Top Picks: 5
```
→ Full year+ weekly analysis, semua candidates
⏱️ Runtime: ~15-20 menit

---

## 🎯 Interpretation Guide

### Win Rate ≥ 55%
✅ Good — Sistem lebih sering menang daripada kalah

### Expectancy ≥ 0.5R
✅ Good — Setiap trade rata-rata untung 0.5 risk unit

### Profit Factor ≥ 1.5
✅ Good — Total wins 1.5x lebih besar dari losses

### Average Win ≥ 1.5 × Average Loss (absolute)
✅ Good — Win size lebih besar dari loss size (asymmetric payoff)

### Entry Rate < 30%
⚠️ Check — Signals jarang trigger; setup terlalu strict atau market conditions poor

### Entry Rate > 60%
⚠️ Check — Signals terlalu sering trigger; false entry risk tinggi

---

## 🐛 Troubleshooting

### "Download Yahoo Finance gagal"
- Cek koneksi internet
- Yahoo Finance mungkin rate-limit; tunggu beberapa menit
- Retry workflow

### "Universe saham kosong"
- File `resource/daftar-saham.xlsx` tidak ditemukan
- Atau kolom "Kode" tidak ada di Excel
- Jangan ubah nama/struktur Excel file

### "Data IHSG (^JKSE) tidak tersedia"
- IHSG data tidak ada di periode yang diminta
- Perpanjang start date (ke belakang)
- Atau gunakan period yang lebih besar

### Workflow Timeout (>120 menit)
- Workflow maksimal 120 menit
- Gunakan `signal_step: 5` untuk mempercepat
- Atau pisahkan backtest per timeframe

### Artifact Terlalu Besar
- GitHub Actions free tier max 400MB total artifacts
- Signal Step > 5 untuk mengurangi data
- Atau delete artifacts lama

---

## 📝 CSV Column Guide

### signals_*.csv

| Column | Deskripsi |
| :--- | :--- |
| `timeframe` | Mode tren (1hari/1minggu/1bulan) |
| `signal_date` | Tanggal signal generation |
| `ticker` | Kode saham |
| `rank` | Ranking dalam top picks (1=best) |
| `regime` | Market regime saat signal (BULLISH/NEUTRAL/BEARISH) |
| `quality_score` | Score 0-100 (higher = better) |
| `rs_percentile` | RS ranking vs universe (0-100) |
| `setup` | Tipe setup (Breakout/Pullback/Pattern) |
| `signal_close` | Close price saat signal generated |
| `entry_trigger` | Entry level yang direncanakan |
| `planned_stop` | Stop loss level |
| `planned_target` | Target profit level |
| `planned_rr` | Risk-Reward ratio |
| `triggered` | Apakah trade berhasil entry (True/False) |
| `outcome` | Hasil trade (TARGET/STOP/EXPIRED/etc) |
| `entry_date` | Tanggal entry aktual |
| `entry_price` | Entry price dengan slippage |
| `exit_date` | Tanggal exit |
| `exit_price` | Exit price dengan slippage |
| `hold_sessions` | Berapa sessions dipegang |
| `gross_r` | Return dalam risk unit (sebelum fee) |
| `net_r` | Return dalam risk unit (sesudah fee) |

---

## 🔐 Security Notes

- GEMINI_API_KEY tidak diperlukan untuk backtest (beda dengan live screener)
- Backtest hanya membaca data historis Yahoo Finance
- Hasil commit otomatis dapat di-disable dengan hapus step "Commit Results"
- Artifacts disimpan private per default (tidak terlihat publik)

---

## 📞 Common Questions

**Q: Berapa lama backtest biasanya?**  
A: 1hari (1 bulan) ~3 menit | 1minggu (1 tahun) ~5 menit | all (6 bulan) ~30 menit

**Q: Bisa backtest lebih dari 1 tahun?**  
A: Ya, modify start date ke awal. Tapi runtime akan lebih lama.

**Q: Kenapa beberapa signals tidak triggered?**  
A: Entry window terlalu singkat, atau harga sudah di-gap past entry/target/stop

**Q: Overlap flag apa gunanya?**  
A: Untuk simulasi realistic dengan modal terbatas. Default `false` = satu ticker max 1 posisi aktif.

**Q: Bisa schedule backtest otomatis?**  
A: Saat ini workflow hanya manual trigger. Bisa tambah `schedule` trigger di workflow YAML jika ingin daily backtest.

---

*Last Updated: 2025-08-18*  
*Workflow Version: 1.0*
