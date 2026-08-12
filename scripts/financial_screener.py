import os
import sys
import argparse
import yfinance as yf
import pandas as pd
import numpy as np
from google import genai
from google.genai import types

def hitung_rsi(series, period=14):
    """Menghitung RSI secara lokal menggunakan pandas."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def ambil_semua_ticker_dari_excel(file_path="resource/daftar-saham.xlsx"):
    """Membaca list saham dari kolom 'Kode' di Excel dan menambahkan suffix .JK."""
    try:
        if not os.path.exists(file_path):
            print(f"❌ File '{file_path}' tidak ditemukan!")
            return []
            
        df_excel = pd.read_excel(file_path)
        if 'Kode' not in df_excel.columns:
            print("❌ Kolom 'Kode' tidak ditemukan di file Excel!")
            return []
            
        tickers = df_excel['Kode'].dropna().astype(str).str.strip().str.upper()
        return (tickers + ".JK").tolist()
    except Exception as e:
        print(f"❌ Gagal membaca Excel: {str(e)}")
        return []

def siapkan_data_untuk_timeframe(df_saham, mode_tren):
    """Resample data sesuai timeframe dan hitung seluruh indikator teknikal."""
    required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    df = df_saham.dropna(subset=required_cols).copy()
    if df.empty:
        raise ValueError("Data tidak tersedia")

    if mode_tren == "1hari":
        timeframe_df = df[required_cols].copy()
    elif mode_tren == "1minggu":
        timeframe_df = (
            df[required_cols]
            .resample('W-FRI', label='right', closed='right')
            .agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})
            .dropna()
        )
    else: # 1bulan
        timeframe_df = (
            df[required_cols]
            .resample('ME', label='right', closed='right')
            .agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})
            .dropna()
        )

    min_rows = {"1hari": 200, "1minggu": 52, "1bulan": 24}[mode_tren]
    if len(timeframe_df) < min_rows:
        raise ValueError(f"Data tidak cukup: {len(timeframe_df)} bar (min {min_rows})")

    # Moving Averages
    timeframe_df['EMA9'] = timeframe_df['Close'].ewm(span=9, adjust=False).mean()
    timeframe_df['EMA20'] = timeframe_df['Close'].ewm(span=20, adjust=False).mean()
    timeframe_df['EMA50'] = timeframe_df['Close'].ewm(span=50, adjust=False).mean()
    timeframe_df['EMA200'] = timeframe_df['Close'].ewm(span=200, adjust=False).mean()
    
    # RSI & MACD
    timeframe_df['RSI'] = hitung_rsi(timeframe_df['Close'], period=14)
    ema12 = timeframe_df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = timeframe_df['Close'].ewm(span=26, adjust=False).mean()
    timeframe_df['MACD'] = ema12 - ema26
    timeframe_df['Signal_Line'] = timeframe_df['MACD'].ewm(span=9, adjust=False).mean()
    timeframe_df['Histogram'] = timeframe_df['MACD'] - timeframe_df['Signal_Line']

    # Volume & Stochastic
    timeframe_df['Vol_MA20'] = timeframe_df['Volume'].rolling(window=20).mean()
    low_14 = timeframe_df['Low'].rolling(window=14).min()
    high_14 = timeframe_df['High'].rolling(window=14).max()
    timeframe_df['Fast_K'] = 100 * ((timeframe_df['Close'] - low_14) / (high_14 - low_14).replace(0, np.nan))
    timeframe_df['Slow_K'] = timeframe_df['Fast_K'].clip(lower=0, upper=100).rolling(window=3).mean()
    timeframe_df['Slow_D'] = timeframe_df['Slow_K'].rolling(window=3).mean()

    # ATR (Average True Range)
    tr1 = timeframe_df['High'] - timeframe_df['Low']
    tr2 = (timeframe_df['High'] - timeframe_df['Close'].shift(1)).abs()
    tr3 = (timeframe_df['Low'] - timeframe_df['Close'].shift(1)).abs()
    timeframe_df['ATR'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(window=14).mean()

    # Prev High 20 & Structure
    timeframe_df['Prev_High_20'] = timeframe_df['High'].rolling(window=20).max().shift(1)
    timeframe_df['Body'] = timeframe_df['Close'] - timeframe_df['Open']
    timeframe_df['Upper_Wick'] = timeframe_df['High'] - timeframe_df[['Open', 'Close']].max(axis=1)
    timeframe_df['Lower_Wick'] = timeframe_df[['Open', 'Close']].min(axis=1) - timeframe_df['Low']
    timeframe_df['Candle_Range'] = timeframe_df['High'] - timeframe_df['Low']

    return timeframe_df


def deteksi_pola_candle(hari_ini, kemarin):
    """Mendeteksi pola candlestick bullish reversal/continuation."""
    body = float(hari_ini['Body'])
    candle_range = float(hari_ini['Candle_Range']) if float(hari_ini['Candle_Range']) > 0 else 0.0001
    lower_wick = float(hari_ini['Lower_Wick'])
    upper_wick = float(hari_ini['Upper_Wick'])
    kemarin_body = float(kemarin['Body'])

    bullish_engulfing = (
        body > 0 and kemarin_body < 0 and
        float(hari_ini['Open']) < float(kemarin['Close']) and
        float(hari_ini['Close']) > float(kemarin['Open'])
    )
    hammer = (
        body > 0 and lower_wick >= 2 * abs(body) and
        upper_wick <= abs(body) * 0.5 and (lower_wick / candle_range) >= 0.55
    )
    marubozu = (body > 0 and (abs(body) / candle_range) >= 0.75)
    strong_close = ((float(hari_ini['Close']) - float(hari_ini['Low'])) / candle_range) >= 0.60 if candle_range > 0 else False

    if bullish_engulfing: return "Bullish Engulfing", True
    if marubozu: return "Marubozu Bullish", True
    if hammer: return "Hammer", True
    if strong_close and body > 0: return "Strong Bullish Close", True
    return "Tidak ada pola bullish kuat", False


def analisa_saham_confluence(ticker_code, df_saham, mode_tren):
    """Screening multi-factor confluence terkalibrasi per timeframe."""
    try:
        df = siapkan_data_untuk_timeframe(df_saham, mode_tren)
        hari_ini = df.iloc[-1]
        kemarin = df.iloc[-2]
        kondisi_teks = []

        # ── 1. HARD FILTERS (PERSYARATAN MUTLAK) ──────────────────────────────
        if mode_tren == "1hari":
            hard_ema = (hari_ini['Close'] > hari_ini['EMA200']) and (hari_ini['EMA20'] > hari_ini['EMA50'])
            jarak_ema9 = (hari_ini['Close'] - hari_ini['EMA9']) / hari_ini['EMA9']
            not_extended = jarak_ema9 <= 0.035
            candle_range = float(hari_ini['Candle_Range']) if float(hari_ini['Candle_Range']) > 0 else 0.0001
            valid_wick = (float(hari_ini['Upper_Wick']) / candle_range) <= 0.35
            hard_pass = hard_ema and not_extended and valid_wick
        elif mode_tren == "1minggu":
            hard_ema = (hari_ini['Close'] > hari_ini['EMA50']) and (hari_ini['EMA20'] > hari_ini['EMA50'])
            not_extended = ((hari_ini['Close'] - hari_ini['EMA9']) / hari_ini['EMA9']) <= 0.06
            hard_pass = hard_ema and not_extended
        else: # 1bulan
            hard_pass = (hari_ini['Close'] > hari_ini['EMA20']) and (hari_ini['EMA20'] > hari_ini['EMA50'])

        # ── 2. SCORING FAKTOR TIKER ───────────────────────────────────────────
        # Trend
        trend_ok = (hari_ini['Close'] > hari_ini['EMA20']) and (hari_ini['EMA9'] > hari_ini['EMA20'])
        kondisi_teks.append(f"{'✅' if trend_ok else '❌'} Trend (Close>EMA20 & EMA9>EMA20)")

        # Momentum RSI
        rsi_now = float(hari_ini['RSI'])
        rsi_prev = float(kemarin['RSI']) if not pd.isna(kemarin['RSI']) else rsi_now
        rsi_bounds = {"1hari": (50, 70), "1minggu": (45, 70), "1bulan": (40, 65)}[mode_tren]
        momentum_ok = (rsi_bounds[0] <= rsi_now <= rsi_bounds[1]) and (rsi_now > rsi_prev)
        kondisi_teks.append(f"{'✅' if momentum_ok else '❌'} Momentum (RSI:{rsi_now:.1f} zone {rsi_bounds[0]}-{rsi_bounds[1]})")

        # MACD
        hist_now = float(hari_ini['Histogram'])
        hist_prev = float(kemarin['Histogram']) if not pd.isna(kemarin['Histogram']) else hist_now
        convergence_ok = (hari_ini['MACD'] > hari_ini['Signal_Line']) and (hist_now > 0) and (hist_now > hist_prev)
        kondisi_teks.append(f"{'✅' if convergence_ok else '❌'} MACD (Hist:{hist_now:.4f} ↑)")

        # Volume
        vol_ratio = float(hari_ini['Volume']) / float(hari_ini['Vol_MA20']) if float(hari_ini['Vol_MA20']) > 0 else 0
        vol_threshold = {"1hari": 1.5, "1minggu": 1.3, "1bulan": 1.2}[mode_tren]
        volume_ok = (vol_ratio >= vol_threshold) and (float(hari_ini['Body']) > 0)
        kondisi_teks.append(f"{'✅' if volume_ok else '❌'} Volume ({vol_ratio:.2f}x MA20)")

        # Stochastic (Abaikan untuk Monthly)
        if mode_tren in ("1hari", "1minggu"):
            sk = float(hari_ini['Slow_K']) if not pd.isna(hari_ini['Slow_K']) else 50
            sd = float(hari_ini['Slow_D']) if not pd.isna(hari_ini['Slow_D']) else 50
            stoch_ceiling = 80 if mode_tren == "1hari" else 85
            stoch_ok = (sk > sd) and (sk <= stoch_ceiling)
            kondisi_teks.append(f"{'✅' if stoch_ok else '❌'} Stochastic (K:{sk:.1f} D:{sd:.1f})")
        else:
            stoch_ok = True

        # Breakout & Pattern
        prev_high_20 = float(hari_ini['Prev_High_20']) if not pd.isna(hari_ini['Prev_High_20']) else float(hari_ini['Close'])
        breakout_ok = float(hari_ini['Close']) > prev_high_20
        nama_pola, pola_candle_ok = deteksi_pola_candle(hari_ini, kemarin)
        kondisi_teks.append(f"{'✅' if (breakout_ok or pola_candle_ok) else '❌'} Pattern/Breakout ({nama_pola})")

        total_faktor = 6 if mode_tren in ("1hari", "1minggu") else 5
        skor = (int(trend_ok) + int(momentum_ok) + int(convergence_ok) + 
                int(volume_ok) + (int(stoch_ok) if mode_tren != "1bulan" else 0) + int(pola_candle_ok))

        # ── 3. STRONG BUY CRITERIA ────────────────────────────────────────────
        min_score = 5 if mode_tren == "1hari" else 4
        strong_buy = hard_pass and (skor >= min_score)

        # ── 4. LEVEL HARGA BERBASIS ATR ───────────────────────────────────────
        atr = float(hari_ini['ATR']) if not pd.isna(hari_ini['ATR']) else float(hari_ini['Close']) * 0.02
        close = float(hari_ini['Close'])

        sr_window = {"1hari": 10, "1minggu": 8, "1bulan": 6}[mode_tren]
        support_level = float(df['Low'].rolling(window=sr_window).min().iloc[-1])
        resistance_level = float(df['High'].rolling(window=sr_window).max().iloc[-1])

        stop_mult = {"1hari": 1.5, "1minggu": 2.0, "1bulan": 2.5}[mode_tren]
        target_mult = {"1hari": 2.5, "1minggu": 3.5, "1bulan": 4.5}[mode_tren]

        entry_level = close
        stop_level = max(support_level * 0.98, close - (stop_mult * atr))
        target_price = max(resistance_level * 1.02, entry_level + (target_mult * atr))

        risk_reward = (target_price - entry_level) / (entry_level - stop_level) if (entry_level - stop_level) > 0 else 0
        risk_note = f"ATR({mode_tren}): {atr:.2f} | Stop Loss: Rp {stop_level:,.0f} | Target: Rp {target_price:,.0f}"

        return {
            "ticker": ticker_code,
            "harga_terakhir": close,
            "support_level": support_level,
            "resistance_level": resistance_level,
            "entry_level": entry_level,
            "stop_level": stop_level,
            "target_price": target_price,
            "risk_note": risk_note,
            "skor": skor,
            "total_faktor": total_faktor,
            "status": "Strong Buy" if strong_buy else "Watchlist",
            "kondisi_detail": ", ".join(kondisi_teks),
            "lonjakan_volume": vol_ratio,
            "risk_reward": risk_reward,
            "error": False
        }
    except Exception as e:
        return {"error": True, "alasan": str(e)}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--trend', choices=['1hari', '1minggu', '1bulan'], default='1hari')
    args = parser.parse_args()
    
    pool = ambil_semua_ticker_dari_excel("resource/daftar-saham.xlsx")
    if not pool:
        return
        
    print(f"📥 Unduh data historis massal ({len(pool)} saham) - Mode: {args.trend}...")
    data_massal = yf.download(pool, period="5y", interval="1d", group_by='ticker', progress=False, threads=True)
    
    candidates = []
    for t in pool:
        if t in data_massal and not data_massal[t].empty:
            res = analisa_saham_confluence(t, data_massal[t], args.trend)
            if not res["error"]:
                candidates.append(res)

    strong_buy_candidates = [c for c in candidates if c["status"] == "Strong Buy"]
    top_3 = sorted(
        strong_buy_candidates,
        key=lambda x: (x["skor"] / x["total_faktor"], x.get("risk_reward", 0), x["lonjakan_volume"]),
        reverse=True
    )[:3]

    if not top_3:
        top_3 = sorted(
            candidates,
            key=lambda x: (x["skor"] / x["total_faktor"], x.get("risk_reward", 0), x["lonjakan_volume"]),
            reverse=True
        )[:3]
        print(f"⚠️ Tidak ada Strong Buy yang lolos kriteria ketat untuk {args.trend}; menampilkan kandidat Watchlist terbaik.")

    prompt_data = f"PERSPEKTIF TREN TRADING: {args.trend.upper()}\n\n"
    for s in top_3:
        ticker_obj = yf.Ticker(s['ticker'])
        berita_terbaru = ticker_obj.news
        teks_berita = ""
        if berita_terbaru:
            for item in berita_terbaru[:2]:
                teks_berita += f"- {item.get('title')} ({item.get('publisher')})\n"
        else:
            teks_berita = "- Tidak ada berita terbaru.\n"

        prompt_data += f"""
        Ticker: {s['ticker']} | Status Teknikal: {s['status']} (Score {s['skor']}/{s['total_faktor']})
        Harga Terakhir: Rp {s['harga_terakhir']:,.2f}
        Support: Rp {s['support_level']:,.2f} | Resistance: Rp {s['resistance_level']:,.2f}
        Entry: Rp {s['entry_level']:,.2f} | Stop: Rp {s['stop_level']:,.2f} | Target: Rp {s['target_price']:,.2f}
        Risk/Reward: {s.get('risk_reward', 0):.1f}:1
        Risiko: {s['risk_note']}
        Kondisi Faktor: {s['kondisi_detail']}
        Berita Terkini:
        {teks_berita}
        ---
        """

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("❌ API Key tidak ditemukan.")
        return
    client = genai.Client(api_key=api_key)

    system_instruction = (
        f"Anda adalah sistem analis otomatis portofolio saham IDX.\n"
        f"Perspektif analisis: {args.trend.upper()}\n\n"
        "FORMAT OUTPUT (langsung tanpa kalimat pembuka):\n\n"
        f"### 📊 IDX Stock Report — {args.trend.upper()} (Top Picks)\n"
        "| Ticker | Status | Score | Close | Entry | Stop | Target | R:R |\n"
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        "| [KODE] | [Strong Buy/Watchlist] | [X/Y] | [Harga] | [Angka] | [Angka] | [Angka] | [X:1] |\n\n"
        "**Executive Summary:**\n"
        "- 2-3 poin singkat: alasan bullish utama, konfirmasi volume/candle, sentimen berita.\n"
        "- Per ticker: 1 kalimat — alasan masuk, level kritis yang harus dijaga, dan kapan exit.\n\n"
        "**Watchlist Note** (jika ada Watchlist di data):\n"
        "- Sebutkan faktor apa yang belum terpenuhi dan kondisi apa yang harus muncul agar layak entry.\n\n"
        "Akhiri dengan 1 kalimat disclaimer trading singkat."
    )

    print("🤖 Mengirimkan ke Gemini AI...")
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt_data,
            config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.2)
        )
        print(response.text)
    except Exception as e:
        print(f"❌ Gemini API gagal: {str(e)}")

if __name__ == "__main__":
    main()