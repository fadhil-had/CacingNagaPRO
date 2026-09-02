# CacingNagaPRO — Executable Test Plan Dual Mode V1

**Status:** READY TO EXECUTE (revisi + fresh ideas)  
**Tanggal:** 2026-09-02  
**Revisi:** CLI kontrak `--trend`/`--ticker`, engine time-aware (`as_of_date`), precomputed feature store, pinned snapshot, config fingerprinting, kode error terpusat, guardrail alignment, harness consistency/regression  
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

Kode saham dinormalisasi ke uppercase dan format ticker IDX yang digunakan sistem (`normalisasi_ticker` → `BBCA.JK`). Ticker tidak ditemukan harus menghasilkan error yang jelas, bukan hasil kosong tanpa penjelasan.

> **Kesepakatan CLI:** repository memakai argumen `--trend` (bukan `--timeframe`) dan `--ticker` (bukan `--stock-code`). Seluruh command di Bagian 9 memakai kontrak sebenarnya di repository. Alias `--timeframe`/`--stock-code` boleh ditambahkan, tetapi kontrak utama tetap `--trend`/`--ticker`.

Semua error routing memakai kode terpusat agar bisa di-assert otomatis (lihat Task 6):

| Kode | Kondisi |
|---|---|
| `ERR_INPUT` | `all` tanpa ticker, timeframe tidak valid, atau timeframe tertentu + ticker (TC-03/04/05) |
| `ERR_TICKER_NOT_FOUND` | Ticker tidak ada di universe / tanpa data OHLCV (TC-07) |
| `ERR_MARKET_CONTEXT` | Market context/regime gagal dihitung (TC-12) |
| `ERR_INSUFFICIENT_DATA` | Satu timeframe kekurangan data; timeframe lain tetap tampil (TC-08) |

Error routing/context keluar dengan exit code non-zero agar CI bisa menangkapnya.

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

> **Syarat kunci (fresh):** seluruh fungsi analisis harus **time-aware** — menerima `as_of_date` dan menghitung hanya dari data `<= as_of_date`, bukan selalu bar terakhir. `analisa_saham_confluence`, `analisa_market_regime`, dan `hitung_relative_strength` wajib menerima cutoff tanggal. Tanpa ini Mode 2 tidak bisa mereproduksi signal historis Mode 1 dan consistency test mustahil lolos.

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

### Task 6 — Precomputed feature store (perf + konsistensi)

Jangan hitung ulang indikator untuk setiap tanggal signal. Precompute **sekali** per (ticker, timeframe) untuk seluruh rentang, lalu untuk tiap `as_of_date` cukup potong `df.loc[:as_of_date]` dan ambil bar terakhir.

```text
feature_store/<ticker>/<timeframe>.parquet    # OHLCV + indikator (EMA/RSI/MACD/ATR/Vol)
rs_excess_cache/<date>/<timeframe>.parquet    # rs_excess per ticker dari universe yang sama
```

Keuntungan: (1) Mode 1 dan Mode 2 membaca input byte-identik sehingga consistency bisa dicapai; (2) menghilangkan perhitungan berulang O(signal_dates × universe) pada setiap cutoff.

### Task 7 — Pinned data snapshot (determinisme lintas hari/mesin)

Data yfinance berubah antar hari (split, adjustment, delisting) sehingga TC-11 dan regression tidak bisa memakai unduhan live. Bekukan snapshot:

- `scripts/pin_snapshot.py` mengunduh sekali lalu menyimpan ke `tests/fixtures/data/<name>.parquet` (subset ticker likuid: BBCA, ASII, TLKM, ANTM, GOTO, BRPT, dst).
- Test unit/consistency/regression memakai `--snapshot` agar offline & deterministik.
- Backtest full universe tetap live, tetapi config snapshot menyimpan `data_snapshot_md5` + tanggal unduh sebagai jejak.

### Task 8 — Config & data fingerprinting

Setiap folder output berisi `config_snapshot.json`:

```json
{
  "commit": "<sha>",
  "universe_md5": "...",
  "data_snapshot_md5": "...",
  "params": {"top": 3, "signal_step": 1, "fees": {"buy": 0.15, "sell": 0.25}},
  "timeframe_config": {},
  "factor_weights": {}
}
```

Regression membandingkan fingerprint terlebih dahulu. Jika config/data berbeda, hasil tidak dianggap "regresi" — hanya ditandai metadata mismatch.

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
| TC-12 | Mode 2 gagal membaca market context | Error terkontrol (`ERR_MARKET_CONTEXT`), tidak menghitung percentile palsu |
| TC-13 | `as_of_date` historis (bukan hari ini) untuk ticker yang sama | Mode 2 pada tanggal historis identik dengan baris Mode 1 tanggal itu |
| TC-14 | Snapshot data sama, mesin/tanggal berbeda | Output identik (deterministik) |

