# 📈 CacingNagaPRO - Multi-Factor IDX Stock Screener

Sistem automasi pemindaian saham IHSG berbasis **Multi-Factor Confluence** dengan **Gemini AI** & GitHub Actions. Fokus pada **3 timeframe utama** (daily_swing, weekly_position, monthly_long_term).

---

## 🧠 Strategi (Multi-Factor)

Setiap saham di-score dari beberapa faktor kunci. Hanya yang pass hard filters & score ≥threshold menjadi **Ready to Enter**.

### Faktor yang Dievaluasi

| # | Faktor | Deskripsi | Bobot |
| :--- | :--- | :--- | ---: |
| 1 | **Trend (EMA)** | Close > EMA20, EMA9 > EMA20, slope EMA20 naik | 20% |
| 2 | **Relative Strength** | Outperform IHSG (excess > 0 + RS naik + percentile universe) | 20% |
| 3 | **Momentum (RSI)** | RSI dalam rentang timeframe & tidak turun drastis | 10% |
| 4 | **MACD** | MACD > Signal, Histogram > 0 & naik | 10% |
| 5 | **Volume** | Body hijau + (ratio ≥1.2× MA20 atau Z ≥1.0) | 15% |
| 6 | **Price Action** | Breakout / Pullback / Pola candlestick | 15% |
| 7 | **Volatility (ATR%)** | ATR% dalam rentang timeframe | 10% |
| — | **Hard Filters** | Struktur tren, Extension, Volatility, Likuiditas, Harga min | N/A |

### Timeframe

| Mode | Trader | Max Hold V1 (time-stop) | Threshold |
| :--- | :--- | :--- | :--- |
| **daily_swing** | Day / Short swing | 10 trading days (10 bar daily) | ≥72/100 |
| **weekly_position** | Swing / Medium position | 8 minggu (8 bar weekly = 40 hari bursa) | ≥70/100 |
| **monthly_long_term** | Position / Long-term | 6 bulan (6 bar monthly = 126 hari bursa) | ≥68/100 |

---

## 📊 Teknis

**Stop & Target:** Berbasis ATR (volatility-adjusted), bukan persentase flat.

| TF | Stop | Target | Min R:R |
| :--- | :--- | :--- | :--- |
| daily_swing | 1.5× ATR | 2× ATR | 1.5:1 |
| weekly_position | 2.0× ATR | 3× ATR | 2.0:1 |
| monthly_long_term | 2.5× ATR | 4× ATR | 2.5:1 |

**Universe:** 900+ saham IDX dari `resource/daftar-saham.xlsx`

---

## 🚀 Cara Menjalankan

### Mode 1 — Pilih timeframe saja → rekomendasi saham + analisa
```bash
# Berjalan otomatis setiap hari jam 16:00 WIB via GitHub Actions
# Hasil → GitHub Step Summary + output/idx-screening.csv

# Manual lokal (3 rekomendasi terbaik sesuai indikator timeframe + analisa):
python scripts/financial_screener.py --trend daily_swing
python scripts/financial_screener.py --trend weekly_position
python scripts/financial_screener.py --trend monthly_long_term
```

### Mode 2 — `all` + kode saham → hasil 3 timeframe + analisa
```bash
# Analisis 1 saham di 3 timeframe sekaligus (daily_swing / weekly_position / monthly_long_term):
python scripts/financial_screener.py --trend all --ticker BBCA
python scripts/financial_screener.py --trend all --ticker BBCA.JK --capital 25_000_000

# Output: output/idx_single_BBCA.csv + report multi-timeframe
```

### ❌ Error — `all` tanpa kode saham
```bash
python scripts/financial_screener.py --trend all
# ❌ Jangan semua timeframe, berat.
#    Pilih satu timeframe (daily_swing/weekly_position/monthly_long_term) atau kombinasikan --trend all --ticker <kode>.
```

> Aturan:
> - `--trend daily_swing|weekly_position|monthly_long_term` (tanpa `--ticker`) → rekomendasi 3 saham + analisa.
> - `--trend all --ticker <kode>` → analisis 1 saham di 3 timeframe + analisa.
> - `--trend all` tanpa `--ticker`, atau `--trend <timeframe>` dengan `--ticker` → error.

### Backtest (Manual)
```bash
# Via GitHub Actions:
# 1. Actions tab → "Backtest IDX Stock Screener" → Run workflow
# 2. Isi: timeframe, dates, top picks, signal step
# 3. Download artifacts (CSV + reports)

# Manual lokal:
python tests/test_financial_screener.py \
  --trend daily_swing \
  --start 2025-01-01 \
  --end 2025-08-18

# Output: output/backtest/
#   - signals_daily_swing.csv (semua signals)
#   - trades_daily_swing.csv (triggered only)
#   - BACKTEST_REPORT_daily_swing.md (summary)
#   - summary_*.csv (grouped metrics)
```

Pastikan `GEMINI_API_KEY` di-set di environment.

---

## 📂 Struktur

```
scripts/
  └── financial_screener.py      ← Main screener (mode timeframe / mode saham)
tests/
  └── test_financial_screener.py ← Backtest engine
.github/workflows/
  ├── financial_screener.yml     ← Daily live (daily_swing) + manual (mode 1/2)
  └── backtest-matrix.yml        ← Backtest 3 timeframe
resource/
  └── daftar-saham.xlsx          ← IDX universe (900+)
output/
  ├── idx-screening.csv          ← Hasil screening mode timeframe
  ├── idx_single_<TICKER>.csv    ← Hasil analisis saham tunggal
  └── backtest/                  ← Hasil backtest
```

---

## ⚙️ Dependensi

```
yfinance>=0.2.0
google-genai>=0.3.0
pandas>=2.0
numpy>=1.24
openpyxl>=3.1
```

Install: `pip install -r requirements.txt`

---

## 📄 Documentation

| File | Isi |
| :--- | :--- |
| **README.md** | Overview, strategy, usage (Anda di sini) |
| **BACKTEST_WORKFLOW_GUIDE.md** | Backtest inputs, outputs, tips |

---

*Disclaimer: Informatif saja, bukan rekomendasi investasi. Selalu backtest dan risk management.*
