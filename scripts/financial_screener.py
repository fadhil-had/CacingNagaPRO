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
    """Mengubah data harian menjadi weekly/monthly candle lalu menghitung indikator teknikal inti."""
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

    timeframe_df['EMA20'] = timeframe_df['Close'].ewm(span=20, adjust=False).mean()
    timeframe_df['EMA50'] = timeframe_df['Close'].ewm(span=50, adjust=False).mean()
    timeframe_df['EMA200'] = timeframe_df['Close'].ewm(span=200, adjust=False).mean()
    timeframe_df['RSI'] = hitung_rsi(timeframe_df['Close'], period=14)

    ema12 = timeframe_df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = timeframe_df['Close'].ewm(span=26, adjust=False).mean()
    timeframe_df['MACD'] = ema12 - ema26
    timeframe_df['Signal_Line'] = timeframe_df['MACD'].ewm(span=9, adjust=False).mean()
    timeframe_df['Histogram'] = timeframe_df['MACD'] - timeframe_df['Signal_Line']

    timeframe_df['Vol_MA20'] = timeframe_df['Volume'].rolling(window=20).mean()
    timeframe_df['Vol_MA50'] = timeframe_df['Volume'].rolling(window=50).mean()

    low_14 = timeframe_df['Low'].rolling(window=14).min()
    high_14 = timeframe_df['High'].rolling(window=14).max()
    timeframe_df['Fast_K'] = 100 * ((timeframe_df['Close'] - low_14) / (high_14 - low_14).replace(0, np.nan))
    timeframe_df['Fast_K'] = timeframe_df['Fast_K'].clip(lower=0, upper=100)
    timeframe_df['Slow_K'] = timeframe_df['Fast_K'].rolling(window=3).mean()
    timeframe_df['Slow_D'] = timeframe_df['Slow_K'].rolling(window=3).mean()

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

        total_faktor = 4
        kondisi_teks = []

        # Faktor 1: Trend
        if mode_tren == "1hari":
            trend_ok = (
                (hari_ini['Close'] > hari_ini['EMA20']) and
                (hari_ini['EMA20'] > hari_ini['EMA50']) and
                (hari_ini['EMA50'] > hari_ini['EMA200'])
            )
            label_f1 = "Close > EMA20 > EMA50 > EMA200"
        elif mode_tren == "1minggu":
            trend_ok = (hari_ini['Close'] > hari_ini['EMA50']) and (hari_ini['EMA50'] > hari_ini['EMA200'])
            label_f1 = "Close > EMA50 > EMA200"
        else:
            trend_ok = hari_ini['Close'] > hari_ini['EMA200']
            label_f1 = "Close > EMA200"

        kondisi_teks.append(f"✅ Trend ({label_f1})" if trend_ok else f"❌ Trend ({label_f1})")

        # Faktor 2: Momentum (RSI)
        if mode_tren == "1hari":
            momentum_ok = 40 <= hari_ini['RSI'] <= 85
            momentum_label = f"RSI:{hari_ini['RSI']:.1f}"
        else:
            momentum_ok = 40 <= hari_ini['RSI'] <= 65
            momentum_label = f"RSI:{hari_ini['RSI']:.1f}"

        kondisi_teks.append(
            f"✅ Momentum ({momentum_label})" if momentum_ok else f"❌ Momentum ({momentum_label})"
        )

        # Faktor 3: Convergence (MACD)
        convergence_ok = (hari_ini['MACD'] > hari_ini['Signal_Line']) and (hari_ini['Histogram'] > 0)
        kondisi_teks.append(
            "✅ Convergence (MACD > Signal & Histogram > 0)" if convergence_ok else "❌ Convergence (MACD tidak bullish)"
        )

        # Faktor 4: Volume
        lonjakan_vol = hari_ini['Volume'] / hari_ini['Vol_MA20'] if hari_ini['Vol_MA20'] > 0 else 0
        volume_ok = lonjakan_vol > 1.5
        kondisi_teks.append(
            f"✅ Volume (Lonjakan: {lonjakan_vol:.2f}x)" if volume_ok else f"❌ Volume (Lonjakan: {lonjakan_vol:.2f}x)"
        )

        # Breakout validation for daily
        breakout_ok = True
        prev_high_20 = float(hari_ini['Prev_High_20']) if not pd.isna(hari_ini['Prev_High_20']) else None
        if mode_tren == "1hari":
            if prev_high_20 is None:
                kondisi_teks.append("⚠️ Breakout validation tidak tersedia (Prev_High_20 kosong)")
            else:
                breakout_ok = hari_ini['Close'] > prev_high_20
                kondisi_teks.append(
                    "✅ Breakout terkonfirmasi (Close > Prev_High_20)"
                    if breakout_ok
                    else "❌ Breakout tidak terkonfirmasi (Close belum di atas Prev_High_20)"
                )

        skor = int(trend_ok) + int(momentum_ok) + int(convergence_ok) + int(volume_ok)

        support_level = float(df['Low'].rolling(window=10).min().iloc[-1])
        resistance_level = float(df['High'].rolling(window=10).max().iloc[-1])
        target_price = max(float(hari_ini['Close']) * 1.06, resistance_level * 1.02)
        entry_level = min(float(hari_ini['Close']) * 1.01, resistance_level * 0.99)
        stop_level = support_level * 0.98
        risk_note = (
            "Risiko: breakout bisa gagal jika harga kembali di bawah support terdekat; "
            "cut loss jika harga menembus support dengan volume yang meningkat."
        )

        strong_buy = (skor >= 3 and (breakout_ok or prev_high_20 is None)) if mode_tren == "1hari" else skor == total_faktor
        return {
            "ticker": ticker_code,
            "harga_terakhir": hari_ini['Close'],
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
    
    candidates = []
    for t in pool:
        if t in data_massal and not data_massal[t].empty:
            res = analisa_saham_confluence(t, data_massal[t], args.trend)
            if not res["error"]:
                candidates.append(res)

    strong_buy_candidates = [c for c in candidates if c["status"] == "Strong Buy"]
    top_3 = sorted(strong_buy_candidates, key=lambda x: (x["skor"]/x["total_faktor"], x["lonjakan_volume"]), reverse=True)[:3]

    if not top_3:
        top_3 = sorted(candidates, key=lambda x: (x["skor"]/x["total_faktor"], x["lonjakan_volume"]), reverse=True)[:3]
        print(f"⚠️ Tidak ada Strong Buy untuk {args.trend}; menampilkan kandidat Watchlist terbaik.")

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