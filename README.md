# 📈 CacingNagaPRO - Multi-Factor IDX Stock Screener (V2)

Sistem automasi pemindaian saham IHSG berbasis **Multi-Factor Confluence** dengan **Gemini AI** & GitHub Actions. V2 menghapus indikator redundan (RSI, Stochastic) untuk sinyal berkualitas tinggi.

---

## 🧠 Strategi (5 Faktor Inti)

Setiap saham di-score dari 5 faktor kunci. Hanya yang pass hard filters & score ≥threshold menjadi **Strong Buy**.

### Faktor yang Dievaluasi

| # | Faktor | Deskripsi | Bobot |
| :--- | :--- | :--- | ---: |
| 1 | **Trend (EMA)** | Close > EMA20/50/200 sesuai timeframe | 22% |
| 2 | **MACD** | MACD > Signal, Histogram > 0 & naik | 15% |
| 3 | **Volume** | Spike ≥1.3-1.5× MA20 + bullish candle | 17% |
| 4 | **Price Action** | Breakout / Pullback / Pola candlestick | 17% |
| 5 | **Relative Strength** | Outperform IHSG benchmark | 22% |
| — | **Hard Filters** | Trend struktur, Extension, Volatility, Likuiditas | N/A |

### Timeframe

| Mode | Trader | Hold | Threshold |
| :--- | :--- | :--- | :--- |
| **1hari** | Day | 1-20 bar | ≥72/100 |
| **1minggu** | Swing | 1-65 bar | ≥70/100 |
| **1bulan** | Position | 1-252 bar | ≥68/100 |

---

## 📊 Teknis

**Stop & Target:** Berbasis ATR (volatility-adjusted), bukan persentase flat.

| TF | Stop | Target | Min R:R |
| :--- | :--- | :--- | :--- |
| 1hari | 1.5× ATR | 2× ATR | 1.5:1 |
| 1minggu | 2.0× ATR | 3× ATR | 2.0:1 |
| 1bulan | 2.5× ATR | 4× ATR | 2.5:1 |

**Universe:** 900+ saham IDX dari `resource/daftar-saham.xlsx`

---

## 🚀 Cara Menjalankan

### Mode 1 — Pilih timeframe saja → rekomendasi saham + analisa
```bash
# Berjalan otomatis setiap hari jam 16:00 WIB via GitHub Actions
# Hasil → GitHub Step Summary + output/idx-screening.csv

# Manual lokal (3 rekomendasi terbaik sesuai indikator timeframe + analisa):
python scripts/financial_screener.py --trend 1hari
python scripts/financial_screener.py --trend 1minggu
python scripts/financial_screener.py --trend 1bulan
```

### Mode 2 — `all` + kode saham → hasil 3 timeframe + analisa
```bash
# Analisis 1 saham di 3 timeframe sekaligus (1hari / 1minggu / 1bulan):
python scripts/financial_screener.py --trend all --ticker BBCA
python scripts/financial_screener.py --trend all --ticker BBCA.JK --capital 25_000_000

# Output: output/idx_single_BBCA.csv + report multi-timeframe
```

### ❌ Error — `all` tanpa kode saham
```bash
python scripts/financial_screener.py --trend all
# ❌ Jangan semua timeframe, berat.
#    Pilih satu timeframe (1hari/1minggu/1bulan) atau kombinasikan --trend all --ticker <kode>.
```

> Aturan:
> - `--trend 1hari|1minggu|1bulan` (tanpa `--ticker`) → rekomendasi 3 saham + analisa.
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
  --trend 1hari \
  --start 2025-01-01 \
  --end 2025-08-18

# Output: output/backtest/
#   - signals_1hari.csv (semua signals)
#   - trades_1hari.csv (triggered only)
#   - BACKTEST_REPORT_1hari.md (summary)
#   - summary_*.csv (grouped metrics)
```

Pastikan `GEMINI_API_KEY` di-set di environment.

---

## 📂 Struktur

```
scripts/
  └── financial_screener.py      ← Main screener V2
tests/
  └── test_financial_screener.py ← Backtest engine
.github/workflows/
  ├── financial_screener.yml     ← Daily live
  └── backtest.yml               ← Manual backtest
resource/
  └── daftar-saham.xlsx          ← IDX universe (900+)
output/
  └── backtest/                  ← Generated results
```

---

## 📋 Changelog V1 → V2

### Dihapus (Redundan)
- ❌ **RSI** → Tumpang tindih MACD
- ❌ **Stochastic** → Duplicate oscillator

### Diperkuat
- ✅ **MACD** → Primary momentum (10% → 15%)
- ✅ **Volume** → Higher threshold (15% → 17%)
- ✅ **Price Action** → Pullback pattern added (15% → 17%)

### Expected Improvement
- Win rate: 52% → 58% (+6%)
- Expectancy: 0.40R → 0.55R
- Profit Factor: 1.3 → 1.6+ (sustainable)
- Entry rate: -33% (quality > quantity)

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
| **RELEASE_NOTES_V2.md** | V2 improvements & technical details |
| **BACKTEST_WORKFLOW_GUIDE.md** | Backtest inputs, outputs, tips |

---

*Disclaimer: Informatif saja, bukan rekomendasi investasi. Selalu backtest dan risk management.*