Runner consistency dipakai lewat `scripts/check_consistency.py` (output CSV mismatch per field). Unit tests TC-01..TC-14 memakai snapshot offline tanpa network.

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

1. Bekukan commit V1 sebelum perubahan (`git rev-parse HEAD` / tag).
2. Jalankan golden backtest pada **pinned snapshot** yang sama (bukan live data).
3. Simpan golden output + fingerprint ke `output/regression/before/`.
4. Implementasikan Mode 2 (tanpa menyentuh formula V1).
5. Jalankan ulang Mode 1 dengan data (snapshot) dan config identik.
6. Bandingkan via `scripts/regression_compare.py` (fingerprint dulu, lalu field-by-field).
7. Selisih hanya boleh di kolom metadata baru.

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
- Fee beli 0,15% (`--buy-fee 0.15`), fee jual 0,25% (`--sell-fee 0.25`).
- Slippage eksplisit (`--slippage-bps 0` default) — ditulis eksplisit agar reproducible.
- Same-bar policy: `--same-bar-policy stop`.
- Overlap: default off (`--allow-overlap-same-ticker` bila baseline V1 memakai overlap).
- Data dibekukan via snapshot untuk smoke/regression; full run live dengan `data_snapshot_md5` tercatat.
- V1 config (`TIMEFRAME_CONFIG`, `FACTOR_WEIGHTS`) tidak disentuh selama test ini.

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

Setiap folder minimal berisi `signals.csv`, `trades.csv`, `BACKTEST_REPORT_*.md`, `config_snapshot.json`, exclusion log, dan run report.

CLI aktual: `python tests/test_financial_screener.py --trend <tf> --start ... --end ... --top 3 --signal-step 1 --output-dir output/backtest/v1_mode1/<tf>`.

---

## 8. Analisis Multi-Timeframe Mode 2

Mode 2 tidak menjalankan simulasi trade baru. Gunakan signal/trade Mode 1, kemudian tambahkan kondisi tiga timeframe pada ticker dan tanggal signal yang sama.

Periode analisis Mode 2 adalah **2022-01-01 sampai 2024-12-31**. Periode bersama ini dipilih agar status `1hari`, `1minggu`, dan `1bulan` dibandingkan pada jendela waktu yang identik.

### Proses

1. Ambil setiap signal top-3 Mode 1.
2. Ambil hasil `1hari`, `1minggu`, dan `1bulan` untuk ticker dan tanggal tersebut dari shared feature/cache.
3. Bentuk `alignment_count` = jumlah timeframe dengan `status == "Strong Buy"`. Kolom sekunder `watch_alignment_count` = jumlah timeframe dengan `status in {"Strong Buy", "Watchlist"}` untuk sensitivitas. Definisi ini eksplisit agar konsisten antar run.
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

**Guardrail sample size (fresh):** untuk metrik per kelompok (win rate, expectancy, profit factor, target rate, dsb) jangan menarik kesimpulan bila `n_trade < 20` — tampilkan `n/a`. Kesimpulan pada kelompok kecil = overfit. Minimum trade untuk laporan alignment: 20 per kelompok.

Alignment hanya menjadi kandidat filter V2 jika peningkatan kualitas konsisten dan jumlah trade memadai. Jangan memasukkannya ke V1 selama test ini.

---

## 9. Command Contract yang Perlu Tersedia

Sesuaikan nama entry point dengan repository, tetapi kontrak eksekusinya harus setara dengan berikut.

### Functional run (CLI aktual repo: `--trend` / `--ticker`)

```bash
python scripts/financial_screener.py --trend 1hari
python scripts/financial_screener.py --trend 1minggu
python scripts/financial_screener.py --trend 1bulan
python scripts/financial_screener.py --trend all --ticker BBCA
```

### Automated tests

```bash
pytest tests/test_dual_mode.py -q          # TC-01..TC-14, offline via snapshot
pytest tests/test_financial_screener.py -q # test existing (fixture)
```

### Backtest Mode 1 (kontrak repo)

```bash
python tests/test_financial_screener.py --trend 1hari \
  --start 2022-01-01 --end 2024-12-31 --top 3 --signal-step 1 \
  --period 10y --output-dir output/backtest/v1_mode1/1hari

python tests/test_financial_screener.py --trend 1minggu \
  --start 2020-01-01 --end 2024-12-31 --top 3 --signal-step 1 \
  --period 10y --output-dir output/backtest/v1_mode1/1minggu

python tests/test_financial_screener.py --trend 1bulan \
  --start 2018-01-01 --end 2024-12-31 --top 3 --signal-step 1 \
  --period max --output-dir output/backtest/v1_mode1/1bulan
```

