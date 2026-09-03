# Backtest Plan V1 — IDX Multi-Factor Screener (Baseline sebelum V2)

> Tujuan: mengukur performa **aturan V1 yang frozen** di `scripts/financial_screener.py`
> sebelum indikator/threshold/bobot diubah untuk V2.

Runner: `tests/backtest_screener_v1.py`. Terkait: `README.md`, `scripts/financial_screener.py`.

**Kondisi jujur saat dokumen ini ditulis (2026-09-03):**
- File backtest yang disebut `README.md` (`tests/test_financial_screener.py`,
  `backtest-matrix.yml`, `BACKTEST_WORKFLOW_GUIDE.md`, `output/backtest/`)
  **belum ada di repo ini**. Jangan mengacu ke file itu.
- Universe `resource/daftar-saham.xlsx` = **963 baris** (cek lokal).
- Runner V1 mengimpor ulang fungsi screener, tidak menduplikasi rumus.

## 1. Apa yang diuji (dan apa yang TIDAK)

### 1.1 Diuji — logika V1 apa adanya (frozen)
Fungsi yang dipakai ulang: `resample_timeframe`, `tambah_indikator`,
`analisa_saham_confluence`, `finalisasi_score_dan_status`, `analisa_market_regime`,
`hitung_market_breadth_from_frames`, `get_timeframe_config`, `get_max_hold_days`.

Per timeframe (`daily_swing` / `weekly_position` / `monthly_long_term`):
- Sinyal `Ready to Enter` dan `Wait for Trigger`.
- Setup: `Breakout Confirmed` / `Bullish Pullback/Reclaim` / pola candle.
- Level: `trigger_price`, `entry`, `stop`, `target`, `entry_window`, `max_hold_days`.

### 1.2 Frozen — JANGAN diubah selama backtest
`TIMEFRAME_CONFIG` (RSI min/max, ATR min/max, extension, volume ratio, skor,
bobot `FACTOR_WEIGHTS`, `stop_atr`, `target_rr`, `entry_window`, `max_hold_days`).
Kalau diubah = sudah V2, catat sebagai varian.

### 1.3 Non-goals (anti over-engineering)
- Tanpa optimasi/grid-search/ML. Tanpa intraday, short, compounding.
- Tanpa Gemini/berita. Tanpa microstructure bid-offer.

## 2. Metodologi — walk-forward point-in-time (anti look-ahead)

1. **Unduh sekali** data daily OHLCV + IHSG (`^JKSE`), lalu **slice per tanggal sinyal**.
2. **Tanggal sinyal (rebalance)** — bukan tiap bar (mahal utk 963 saham):
   - `daily_swing`: tiap **5 hari bursa**. `weekly_position` / `monthly_long_term`:
     tiap **20 hari bursa**. Override via `--step-days`.
3. Pada tiap tanggal sinyal `S`, tiap ticker: potong `df_daily.loc[:S]`, jalankan
   pipeline screener **persis seperti live**. Candle `S` dianggap selesai (asumsi
   rebalance historis — didokumentasikan).
4. **Breadth & regime point-in-time**: `hitung_market_breadth_from_frames` dari frame
   yang sudah di-slice `<= S`, lalu `analisa_market_regime`. Tanpa data masa depan.
5. **RS percentile universe point-in-time** via `finalisasi_score_dan_status` per tanggal `S`.
6. Sinyal `Ready`/`Wait` dicatat ke `signals_*.csv` beserta levelnya.

## 3. Eksekusi simulasi (data daily, jujur gap)

- **Entry**:
  - `entry_type == "active"`: entry di **Open hari bursa berikutnya** (S+1)
    ditambah slippage. **Batal (return `None` = no-trade)** bila gap-open
    menembus level sebelum entry: `open <= stop` atau `open >= target`
    (pelajaran draf lama: gap pra-entry tidak boleh dipaksa entry lalu
    langsung stop/target di bar sama).
  - `entry_type == "planned"`: pantau `entry_window` sesi (3/5/10); entry limit
    di `trigger_price` pada hari pertama `High >= trigger_price` dengan harga
    `max(open, trigger) + slippage`. **Batal (`None`)** bila sebelum trigger
    ada bar dengan `open <= stop` / `open >= target`, atau `Low <= stop`
    di hari yang belum trigger. Gagal trigger = no-trade (`None`),
    bukan 0 — agar `trigger_rate = n_trades / n_signals` jujur.
