# 📈 CacingNagaPRO - Multi-Factor IDX Stock Screener

Sistem automasi pemindaian saham Bursa Efek Indonesia (IHSG) berbasis strategi **Multi-Factor Confluence** yang terintegrasi secara *native* dengan **Gemini AI** dan berjalan otomatis menggunakan **GitHub Actions**.

---

## 📂 Struktur Proyek

```text
CacingNagaPRO/
├── .github/
│   └── workflows/
│       └── financial_screener.yml  # Pengatur jadwal otomatis harian GitHub
├── resource/
│   └── daftar-saham.xlsx          # Database list kode saham IHSG (900+)
├── scripts/
│   ├── financial_screener.py      # Skrip utama backend (GitHub Actions)
│   └── financial_screener_app.py  # Skrip antarmuka web visual (Streamlit lokal)
├── main/
│   └── financial_analysis.py      # Analisis fundamental berbasis Streamlit
├── README.md                      # Dokumentasi proyek
└── requirements.txt               # Daftar pustaka / dependensi Python
```

---

## 🧠 Strategi Screening: Multi-Factor Confluence

Setiap saham dianalisa menggunakan **confluence multi-faktor** yang dikalibrasi per timeframe. Saham hanya masuk kategori **Strong Buy** jika faktor-faktor kunci terpenuhi secara bersamaan — bukan hanya satu indikator.

### Timeframe yang Didukung

| Mode | Tipe Trader | Hold Period | Strong Buy Gate |
| :--- | :--- | :--- | :--- |
| `--trend 1hari` | Day Trader | 1–2 hari | ≥4/6 faktor + breakout harian |
| `--trend 1minggu` | Swing Trader | 1–2 minggu | ≥4/6 faktor + breakout mingguan |
| `--trend 1bulan` | Position Trader | 1–3 bulan | ≥4/5 faktor |

---

## 📊 Faktor Teknikal per Timeframe

### Faktor 1 — Trend (Struktur EMA)
| Timeframe | Kondisi Hard | Konteks |
| :--- | :--- | :--- |
| Daily | Close > EMA20 > EMA50 > EMA200 | EMA9 > EMA20 sebagai momentum jangka pendek |
| Weekly | Close > EMA20 > EMA50 | EMA200 ditampilkan jika tersedia ≥200 bar (~4 tahun) |
| Monthly | Close > EMA20 > EMA50 | EMA200 tidak digunakan (butuh 16+ tahun data) |

### Faktor 2 — Momentum (RSI + Arah)
| Timeframe | Zona RSI | Syarat Tambahan |
| :--- | :--- | :--- |
| Daily | 45–75 | RSI harus naik (↑) dari bar sebelumnya |
| Weekly | 45–70 | RSI harus naik (↑) |
| Monthly | 40–65 | RSI harus naik (↑) |

### Faktor 3 — MACD (Posisi + Arah Histogram)
- MACD > Signal Line
- Histogram > 0
- Histogram **harus naik** dari bar sebelumnya (momentum bertambah, bukan melemah)

### Faktor 4 — Volume (Spike + Konfirmasi Candle)
| Timeframe | Threshold Spike | Syarat Candle |
| :--- | :--- | :--- |
| Daily | ≥ 1.5× MA20 | Candle harus hijau (Close > Open) |
| Weekly | ≥ 1.3× MA20 | Candle harus hijau |
| Monthly | ≥ 1.3× MA20 | Candle harus hijau |

### Faktor 5 — Stochastic (Daily & Weekly only)
- %K > %D
- %K sedang naik dari bar sebelumnya
- %K ≤ 80 (daily) / ≤ 85 (weekly) — tidak overbought

### Faktor 6 — Breakout + Pola Candlestick
**Breakout** (daily & weekly, wajib untuk Strong Buy):
- Close > PrevHigh20 (high tertinggi 20 bar sebelumnya, di-shift 1 agar tidak bocor)
- Jika data tidak tersedia → `breakout_ok = False` (tidak default True)

**Pola Candlestick** (semua timeframe, candle monthly paling berbobot):
- Bullish Engulfing
- Marubozu Bullish (body ≥ 75% dari range)
- Hammer (lower wick ≥ 2× body)
- Strong Bullish Close (close di atas 60% dari range candle)

---

## 💰 Level Harga Berbasis ATR

Stop loss dan target dihitung menggunakan **ATR (Average True Range)** — bukan persentase flat — agar proporsional dengan volatilitas masing-masing saham dan timeframe.

| Timeframe | Stop Multiplier | Target Multiplier | Min R:R |
| :--- | :--- | :--- | :--- |
| Daily | 1.5× ATR | 2× ATR | 1.5:1 |
| Weekly | 2.0× ATR | 3× ATR | 2.0:1 |
| Monthly | 2.5× ATR | 4× ATR | 2.5:1 |

Support/Resistance window: Daily=10 bar, Weekly=8 bar, Monthly=6 bar.

---

## 🤖 Integrasi Gemini AI

Top 3 kandidat dikirim ke **Gemini 2.5 Flash** dengan system prompt yang dikalibrasi per timeframe:
- Deskripsi persona trader yang sesuai (Day / Swing / Position)
- Validasi harga (Stop WAJIB di bawah support, Target WAJIB di atas resistance)
- Flagging R:R jika < threshold minimum
- Output tabel markdown + Executive Summary + Watchlist Note (faktor apa yang belum terpenuhi)
- Berita terkini per ticker diikutsertakan sebagai konteks sentimen

---

## 🚀 Cara Menjalankan

### GitHub Actions (Otomatis)
Screener berjalan terjadwal via `.github/workflows/financial_screener.yml`. Hasil laporan muncul di tab **Summary** setiap GitHub Actions run.

### Lokal
```bash
# Install dependensi
pip install -r requirements.txt

# Jalankan screener
python scripts/financial_screener.py --trend 1hari    # Day trading
python scripts/financial_screener.py --trend 1minggu  # Swing trading
python scripts/financial_screener.py --trend 1bulan   # Position trading
```

Pastikan environment variable `GEMINI_API_KEY` sudah di-set.

---

## ⚙️ Dependensi

```
yfinance
google-genai
pandas
numpy
openpyxl
```

---

*Disclaimer: Laporan ini bersifat informatif dan bukan merupakan rekomendasi investasi. Selalu lakukan riset mandiri sebelum mengambil keputusan trading.*