### Snapshot, consistency, regression, multi-timeframe

```bash
# Pin data snapshot sekali (offline untuk test & regression)
python scripts/pin_snapshot.py --output tests/fixtures/data/idxsnap.parquet \
  --tickers BBCA ASII TLKM ANTM GOTO BRPT --start 2021-01-01 --end 2024-12-31

# Consistency Mode 1 vs Mode 2 (pass criteria: mismatch = 0)
python scripts/check_consistency.py \
  --snapshot tests/fixtures/data/idxsnap.parquet \
  --output output/backtest/v1_mode2/consistency_check.csv

# Regression Mode 1 before vs after
python scripts/regression_compare.py \
  --before output/regression/before \
  --after output/backtest/v1_mode1 \
  --output output/backtest/regression/mode1_before_vs_after.csv

# Multi-timeframe join + alignment
python scripts/analyze_multi_timeframe_backtest.py \
  --input output/backtest/v1_mode1 \
  --output output/backtest/v1_mode2 \
  --start 2022-01-01 --end 2024-12-31
```

Kontrak ini adalah kontrak aktual repository. Coding agent boleh menyesuaikan detail flag tanpa mengubah scope dan pass criteria.

---

## 10. Output Akhir

```text
output/backtest/
├── v1_mode1/
│   ├── 1hari/          # signals_1hari.csv, trades_1hari.csv, BACKTEST_REPORT_1hari.md,
│   │                   # summary_*.csv, config_snapshot.json, exclusion log, run report
│   ├── 1minggu/
│   └── 1bulan/
├── v1_mode2/
│   ├── consistency_check.csv          # mismatch per field (pass = 0)
│   ├── multi_timeframe_signals.csv    # join signal Mode 1 + status 3 timeframe
│   ├── summary_alignment.csv
│   └── MULTI_TIMEFRAME_REPORT.md
└── regression/
    ├── before/                        # golden output Mode 1 (pinned snapshot)
    └── mode1_before_vs_after.csv

tests/fixtures/data/idxsnap.parquet    # pinned snapshot (subset untuk test deterministik)
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
- Semua error routing/context memakai kode terpusat (`ERR_INPUT`, `ERR_TICKER_NOT_FOUND`, `ERR_MARKET_CONTEXT`, `ERR_INSUFFICIENT_DATA`) dengan exit code non-zero.
- Engine analisis time-aware: `as_of_date` historis menghasilkan output yang sama dengan baris Mode 1 di tanggal yang sama (TC-13 lulus).
- Determinisme TC-14 terverifikasi lewat pinned snapshot di dua run (mesin/tanggal berbeda).
- Alignment dilaporkan dengan guardrail `n_trade >= 20` per kelompok; kelompok kecil ditandai `n/a`.

---

## 12. Urutan Eksekusi Final

1. Bekukan commit V1 + pin data snapshot (`pin_snapshot.py`).
2. Buat golden output Mode 1 dari snapshot (simpan ke `output/regression/before/`).
3. Implementasikan engine analisis time-aware (`as_of_date`) + shared feature store.
4. Implementasikan input routing (`--trend`/`--ticker`) + kode error terpusat.
5. Implementasikan shared market-context cache (breadth, regime, RS percentile per tanggal-timeframe).
6. Satukan finalisasi score/status — satu jalur untuk Mode 1 & Mode 2, hapus duplikasi `finalisasi_status_tunggal`.
7. Implementasikan output Mode 2 (3 baris, urutan tetap, `INSUFFICIENT_DATA` per timeframe).
8. Jalankan unit & functional tests TC-01..TC-14 (offline, snapshot).
9. Jalankan consistency test (`check_consistency.py`) — pass criteria mismatch = 0.
10. Jalankan regression test Mode 1 (`regression_compare.py`).
11. Smoke gate: subset 20–30 ticker + periode pendek; assert output non-empty + mismatch 0.
12. Jalankan full backtest Mode 1 per timeframe (1hari → 1minggu → 1bulan).
13. Buat multi-timeframe join dari signal Mode 1 (`analyze_multi_timeframe_backtest.py`).
14. Buat summary alignment + laporan dengan guardrail n >= 20.
15. Gunakan hasilnya sebagai bahan diagnosis V1 dan kandidat perubahan V2 (tanpa menyentuh V1).

---

**Catatan:** plan ini menguji fitur dan strategi V1 tanpa menjadikan multi-timeframe alignment sebagai rekomendasi investasi atau aturan V2 secara otomatis.