- **Exit** (loop dari `entry_idx` s/d `entry_idx + max_hold_days - 1`):
  - Gap-open dicek dulu tiap bar: `open <= stop` → `STOP_GAP` di open;
    `open >= target` → `TARGET` di target (pelajaran draf lama).
  - Bila stop+target kena di bar sama → **`--same-bar-policy`** (default
    `stop` = konservatif → `STOP_SAME_BAR`; alternatif `target` →
    `TARGET_SAME_BAR`). Tanpa flag, hasil bias optimis.
  - Selain itu `Low <= stop` → SL; `High >= target` → TP; lewat
    `max_hold_days` (10/40/126) → `time-stop` di Close + slippage.
- **Overlap**: default satu ticker satu posisi terbuka — sinyal baru pada
  ticker yang `signal_date <= exit_date` posisi berjalan dilewati
  (override `--allow-overlap-same-ticker`). Mencegah double-count.
- **Portfolio vs signal-level**: default semua sinyal dicatat di
  `signals_*.csv` dan disimulasikan (signal-level). `--top N` memakai
  `ranking_candidates` screener lalu hanya top-N per tanggal yang
  disimulasikan (kolom `rank`) — catat di report.

## 4. Universe, periode, buffer

- Universe default: `resource/daftar-saham.xlsx` (963 kode). Iterasi cepat:
  `--tickers BBCA,BBRI,TLKM` atau `--max-tickers N` (alfabetis, dicatat di report).
- Periode yang disarankan: `--start 2019-01-01 --end 2024-12-31`
  (cakup bull/bear/COVID/recovery). Monthly butuh >= 6 thn data (>= 72 bar bulanan).
- Buffer lookback otomatis: daily 400 hari, weekly 800 hari, monthly 2500 hari kalender.
  Runner unduh `period=max` lalu slice — sederhana dan konsisten.
- **Survivorship bias diakui**: universe hari ini dipakai utk masa lalu.
  Hasil V1 = **upper bound**. Tidak dikoreksi di V1, hanya dicatat.

## 5. Sizing & metrik

- **Sizing V1**: notional tetap per trade (`--notional`, default Rp10jt),
  dibulatkan ke lot, dilewati bila kas tak cukup / harga < min / turnover < min.
  Tanpa compounding — mengukur kualitas sinyal, bukan efek bunga-berbunga.
- Metrik per timeframe: `n_signals`, `trigger_rate`, `n_trades`, `win_rate`,
  `avg_ret_net`, `expectancy_R`, `profit_factor`, `max_drawdown` (cumsum PnL),
  `avg_hold_days`, `% time-stop`, breakdown per `setup` / `regime` / `status`
  + perluasan V2-review (serapan `tests/idx_screener_backtest.py`, tanpa ubah
  eksekusi): `by_factor` (per `factor_*` dari `conditions` V1),
  `by_score_bucket` (`<50/50-59/.../90+`), `by_rank_bucket` (`1/2/3/4+`
  dari `rank`/`status_rank`).
- Cara baca: V2 layak jika `expectancy_R > 0` net, `profit_factor > 1.2`,
  dan `Ready` jelas lebih baik dari `Wait`. Jika tidak, ubah **filter** dulu
  (likuiditas/extension/regime), bukan menaikkan target.
- Detail per dimensi di `breakdown_*.csv` (kolom `dimension/dimension_value/
  n_trades/win_rate/avg_ret_net_pct/expectancy_R/profit_factor`, di-group
  tambahan `status` + `sample_split` bila `--holdout-start` dipakai):
  `rank_bucket/score_bucket/setup/regime/factor_*` — dasar checklist V2.

## 6. Output & cara jalan

