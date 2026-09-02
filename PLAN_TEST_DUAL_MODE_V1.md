# CacingNagaPRO — Executable Test Plan Dual Mode V1

**Status:** READY TO EXECUTE  
**Tanggal:** 2026-09-02  
**Scope:** V1, tanpa mengubah formula score atau strategi

---

## 1. Tujuan

Menguji dua mode penggunaan screener:

| Mode | Input | Output |
|---|---|---|
| Mode 1 — Market Screening | `timeframe=1hari/1minggu/1bulan`, kode saham kosong | Maksimal 3 rekomendasi saham |
| Mode 2 — Single Stock | `timeframe=all`, kode saham terisi | Analisis satu saham pada 3 timeframe |

Mode 2 hanya menampilkan analisis multi-timeframe. Mode 2 tidak mengubah score V1, tidak memilih top 3, dan tidak otomatis menjadi filter entry.

---

## 2. Aturan Input

| Timeframe | Kode saham | Hasil |
|---|---|---|
| `1hari` / `1minggu` / `1bulan` | Kosong | Jalankan Mode 1 |
| `all` | Terisi | Jalankan Mode 2 |
| `all` | Kosong | Validation error |
| Timeframe tertentu | Terisi | Validation error untuk scope saat ini |
| Tidak valid | Apa pun | Validation error |

Kode saham dinormalisasi ke uppercase dan format ticker IDX yang digunakan sistem. Ticker tidak ditemukan harus menghasilkan error yang jelas, bukan hasil kosong tanpa penjelasan.

---

## 3. Prinsip Teknis

Gunakan satu fungsi analisis sebagai sumber kebenaran:

```python
analyze_stock(ticker, timeframe, as_of_date, market_context)
```

Alur yang diharapkan:

```python
# Mode 1
results = analyze_universe(timeframe, as_of_date)
output = rank_and_take_top_3(results)

# Mode 2
output = [
    analyze_stock(ticker, "1hari", as_of_date, market_context),
    analyze_stock(ticker, "1minggu", as_of_date, market_context),
    analyze_stock(ticker, "1bulan", as_of_date, market_context),
]
```

Mode 1 dan Mode 2 wajib memakai:

- Data snapshot yang sama.
- Tanggal analisis yang sama.
- Universe RS yang sama.
- Market regime yang sama.
- Fungsi indikator dan scoring yang sama.
- Konfigurasi timeframe yang sama.

RS Percentile tidak dihitung dari ticker tunggal. Nilainya diambil dari hasil ranking universe pada tanggal dan timeframe yang sama.

---

## 4. Pekerjaan Implementasi

### Task 1 — Input routing

- Tambahkan pilihan `timeframe=all`.
- Tambahkan input opsional kode saham.
- Implementasikan matrix validasi pada Bagian 2.
- Pastikan perilaku Mode 1 lama tidak berubah.

### Task 2 — Shared analysis engine

- Pisahkan perhitungan indikator/scoring dari format output.
- Pastikan Mode 1 dan Mode 2 memanggil fungsi analisis yang sama.
- Jangan menduplikasi formula score untuk Mode 2.

### Task 3 — Shared market context

- Hitung/cache benchmark, regime, dan RS Percentile per tanggal dan timeframe.
- Gunakan cache yang sama untuk kedua mode.
- Jangan mengunduh ulang full universe untuk setiap permintaan ticker Mode 2.

Cache minimal:

```text
as_of_date | timeframe | ticker | rs_excess | rs_percentile | market_regime
```

### Task 4 — Output Mode 2

Mode 2 menghasilkan tepat tiga baris:

```text
ticker | timeframe | status | score | required_score
rs_percentile | factors | entry | stop | target | reason
```

Urutan harus tetap:

1. `1hari`
2. `1minggu`
3. `1bulan`

Jika satu timeframe kekurangan data, tampilkan `INSUFFICIENT_DATA` untuk timeframe tersebut tanpa menggagalkan timeframe lain.

### Task 5 — Output machine-readable

Pastikan hasil dapat disimpan sebagai CSV/JSON agar dapat dibandingkan otomatis. Tambahkan minimal:

- `mode`
- `as_of_date`
- `ticker`
- `timeframe`
- `rank` — hanya Mode 1
- `score`
- `required_score`
- `status`
- `rs_percentile`
- Seluruh status faktor
- `entry`, `stop`, `target`
- `reason`

---

## 5. Automated Test Cases

Gunakan fixture data tetap agar hasil test deterministik.

