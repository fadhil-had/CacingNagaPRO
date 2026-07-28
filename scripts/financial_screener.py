import os
import sys
import argparse
import yfinance as yf
import pandas as pd
import numpy as np
from google import genai
from google.genai import types

def hitung_rsi(series, period=14):
    """Menghitung RSI secara lokal menggunakan pandas"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def ambil_semua_ticker_dari_excel(file_path="resource/daftar-saham.xlsx"):
    """Membaca semua list saham dari kolom 'Kode' di file Excel dan menambahkan .JK"""
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
    """Mengubah data harian menjadi weekly/monthly candle lalu menghitung indikator teknikal."""
    required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    df = df_saham.dropna(subset=required_cols).copy()
    if df.empty:
        raise ValueError("Data tidak tersedia")

    if mode_tren == "1hari":
        timeframe_df = df[required_cols]
    elif mode_tren == "1minggu":
        timeframe_df = (
            df[required_cols]
            .resample('W-FRI', label='right', closed='right')
            .agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})
            .dropna()
        )
    else:
        timeframe_df = (
            df[required_cols]
            .resample('ME', label='right', closed='right')
            .agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})
            .dropna()
        )

    if timeframe_df.empty:
        raise ValueError("Tidak ada data timeframe")

    min_rows = {"1hari": 120, "1minggu": 8, "1bulan": 3}[mode_tren]
    if len(timeframe_df) < min_rows:
        raise ValueError("Data tidak cukup untuk indikator")

    # --- 1. HITUNG INDIKATOR UTAMA ---
    timeframe_df['EMA20'] = timeframe_df['Close'].ewm(span=20, adjust=False).mean()
    timeframe_df['EMA50'] = timeframe_df['Close'].ewm(span=50, adjust=False).mean()
    timeframe_df['EMA200'] = timeframe_df['Close'].ewm(span=200, adjust=False).mean()
    timeframe_df['RSI'] = hitung_rsi(timeframe_df['Close'], period=14)

    # MACD
    ema12 = timeframe_df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = timeframe_df['Close'].ewm(span=26, adjust=False).mean()
    timeframe_df['MACD'] = ema12 - ema26
    timeframe_df['Signal_Line'] = timeframe_df['MACD'].ewm(span=9, adjust=False).mean()
    timeframe_df['Histogram'] = timeframe_df['MACD'] - timeframe_df['Signal_Line']

    # Volume
    timeframe_df['Vol_MA20'] = timeframe_df['Volume'].rolling(window=20).mean()

    # Bollinger Bands (20, 2)
    timeframe_df['BB_Mid'] = timeframe_df['Close'].rolling(window=20).mean()
    timeframe_df['BB_Std'] = timeframe_df['Close'].rolling(window=20).std()
    timeframe_df['BB_Upper'] = timeframe_df['BB_Mid'] + (2 * timeframe_df['BB_Std'])
    timeframe_df['BB_Lower'] = timeframe_df['BB_Mid'] - (2 * timeframe_df['BB_Std'])

    # ATR (14)
    high_low = timeframe_df['High'] - timeframe_df['Low']
    high_close = (timeframe_df['High'] - timeframe_df['Close'].shift(1)).abs()
    low_close = (timeframe_df['Low'] - timeframe_df['Close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    timeframe_df['ATR14'] = tr.rolling(window=14).mean()

    # Slow Stochastic (14, 3, 3)
    low_14 = timeframe_df['Low'].rolling(window=14).min()
    high_14 = timeframe_df['High'].rolling(window=14).max()
    timeframe_df['Fast_K'] = 100 * ((timeframe_df['Close'] - low_14) / (high_14 - low_14).replace(0, np.nan))
    timeframe_df['Fast_K'] = timeframe_df['Fast_K'].clip(lower=0, upper=100)
    timeframe_df['Slow_K'] = timeframe_df['Fast_K'].rolling(window=3).mean()
    timeframe_df['Slow_D'] = timeframe_df['Slow_K'].rolling(window=3).mean()

    # Breakout confirmation
    timeframe_df['Prev_High_20'] = timeframe_df['High'].rolling(window=20).max().shift(1)
    timeframe_df['Prev_Low_20'] = timeframe_df['Low'].rolling(window=20).min().shift(1)

    return timeframe_df


def analisa_saham_confluence(ticker_code, df_saham, mode_tren):
    """Melakukan screening teknikal harian yang lebih konservatif dan terkonfirmasi."""
    try:
        df = siapkan_data_untuk_timeframe(df_saham, mode_tren)

        hari_ini = df.iloc[-1]
        kemarin = df.iloc[-2]
        prev2 = df.iloc[-3]
        prev3 = df.iloc[-4]

        # --- 2. EVALUASI FAKTOR CONFLUENCE ---
        skor = 0
        total_faktor = 4
        kondisi_teks = []

        # Faktor 1: Trend
        if mode_tren == "1hari":
            f1_cond = (
                (hari_ini['Close'] > hari_ini['EMA20']) and
                (hari_ini['EMA20'] > hari_ini['EMA50']) and
                (hari_ini['EMA50'] > hari_ini['EMA200']) and
                (hari_ini['Close'] > kemarin['Close'])
            )
            label_f1 = "Close > EMA20 > EMA50 > EMA200 dan candle naik"
        elif mode_tren == "1minggu":
            f1_cond = (hari_ini['Close'] > hari_ini['EMA50']) and (hari_ini['EMA50'] > hari_ini['EMA200'])
            label_f1 = "Close > EMA50 AND EMA50 > EMA200"
        else:  # 1bulan
            f1_cond = (hari_ini['Close'] > hari_ini['EMA200'])
            label_f1 = "Close > EMA200"

        if f1_cond:
            skor += 1
            kondisi_teks.append(f"✅ Trend ({label_f1})")
        else:
            kondisi_teks.append(f"❌ Trend ({label_f1})")

        # Faktor 2: Momentum (RSI & Stochastic)
        if mode_tren == "1hari":
            rsi_ok = 58 <= hari_ini['RSI'] <= 72
            stoch_ok = (hari_ini['Slow_K'] > hari_ini['Slow_D']) and (hari_ini['Slow_K'] >= 60)
            if rsi_ok and stoch_ok:
                skor += 1
                kondisi_teks.append(f"✅ Momentum (RSI:{hari_ini['RSI']:.1f} & Stoch:{hari_ini['Slow_K']:.1f} bullish)")
            else:
                kondisi_teks.append(f"❌ Momentum (RSI:{hari_ini['RSI']:.1f}, Stoch:{hari_ini['Slow_K']:.1f})")
        elif mode_tren == "1minggu":
            stoch_bullish = (hari_ini['Slow_K'] > hari_ini['Slow_D'])
            stoch_aman = (hari_ini['Slow_K'] <= 45)
            if (45 <= hari_ini['RSI'] <= 60) and stoch_bullish and stoch_aman:
                skor += 1
                kondisi_teks.append(f"✅ Momentum (RSI:{hari_ini['RSI']:.1f} & Stochastic Bullish di Area Bawah)")
            else:
                kondisi_teks.append("❌ Momentum (Gagal kombinasi RSI/Stochastic Accumulation)")
        else:
            stoch_bullish = (hari_ini['Slow_K'] > hari_ini['Slow_D'])
            stoch_aman = (hari_ini['Slow_K'] <= 40)
            if (45 <= hari_ini['RSI'] <= 60) and stoch_bullish and stoch_aman:
                skor += 1
                kondisi_teks.append(f"✅ Momentum (45 <= RSI:{hari_ini['RSI']:.1f} <= 60 & Stoch Bullish)")
            else:
                kondisi_teks.append(f"❌ Momentum (RSI:{hari_ini['RSI']:.1f} di luar range 45-60)")

        # Faktor 3: Convergence (MACD)
        if hari_ini['MACD'] > hari_ini['Signal_Line'] and hari_ini['Histogram'] > kemarin['Histogram']:
            skor += 1
            kondisi_teks.append("✅ Convergence (MACD > Signal & Histogram Akselerasi)")
        else:
            kondisi_teks.append("❌ Convergence (Momentum MACD melemah/bearish)")

        # Faktor 4: Volume
        lonjakan_vol = hari_ini['Volume'] / hari_ini['Vol_MA20'] if hari_ini['Vol_MA20'] > 0 else 0
        if mode_tren == "1hari":
            vol_ok = lonjakan_vol > 2.2 and hari_ini['Volume'] > kemarin['Volume']
        elif mode_tren == "1minggu":
            vol_ok = lonjakan_vol > 2.0
        else:
            vol_ok = lonjakan_vol > 2.2
        if vol_ok:
            skor += 1
            kondisi_teks.append(f"✅ Volume (Lonjakan: {lonjakan_vol:.2f}x)")
        else:
            kondisi_teks.append("❌ Volume")

        # Faktor 5: Khusus 1hari (Breakout terkonfirmasi)
        if mode_tren == "1hari":
            total_faktor = 6
            close_above_prev_high = hari_ini['Close'] > hari_ini['Prev_High_20']
            close_above_ema20 = hari_ini['Close'] > hari_ini['EMA20']
            close_up_3_candle = (hari_ini['Close'] > kemarin['Close']) and (kemarin['Close'] > prev2['Close']) and (prev2['Close'] > prev3['Close'])
            body_strength = ((hari_ini['Close'] - hari_ini['Open']) / max(hari_ini['High'] - hari_ini['Low'], 1e-6)) > 0.6
            if close_above_prev_high and close_above_ema20 and close_up_3_candle and body_strength:
                skor += 1
                kondisi_teks.append("✅ Breakout (Close di atas high 20-bar, EMA20, dan 3 candle naik)")
            else:
                kondisi_teks.append("❌ Breakout (Belum terkonfirmasi)")

        # Faktor 6: Khusus 1hari (Volatility & support-resistance sanity check)
        if mode_tren == "1hari":
            atr_ok = hari_ini['ATR14'] > 0 and (hari_ini['Close'] - hari_ini['BB_Lower']) > (0.7 * hari_ini['ATR14'])
            if atr_ok:
                skor += 1
                kondisi_teks.append("✅ Volatilitas (Breakout cukup kuat & tidak tipis)")
            else:
                kondisi_teks.append("❌ Volatilitas (Breakout terlalu tipis)")

        support_level = float(df['Low'].rolling(window=10).min().iloc[-1])
        resistance_level = float(df['High'].rolling(window=10).max().iloc[-1])
        target_price = max(float(hari_ini['Close']) * 1.06, resistance_level * 1.02)
        risk_note = (
            "Risiko: breakout bisa gagal jika harga kembali di bawah support terdekat; "
            "keluar posisi saat price action melemah atau volume menurun."
        )

        strong_buy_threshold = 6 if mode_tren == "1hari" else 4
        return {
            "ticker": ticker_code,
            "harga_terakhir": hari_ini['Close'],
            "support_level": support_level,
            "resistance_level": resistance_level,
            "target_price": target_price,
            "risk_note": risk_note,
            "skor": skor,
            "total_faktor": total_faktor,
            "status": "Strong Buy" if skor >= strong_buy_threshold else "Watchlist",
            "kondisi_detail": ", ".join(kondisi_teks),
            "lonjakan_volume": lonjakan_vol,
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
        
    print(f"📥 Mengunduh data historis massal untuk {len(pool)} saham...")
    data_massal = yf.download(pool, period="5y", interval="1d", group_by='ticker', progress=False, threads=True)
    
    saham_lolos = []
    for t in pool:
        if t in data_massal and not data_massal[t].empty:
            res = analisa_saham_confluence(t, data_massal[t], args.trend)
            if not res["error"] and res["status"] == "Strong Buy":
                saham_lolos.append(res)
                
    top_3 = sorted(saham_lolos, key=lambda x: (x["skor"]/x["total_faktor"], x["lonjakan_volume"]), reverse=True)[:3]

    if not top_3:
        print(f"❌ Tidak ada saham yang lolos kriteria {args.trend}.")
        return

    prompt_data = f"PERSPEKTIF TREN TRADING: {args.trend.upper()}\n\n"
    for s in top_3:
        ticker_obj = yf.Ticker(s['ticker'])
        berita_terbaru = ticker_obj.news
        teks_berita = ""
        if berita_terbaru:
            for item in berita_terbaru[:2]:
                teks_berita += f"- {item.get('title')} ({item.get('publisher')})\n"
        else:
            teks_berita = "- Tidak ada berita terbaru harian.\n"

        prompt_data += f"""
        Ticker: {s['ticker']} | Status Teknikal: {s['status']} (Score {s['skor']}/{s['total_faktor']})
        Harga Terakhir: Rp {s['harga_terakhir']:,.2f}
        Support: Rp {s['support_level']:,.2f}
        Resistance: Rp {s['resistance_level']:,.2f}
        Target: Rp {s['target_price']:,.2f}
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

    timeframe_desc = {
        "1hari": "Day Trader kilat (target keluar-masuk 1-2 hari dengan konfirmasi Breakout Bollinger Bands).",
        "1minggu": "Swing Trader jangka pendek (target hold 1 minggu/5 hari bursa dengan konfirmasi presisi Slow Stochastic Pullback).",
        "1bulan": "Position Trader / Swing Jangka Menengah (target hold 2-4 minggu)."
    }

    system_instruction = (
        f"Anda adalah sistem analis otomatis portofolio saham. Sesuaikan gaya analisis Anda untuk perspektif {timeframe_desc[args.trend]}\n"
        "Tugas Anda memvalidasi data teknikal serta ulasan berita yang dikirimkan untuk menghasilkan keputusan pasar final yang tajam dan bisa dipakai aksi.\n\n"
        "ATURAN DAN DEFINISI HARGA:\n"
        "- Support: Area lantai harga terdekat yang penting.\n"
        "- Resist: Area atap/resistance terdekat yang menahan harga saat ini.\n"
        "- Entry: Harga masuk yang masuk akal di atas support dan di bawah resistance.\n"
        "- Stop: Level cut loss yang aman, biasanya di bawah support terdekat.\n"
        "- Target: Target harga take profit WAJIB lebih tinggi dari Close saat ini dan lebih tinggi dari Resistance terdekat, menunjukkan potensi lanjut setelah breakout.\n\n"
        "Format output WAJIB langsung menghasilkan ringkasan eksekutif yang rapi, singkat, dan siap pakai:\n\n"
        "### 📊 IDX Stock Report (Top Picks)\n"
        "| Ticker | Status | Close | Entry | Stop | Target |\n"
        "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
        "| [KODE] | [Strong Buy / Watchlist] | [Harga] | [Angka] | [Angka] | [Angka] |\n\n"
        "**Executive Summary:**\n"
        "- Berikan 2-3 poin singkat tentang alasan utama bullish/bearish, konfirmasi volume, dan sentimen berita.\n"
        "- Untuk setiap ticker, sertakan 1 kalimat ringkas: 'Entry, Stop, Target, dan alasan masuk'.\n\n"
        "Akhiri dengan 1 kalimat disclaimer trading singkat."
    )

    print("🤖 Mengirimkan ke Gemini AI...")
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt_data,
        config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.2)
    )
    
    print(response.text)

    summary_file_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file_path:
        with open(summary_file_path, "a", encoding="utf-8") as f:
            f.write("\n" + response.text + "\n")

if __name__ == "__main__":
    main()