```
output/backtest/
  signals_<tf>_<start>_<end>.csv      (semua sinyal + diagnostik V1: signal_id,
    signal_close, rsi, atr_pct, vol_ratio, vol_z, turnover20, rs_*, regime_score,
    factor_*, status_rank, sample_split bila --holdout-start dipakai)
  trades_<tf>_<start>_<end>.csv       (ter-trigger + passthrough diagnostik)
  summary_<tf>_<start>_<end>.json     (+ by_factor/by_score_bucket/by_rank_bucket)
  breakdown_<tf>_<start>_<end>.csv   (R per rank_bucket/score_bucket/setup/regime/status/sample_split/factor_*)
  portfolio_<tf>_<start>_<end>.csv + portfolio_curve_*.csv (bila --max-positions>0)
  BACKTEST_REPORT_<tf>_<start>_<end>.md (incl. V2 Review checklist)
  cache/  (download mentah, --use-cache / --no-cache)
```

```bash
pip install -r requirements.txt
```

### 6.1 Skenario resmi — hanya 4x run (frozen baseline, tanpa varian)

> Fokus aim: ukur **aturan V1 frozen** per timeframe sebagai baseline V2.
> Jangan tambah run ke-5 (sweep `--top` / `--include-wait` / `--same-bar-policy target` /
> `--allow-overlap` / `--step-days` / ubah threshold = varian V2, di luar scope ini).
> Semua run 2–4 memakai **jendela, universe, holdout, dan portfolio yang identik** —
> hanya `--trend` yang beda — agar antar-timeframe apple-to-apple.

| # | Tujuan (satu hipotesis) | Trend / periode | Perintah | Output kunci + kriteria lolos |
|---|---|---|---|---|
| 1 | Validasi runner eksekusi jujur (tanpa network) | — | `python tests/backtest_screener_v1.py --self-test` | `Self-test OK: target, STOP_SAME_BAR, STOP_GAP, time-stop, simulate_trade.` Gagal → stop, betulkan runner, jangan lanjut ke #2–4. |
| 2 | Baseline daily_swing | `daily_swing`, 2019-01-01–2024-12-31 (6 thn, cakup COVID/bear/recovery; step default 5 hari bursa) | lihat `Run #2` di bawah | `signals/trades/summary/breakdown/portfolio(_curve)/BACKTEST_REPORT_daily_swing_*.md` ada; catat `trigger_rate/n_trades/expectancy_R/profit_factor` + SELECTION vs HOLDOUT. |
| 3 | Baseline weekly_position | `weekly_position`, periode sama (step default 20 hari bursa) | lihat `Run #3` di bawah | File setara `*_weekly_position_*`; kriteria sama. |
| 4 | Baseline monthly_long_term | `monthly_long_term`, periode sama (butuh ≥6 thn / ≥72 bar bulanan; step default 20 hari bursa) | lihat `Run #4` di bawah | File setara `*_monthly_long_term_*`; kriteria sama. Wajib ada sebelum klaim poin 8. |

Defaults yang **sengaja TIDAK diubah** di run #2–4 (diubah = V2):
`--top 0` (signal-level), tanpa `--include-wait`, tanpa `--allow-overlap-same-ticker`
(blokir overlap), `--same-bar-policy stop`, `--notional 10000000`,
`--fee-pct 0.2`, `--slippage-pct 0.1`, `--min-turnover 1000000000`,
`--min-price 100`, step default per timeframe.

Arti flag bersama di run #2–4:
- `--max-tickers 100` = Tahap-1 baseline (100 alfabetis, tercatat di report).
  Full 963 = Tahap-2 di luar 4 run ini (breadth PIT mahal — lihat §7.5).
- `--use-cache` = hemat unduhan (`cache/raw_*.pkl`).
- `--holdout-start 2024-01-01` = SELECTION 2019–2023 vs HOLDOUT 2024 dalam
  satu run (kolom `sample_split`); anti-overfit tanpa run tambahan.