| ID | Test | Expected result |
|---|---|---|
| TC-01 | Timeframe tertentu, ticker kosong | Mode 1 berjalan |
| TC-02 | `all`, ticker valid | Mode 2 menghasilkan 3 timeframe |
| TC-03 | `all`, ticker kosong | Validation error |
| TC-04 | Timeframe tertentu, ticker terisi | Validation error |
| TC-05 | Timeframe invalid | Validation error |
| TC-06 | Ticker lowercase/spasi | Dinormalisasi dengan benar |
| TC-07 | Ticker tidak ditemukan | Ticker-not-found error |
| TC-08 | Satu timeframe kekurangan data | Dua timeframe lain tetap tampil |
| TC-09 | Mode 1 memiliki >3 kandidat | Output maksimal 3 saham |
| TC-10 | Mode 1 tidak memiliki kandidat | Empty result yang valid, bukan crash |
| TC-11 | Data dan config sama, run diulang | Output identik |
| TC-12 | Mode 2 gagal membaca market context | Error terkontrol, tidak menghitung percentile palsu |

### Consistency test wajib

Untuk ticker yang muncul pada Mode 1, jalankan Mode 2 pada tanggal yang sama lalu cocokkan baris timeframe yang sama.

Field yang wajib identik:

```text
score
required_score
status
rs_percentile
market_regime
trend_ok
relative_strength_ok
momentum_ok
macd_ok
volume_ok
setup_ok
volatility_ok
entry
stop
target
```

Gunakan toleransi numerik hanya untuk floating-point. Status boolean/string harus sama persis.

**Pass criteria:** mismatch = 0.

---

## 6. Regression Test Mode 1

Tujuan: memastikan penambahan Mode 2 tidak mengubah hasil V1 yang sudah ada.

1. Bekukan commit V1 sebelum perubahan.
2. Jalankan golden backtest pada fixture/sample tetap.
3. Implementasikan Mode 2.
4. Jalankan ulang Mode 1 dengan data dan config identik.
5. Bandingkan output sebelum dan sesudah.

Field pembanding:

- Tanggal signal.
- Ticker dan rank.
- Score/status.
- RS Percentile.
- Entry, stop, target.
- Outcome dan R-multiple.

**Pass criteria:** seluruh hasil identik, kecuali kolom metadata baru yang memang ditambahkan.

---

## 7. Backtest V1 Mode 1

Backtest strategi utama tetap dijalankan melalui Mode 1.

| Timeframe | Periode development | Entry window | Max hold | Top |
|---|---|---:|---:|---:|
| `1hari` | 2022-01-01—2024-12-31 | 3 | 20 | 3 |
| `1minggu` | 2020-01-01—2024-12-31 | 5 | 65 | 3 |
| `1bulan` | 2018-01-01—2024-12-31 | 10 | 252 | 3 |

### Pembagian periode pengujian

| Pengujian | Periode | Kegunaan |
|---|---|---|
| Development Mode 1 `1hari` | 2022-01-01—2024-12-31 | Baseline dan diagnosis V1 harian |
| Development Mode 1 `1minggu` | 2020-01-01—2024-12-31 | Baseline dan diagnosis V1 mingguan |
| Development Mode 1 `1bulan` | 2018-01-01—2024-12-31 | Baseline dan diagnosis V1 bulanan |
| Mode 2 multi-timeframe | **2022-01-01—2024-12-31** | Periode irisan yang sama untuk membandingkan tiga timeframe |
| Locked test V1 vs V2 | **2025-01-01—2026-08-31** | Pengujian final setelah aturan V2 dibekukan |

Data sebelum tanggal mulai tetap boleh dimuat sebagai **warm-up** EMA, MACD, RSI, ATR, dan indikator lain. Signal dan trade hanya dihitung jika tanggal signal berada di dalam periode evaluasi.

Periode locked-test tidak boleh dilihat untuk memilih bobot, threshold, atau aturan V2. Jika V2 diubah setelah hasil locked-test diketahui, periode tersebut tidak lagi dianggap out-of-sample.

Parameter:

- Full universe eligible.
- `signal-step=1`.
- Fee beli 0,15%.
- Fee jual 0,25%.
- Same-bar policy: stop.
- Overlap mengikuti konfigurasi baseline V1.
- Data dan V1 config dibekukan.

Urutan run:

1. Smoke test 20–30 ticker.
2. Full universe `1hari`.
3. Validasi output.
4. Full universe `1minggu`.
5. Validasi output.
6. Full universe `1bulan`.

Output:

```text
output/backtest/v1_mode1/1hari/
output/backtest/v1_mode1/1minggu/
output/backtest/v1_mode1/1bulan/
```

Setiap folder minimal berisi `signals.csv`, `trades.csv`, summary, config snapshot, exclusion log, dan run report.

---

## 8. Analisis Multi-Timeframe Mode 2

Mode 2 tidak menjalankan simulasi trade baru. Gunakan signal/trade Mode 1, kemudian tambahkan kondisi tiga timeframe pada ticker dan tanggal signal yang sama.

Periode analisis Mode 2 adalah **2022-01-01 sampai 2024-12-31**. Periode bersama ini dipilih agar status `1hari`, `1minggu`, dan `1bulan` dibandingkan pada jendela waktu yang identik.

### Proses

1. Ambil setiap signal top-3 Mode 1.
2. Ambil hasil `1hari`, `1minggu`, dan `1bulan` untuk ticker dan tanggal tersebut dari shared feature/cache.
3. Bentuk `alignment_count` berdasarkan jumlah timeframe yang berstatus eligible/bullish sesuai definisi V1.
4. Simpan hasil join.

Output:

```text
signal_date
ticker
source_timeframe
source_rank
source_score
status_1hari
score_1hari
rs_percentile_1hari
status_1minggu
score_1minggu
rs_percentile_1minggu
status_1bulan
score_1bulan
rs_percentile_1bulan
alignment_count
trade_outcome
net_r
```

Kelompok analisis:

| Alignment | Arti |
|---:|---|
| 0/3 | Tidak ada timeframe eligible |
| 1/3 | Hanya satu timeframe eligible |
| 2/3 | Dua timeframe selaras |
| 3/3 | Semua timeframe selaras |

Bandingkan setiap kelompok menggunakan:

- Jumlah trade.
- Win rate.
- Expectancy R.
- Profit factor.
- Target hit rate.
- Average holding period.
- Maximum adverse excursion.
- Breakdown per source timeframe dan market regime.

Alignment hanya menjadi kandidat filter V2 jika peningkatan kualitas konsisten dan jumlah trade memadai. Jangan memasukkannya ke V1 selama test ini.

---

## 9. Command Contract yang Perlu Tersedia

Sesuaikan nama entry point dengan repository, tetapi kontrak eksekusinya harus setara dengan berikut.

### Functional run

```bash
python scripts/financial_screener.py --timeframe 1hari
python scripts/financial_screener.py --timeframe 1minggu
python scripts/financial_screener.py --timeframe 1bulan
python scripts/financial_screener.py --timeframe all --stock-code BBCA
```

### Automated tests

```bash
pytest tests/test_dual_mode.py -q
pytest tests/test_financial_screener.py -q
```

### Backtest Mode 1

```bash
python tests/test_financial_screener.py --timeframe 1hari \
  --start 2022-01-01 --end 2024-12-31 --top 3 --signal-step 1

python tests/test_financial_screener.py --timeframe 1minggu \
  --start 2020-01-01 --end 2024-12-31 --top 3 --signal-step 1

python tests/test_financial_screener.py --timeframe 1bulan \
  --start 2018-01-01 --end 2024-12-31 --top 3 --signal-step 1
```

### Multi-timeframe join/report

Sediakan runner setara dengan:

```bash
python scripts/analyze_multi_timeframe_backtest.py \
  --input output/backtest/v1_mode1 \
  --output output/backtest/v1_mode2 \
  --start 2022-01-01 --end 2024-12-31
```

Jika CLI repository berbeda, coding agent boleh menyesuaikan command tanpa mengubah scope dan pass criteria.

---

## 10. Output Akhir

```text
output/backtest/
├── v1_mode1/
│   ├── 1hari/
│   ├── 1minggu/
│   └── 1bulan/
├── v1_mode2/
│   ├── consistency_check.csv
│   ├── multi_timeframe_signals.csv
│   ├── summary_alignment.csv
│   └── MULTI_TIMEFRAME_REPORT.md
└── regression/
    └── mode1_before_vs_after.csv
```

---

## 11. Definition of Done

Implementasi dan test dinyatakan selesai jika:

- Seluruh TC-01 sampai TC-12 lulus.
- Mode 1 tetap menghasilkan maksimal tiga saham.
- Mode 2 menghasilkan tiga timeframe untuk ticker valid.
- Consistency check Mode 1 vs Mode 2 memiliki mismatch nol.
- Regression test menunjukkan hasil V1 tidak berubah.
- RS Percentile menggunakan universe dan snapshot yang sama.
- Mode 2 tidak memicu download/perhitungan full universe berulang untuk setiap ticker.
- Full backtest Mode 1 selesai untuk tiga timeframe.
- Laporan alignment terbentuk tanpa mengubah trade atau outcome V1.
- Semua error data dicatat dan tidak menghentikan keseluruhan batch tanpa alasan.

---

## 12. Urutan Eksekusi Final

1. Buat golden output Mode 1 sebelum perubahan.
2. Implementasikan input routing dan shared analysis engine.
3. Implementasikan shared market-context cache.
4. Implementasikan output Mode 2.
5. Jalankan unit dan functional tests.
6. Jalankan consistency test.
7. Jalankan regression test Mode 1.
8. Jalankan smoke backtest.
9. Jalankan full backtest Mode 1 per timeframe.
10. Buat multi-timeframe join dari signal Mode 1.
11. Buat summary alignment dan laporan.
12. Gunakan hasilnya sebagai bahan diagnosis V1 dan kandidat perubahan V2.

---

**Catatan:** plan ini menguji fitur dan strategi V1 tanpa menjadikan multi-timeframe alignment sebagai rekomendasi investasi atau aturan V2 secara otomatis.