- `--max-positions 5 --initial-capital 100000000` = kurva ekuitas realistis
  (filter kronologis `entry_date` + `equity = initial + cumsum(pnl)`),
  tanpa ubah sizing signal-level (notional tetap, tanpa compounding).

```bash
# Run #1 — Self-test 1x (wajib pertama, tanpa network):
python3 tests/backtest_screener_v1.py --self-test
# Lolos bila: Self-test OK: target, STOP_SAME_BAR, STOP_GAP, time-stop, simulate_trade.

# Run #2 — Backtest Daily (baseline daily_swing):
python3 tests/backtest_screener_v1.py --trend daily_swing \
  --start 2019-01-01 --end 2024-12-31 --max-tickers 100 --use-cache \
  --holdout-start 2024-01-01 --max-positions 5 --initial-capital 100000000

# Run #3 — Backtest Weekly (baseline weekly_position, setting identik):
python3 tests/backtest_screener_v1.py --trend weekly_position \
  --start 2019-01-01 --end 2024-12-31 --max-tickers 100 --use-cache \
  --holdout-start 2024-01-01 --max-positions 5 --initial-capital 100000000

# Run #4 — Backtest Monthly (baseline monthly_long_term, setting identik):
python tests/backtest_screener_v1.py --trend monthly_long_term \
  --start 2019-01-01 --end 2024-12-31 --max-tickers 100 --use-cache \
  --holdout-start 2024-01-01 --max-positions 5 --initial-capital 100000000
```

### 6.2 Checklist pasca-run (per run #2–4, tanpa run tambahan)

1. File ada: `signals_<tf>_2019-01-01_2024-12-31.csv`,
   `trades_<tf>_*.csv`, `summary_<tf>_*.json`,
   `breakdown_<tf>_*.csv`, `portfolio_<tf>_*.csv` +
   `portfolio_curve_<tf>_*.csv`, `BACKTEST_REPORT_<tf>_*.md`.
2. Baca report: `n_signals → trigger_rate → n_trades → win_rate →
   expectancy_R / profit_factor / max_drawdown`, lalu
   SELECTION vs HOLDOUT (bila HOLDOUT jauh lebih buruk = overfit;
   jangan "perbaiki" dengan run ulang — catat sebagai temuan V2).
3. Baca `breakdown_*.csv` + `by_factor/by_score_bucket/by_rank_bucket` di
   `summary_*.json` untuk hipotesis V2 **satu per satu** (§8) — bukan untuk
   re-run sekarang.
4. Gate V2 (§8): `expectancy_R > 0` net, `profit_factor > 1.2`, dan
   `Ready` > `Wait` (via kolom status di breakdown; tanpa `--include-wait`
   pun `signals_*.csv` tetap mencatat keduanya untuk pembanding).
   Tak lolos → ubah **filter** dulu di V2, bukan target.

## 7. Batasan yang diakui

1. `auto_adjust=True` — return sedikit beda dari harga aktual (dividen/split).
2. Hanya filter turnover median 20D; tanpa model antrean bid-offer.
3. Stop/target dari High/Low daily; gap disederhanakan.
4. Biaya flat per sisi; pajak dividen & suspend/FCA tidak dimodelkan.
5. Breadth point-in-time itu mahal utk universe penuh — pakai `--max-tickers`
   bertahap dan catat coverage di report.

## 8. Dari V1 ke V2 — aturan main

- Baseline V1 **dibekukan dan dilaporkan dulu** (`summary_*.json` + report MD).
- Setiap perubahan V2 = **satu hipotesis + satu varian + satu perbandingan metrik**.
- Urutan saran: (a) filter likuiditas/extension, (b) threshold skor per regime,
  (c) `stop_atr/target_rr`, (d) terakhir bobot faktor. Jangan sekaligus.
- Poin 8 screener terpenuhi bila tiap timeframe punya >= 1 report V1
  (periode >= 3 thn; monthly >= 6 thn).

- **Lot**: kelipatan 100 (`LOT_SIZE`).
- **Biaya**: `--fee-pct` default 0.2%/sisi + `--slippage-pct` default 0.1%.
  Dilaporkan gross vs net terpisah.

