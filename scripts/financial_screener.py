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
        timeframe_df = df[required_cols].copy()
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

    # Weekly butuh min 52 bar agar EMA50 dan ATR bermakna
    # Monthly butuh min 24 bar agar EMA20 dan Stochastic bermakna
    min_rows = {"1hari": 120, "1minggu": 52, "1bulan": 24}[mode_tren]
    if len(timeframe_df) < min_rows:
        raise ValueError(f"Data tidak cukup: {len(timeframe_df)} bar (min {min_rows})")

    timeframe_df['EMA9'] = timeframe_df['Close'].ewm(span=9, adjust=False).mean()
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

    # ATR (Average True Range) untuk stop loss berbasis volatilitas
    tr1 = timeframe_df['High'] - timeframe_df['Low']
    tr2 = (timeframe_df['High'] - timeframe_df['Close'].shift(1)).abs()
    tr3 = (timeframe_df['Low'] - timeframe_df['Close'].shift(1)).abs()
    timeframe_df['ATR'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(window=14).mean()

    # Prev_High_20 dan Prev_Low_20 (shift 1 agar tidak bocor data hari ini)
    timeframe_df['Prev_High_20'] = timeframe_df['High'].rolling(window=20).max().shift(1)
    timeframe_df['Prev_Low_20'] = timeframe_df['Low'].rolling(window=20).min().shift(1)

    # Body candle untuk deteksi pola
    timeframe_df['Body'] = timeframe_df['Close'] - timeframe_df['Open']
    timeframe_df['Body_Pct'] = timeframe_df['Body'] / timeframe_df['Open'].replace(0, np.nan)
    timeframe_df['Upper_Wick'] = timeframe_df['High'] - timeframe_df[['Open', 'Close']].max(axis=1)
    timeframe_df['Lower_Wick'] = timeframe_df[['Open', 'Close']].min(axis=1) - timeframe_df['Low']
    timeframe_df['Candle_Range'] = timeframe_df['High'] - timeframe_df['Low']

    return timeframe_df


def deteksi_pola_candle(hari_ini, kemarin):
    """
    Mendeteksi pola candlestick bullish reversal/continuation yang relevan untuk daily trading.
    Mengembalikan nama pola dan bool konfirmasi.
    """
    body = float(hari_ini['Body'])
    body_pct = float(hari_ini['Body_Pct']) if not pd.isna(hari_ini['Body_Pct']) else 0
    candle_range = float(hari_ini['Candle_Range']) if float(hari_ini['Candle_Range']) > 0 else 0.0001
    lower_wick = float(hari_ini['Lower_Wick'])
    upper_wick = float(hari_ini['Upper_Wick'])
    kemarin_body = float(kemarin['Body'])

    # Bullish Engulfing: candle hijau besar yang menelan seluruh body candle merah sebelumnya
    bullish_engulfing = (
        body > 0 and
        kemarin_body < 0 and
        float(hari_ini['Open']) < float(kemarin['Close']) and
        float(hari_ini['Close']) > float(kemarin['Open'])
    )

    # Hammer: lower wick >= 2x body, upper wick kecil, body di ujung atas candle
    hammer = (
        body > 0 and
        lower_wick >= 2 * abs(body) and
        upper_wick <= abs(body) * 0.5 and
        lower_wick / candle_range >= 0.55
    )

    # Marubozu Bullish: body besar (>= 75% dari range), wick sangat kecil — sinyal momentum kuat
    marubozu_bullish = (
        body > 0 and
        abs(body) / candle_range >= 0.75
    )

    # Strong Bullish Bar: close di atas 60% dari range candle (tidak full marubozu)
    strong_close = (float(hari_ini['Close']) - float(hari_ini['Low'])) / candle_range >= 0.60 if candle_range > 0 else False

    if bullish_engulfing:
        return "Bullish Engulfing", True
    elif marubozu_bullish:
        return "Marubozu Bullish", True
    elif hammer:
        return "Hammer", True
    elif strong_close and body > 0:
        return "Strong Bullish Close", True
    else:
        return "Tidak ada pola bullish kuat", False


def analisa_saham_confluence(ticker_code, df_saham, mode_tren):
    """Melakukan screening teknikal multi-factor confluence untuk daily, weekly, dan monthly."""
    try:
        df = siapkan_data_untuk_timeframe(df_saham, mode_tren)

        hari_ini = df.iloc[-1]
        kemarin = df.iloc[-2]

        kondisi_teks = []

        # ── FAKTOR 1: TREND (struktur EMA per timeframe) ─────────────────────
        # EMA200 butuh banyak bar agar konvergen. Pada data harian (120+ bar) EMA200 masih
        # warm-up; pada weekly (52+ bar) EMA200 sangat noise; pada monthly (24+ bar) EMA200
        # tidak bermakna sama sekali. Karena itu:
        #   - Daily  : EMA200 sebagai hard condition (120 bar harian ≈ cukup sebagai long-term filter)
        #   - Weekly : EMA200 hanya konteks informatif, bukan hard condition
        #   - Monthly: EMA200 tidak digunakan, cukup EMA20>EMA50
        n_bars = len(df)

        if mode_tren == "1hari":
            trend_ok = (
                (hari_ini['Close'] > hari_ini['EMA20']) and
                (hari_ini['EMA20'] > hari_ini['EMA50']) and
                (hari_ini['EMA50'] > hari_ini['EMA200'])
            )
            ema_short_ok = hari_ini['EMA9'] > hari_ini['EMA20']
            label_f1 = f"Close>EMA20>EMA50>EMA200 | EMA9>EMA20:{'✔' if ema_short_ok else '✘'}"

        elif mode_tren == "1minggu":
            # Inti: Close>EMA20>EMA50 (weekly bars cukup untuk kedua EMA ini)
            # EMA200 weekly hanya informatif — butuh 200 weekly bar = ~4 tahun, sering tidak ada
            trend_ok = (
                (hari_ini['Close'] > hari_ini['EMA20']) and
                (hari_ini['EMA20'] > hari_ini['EMA50'])
            )
            ema_short_ok = hari_ini['EMA9'] > hari_ini['EMA20']
            # EMA200 valid jika tersedia ≥ 200 weekly bars (~4 tahun), tampilkan sebagai konteks
            ema200_weekly_valid = n_bars >= 200 and (not pd.isna(hari_ini['EMA200']))
            ema200_label = (
                f"EMA50>EMA200:{'✔' if hari_ini['EMA50'] > hari_ini['EMA200'] else '✘'}"
                if ema200_weekly_valid else "EMA200:~(data terbatas)"
            )
            label_f1 = f"Close>EMA20>EMA50 | {ema200_label} | EMA9>EMA20:{'✔' if ema_short_ok else '✘'}"

        else:  # 1bulan
            # Monthly: EMA20>EMA50 sebagai satu-satunya hard condition trend
            # EMA200 monthly perlu 200 bulan = 16+ tahun data, tidak praktis, tidak dipakai
            trend_ok = (
                (hari_ini['Close'] > hari_ini['EMA20']) and
                (hari_ini['EMA20'] > hari_ini['EMA50'])
            )
            label_f1 = "Close>EMA20>EMA50 (monthly)"

        kondisi_teks.append(f"✅ Trend ({label_f1})" if trend_ok else f"❌ Trend ({label_f1})")

        # ── FAKTOR 2: MOMENTUM (RSI + arah) ─────────────────────────────────
        rsi_now = float(hari_ini['RSI'])
        rsi_prev = float(kemarin['RSI']) if not pd.isna(kemarin['RSI']) else rsi_now
        rsi_rising = rsi_now > rsi_prev

        if mode_tren == "1hari":
            # Daily: zona lebih ketat, hindari overbought >75
            momentum_ok = (45 <= rsi_now <= 75) and rsi_rising
            momentum_label = f"RSI:{rsi_now:.1f} {'↑' if rsi_rising else '↓'} (zone 45-75)"
        elif mode_tren == "1minggu":
            # Weekly: zona sedikit lebih lebar, swing trader bisa masuk hingga RSI 70
            momentum_ok = (45 <= rsi_now <= 70) and rsi_rising
            momentum_label = f"RSI:{rsi_now:.1f} {'↑' if rsi_rising else '↓'} (zone 45-70)"
        else:
            # Monthly: zona paling lebar, position trader masuk di momentum awal (40-65)
            momentum_ok = (40 <= rsi_now <= 65) and rsi_rising
            momentum_label = f"RSI:{rsi_now:.1f} {'↑' if rsi_rising else '↓'} (zone 40-65)"

        kondisi_teks.append(
            f"✅ Momentum ({momentum_label})" if momentum_ok else f"❌ Momentum ({momentum_label})"
        )

        # ── FAKTOR 3: MACD (posisi + arah histogram) ─────────────────────────
        hist_now = float(hari_ini['Histogram'])
        hist_prev = float(kemarin['Histogram']) if not pd.isna(kemarin['Histogram']) else hist_now
        hist_rising = hist_now > hist_prev  # Histogram menguat = momentum bertambah

        convergence_ok = (
            (hari_ini['MACD'] > hari_ini['Signal_Line']) and
            hist_now > 0 and
            hist_rising
        )
        macd_label = f"Hist:{hist_now:.4f} {'↑' if hist_rising else '↓'}"
        kondisi_teks.append(
            f"✅ MACD ({macd_label})" if convergence_ok else f"❌ MACD ({macd_label})"
        )

        # ── FAKTOR 4: VOLUME (spike + konfirmasi warna candle) ───────────────
        lonjakan_vol = float(hari_ini['Volume']) / float(hari_ini['Vol_MA20']) if float(hari_ini['Vol_MA20']) > 0 else 0
        candle_bullish = float(hari_ini['Body']) > 0
        # Threshold volume: weekly/monthly lebih santai karena data lebih sedikit dan volatile
        vol_threshold = 1.5 if mode_tren == "1hari" else 1.3
        volume_ok = lonjakan_vol >= vol_threshold and candle_bullish
        kondisi_teks.append(
            f"✅ Volume ({lonjakan_vol:.2f}x MA20, bullish candle)" if volume_ok
            else f"❌ Volume ({lonjakan_vol:.2f}x MA20, {'candle merah' if not candle_bullish else f'< {vol_threshold}x'})"
        )

        # ── FAKTOR 5: STOCHASTIC (daily + weekly) ────────────────────────────
        # Monthly tidak gunakan stochastic karena terlalu sedikit bar untuk meaningful signal
        stoch_ok = False
        if mode_tren in ("1hari", "1minggu"):
            sk = float(hari_ini['Slow_K']) if not pd.isna(hari_ini['Slow_K']) else 50
            sd = float(hari_ini['Slow_D']) if not pd.isna(hari_ini['Slow_D']) else 50
            sk_prev = float(kemarin['Slow_K']) if not pd.isna(kemarin['Slow_K']) else sk
            sk_rising = sk > sk_prev
            # Weekly: zona overbought lebih toleran (≤85) karena candle lebih besar
            stoch_ceiling = 80 if mode_tren == "1hari" else 85
            stoch_ok = sk > sd and sk_rising and sk <= stoch_ceiling
            stoch_label = f"K:{sk:.1f} D:{sd:.1f} {'↑' if sk_rising else '↓'} (ceiling {stoch_ceiling})"
            kondisi_teks.append(
                f"✅ Stochastic ({stoch_label})" if stoch_ok else f"❌ Stochastic ({stoch_label})"
            )

        # ── FAKTOR 6: BREAKOUT (vs prev high) + POLA CANDLE ──────────────────
        # breakout_ok diinisialisasi sesuai timeframe — monthly tidak punya gate breakout
        prev_high_20 = float(hari_ini['Prev_High_20']) if not pd.isna(hari_ini['Prev_High_20']) else None
        breakout_ok = True   # default aman; hanya dipakai sebagai gate untuk daily & weekly
        pola_candle_ok = False

        if mode_tren in ("1hari", "1minggu"):
            label_ph = "PrevHigh20W" if mode_tren == "1minggu" else "PrevHigh20D"
            if prev_high_20 is None:
                # Jika data tidak tersedia, breakout dianggap tidak terkonfirmasi (bukan True)
                breakout_ok = False
                kondisi_teks.append(f"⚠️ Breakout: data {label_ph} tidak tersedia (dianggap gagal)")
            else:
                breakout_ok = float(hari_ini['Close']) > prev_high_20
                kondisi_teks.append(
                    f"✅ Breakout (Close {float(hari_ini['Close']):.0f} > {label_ph} {prev_high_20:.0f})"
                    if breakout_ok
                    else f"❌ Breakout (Close {float(hari_ini['Close']):.0f} ≤ {label_ph} {prev_high_20:.0f})"
                )
        else:
            # Monthly: tidak ada syarat breakout, tapi tampilkan info prev high sebagai konteks
            if prev_high_20 is not None:
                above = float(hari_ini['Close']) > prev_high_20
                kondisi_teks.append(
                    f"ℹ️ Close vs PrevHigh20M: {'di atas' if above else 'di bawah'} ({prev_high_20:.0f})"
                )

        # Pola candle berlaku untuk semua timeframe — weekly/monthly candle lebih berbobot
        nama_pola, pola_candle_ok = deteksi_pola_candle(hari_ini, kemarin)
        kondisi_teks.append(
            f"✅ Candle Pattern: {nama_pola}" if pola_candle_ok
            else f"❌ Candle Pattern: {nama_pola}"
        )

        # ── SKOR & STRONG BUY per timeframe ──────────────────────────────────
        if mode_tren == "1hari":
            # 6 faktor: trend, rsi, macd, volume, stochastic, candle pattern
            total_faktor = 6
            skor = (int(trend_ok) + int(momentum_ok) + int(convergence_ok) +
                    int(volume_ok) + int(stoch_ok) + int(pola_candle_ok))
            # Strong Buy: ≥4/6 faktor + breakout terkonfirmasi (False jika data prev_high kosong)
            strong_buy = skor >= 4 and breakout_ok

        elif mode_tren == "1minggu":
            # 6 faktor: trend, rsi, macd, volume, stochastic, candle pattern
            total_faktor = 6
            skor = (int(trend_ok) + int(momentum_ok) + int(convergence_ok) +
                    int(volume_ok) + int(stoch_ok) + int(pola_candle_ok))
            # Weekly: ≥4/6 faktor + breakout weekly terkonfirmasi
            strong_buy = skor >= 4 and breakout_ok

        else:  # 1bulan
            # 5 faktor: trend, rsi, macd, volume, candle pattern (tanpa stochastic & breakout gate)
            total_faktor = 5
            skor = (int(trend_ok) + int(momentum_ok) + int(convergence_ok) +
                    int(volume_ok) + int(pola_candle_ok))
            # Monthly: ≥4/5, breakout bukan syarat karena position entry sering di mid-trend
            strong_buy = skor >= 4

        # ── LEVEL HARGA (ATR-based, dikalibrasi per timeframe) ───────────────
        atr = float(hari_ini['ATR']) if not pd.isna(hari_ini['ATR']) else float(hari_ini['Close']) * 0.02
        close = float(hari_ini['Close'])

        # Support/resistance window disesuaikan timeframe
        sr_window = {"1hari": 10, "1minggu": 8, "1bulan": 6}[mode_tren]
        support_level = float(df['Low'].rolling(window=sr_window).min().iloc[-1])
        resistance_level = float(df['High'].rolling(window=sr_window).max().iloc[-1])

        entry_level = min(close * 1.005, resistance_level * 0.995)

        # Stop: ATR multiplier lebih besar untuk timeframe lebih panjang
        # Daily: 1.5×ATR, Weekly: 2×ATR, Monthly: 2.5×ATR
        atr_stop_mult = {"1hari": 1.5, "1minggu": 2.0, "1bulan": 2.5}[mode_tren]
        stop_atr = close - (atr_stop_mult * atr)
        stop_level = max(support_level * 0.98, stop_atr)

        # Target: ATR target multiplier juga skala dengan timeframe
        # Daily: 2×ATR, Weekly: 3×ATR, Monthly: 4×ATR
        atr_target_mult = {"1hari": 2.0, "1minggu": 3.0, "1bulan": 4.0}[mode_tren]
        target_price = max(entry_level + (atr_target_mult * atr), resistance_level * 1.02)

        risk_reward = (target_price - entry_level) / (entry_level - stop_level) if (entry_level - stop_level) > 0 else 0
        risk_note = (
            f"ATR({mode_tren}): {atr:.2f} | R:R {risk_reward:.1f}:1 | "
            "Cut loss jika close di bawah stop dengan volume naik."
        )

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
            "lonjakan_volume": lonjakan_vol,
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
        
    print(f"📥 Mengunduh data historis massal untuk {len(pool)} saham...")
    data_massal = yf.download(pool, period="5y", interval="1d", group_by='ticker', progress=False, threads=True)
    
    candidates = []
    for t in pool:
        if t in data_massal and not data_massal[t].empty:
            res = analisa_saham_confluence(t, data_massal[t], args.trend)
            if not res["error"]:
                candidates.append(res)

    strong_buy_candidates = [c for c in candidates if c["status"] == "Strong Buy"]
    # Urutkan berdasarkan: skor normalized → R:R ratio → volume spike
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
        Entry: Rp {s['entry_level']:,.2f}
        Stop: Rp {s['stop_level']:,.2f}
        Target: Rp {s['target_price']:,.2f}
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

    timeframe_desc = {
        "1hari": (
            "Day Trader (hold 1-2 hari). "
            "Fokus pada momentum breakout intraday yang terkonfirmasi volume tinggi dan pola candle bullish. "
            "Entry harus presisi karena eksposur sangat singkat. "
            "Stop loss ketat berbasis ATR harian. "
            "Faktor kunci: breakout vs high 20 hari, candle pattern, stochastic harian."
        ),
        "1minggu": (
            "Swing Trader (hold 1-2 minggu / 5-10 hari bursa). "
            "Fokus pada saham dengan tren mingguan kuat, pullback sehat ke EMA, dan momentum yang baru bangkit. "
            "Entry bisa dilakukan bertahap. Stop loss 2×ATR mingguan memberikan ruang gerak cukup. "
            "Faktor kunci: EMA stack mingguan, breakout vs high 20 minggu, stochastic %K/>%D, konfirmasi volume+candle hijau."
        ),
        "1bulan": (
            "Position Trader / Swing Jangka Menengah (hold 1-3 bulan). "
            "Fokus pada saham dengan tren besar yang baru dimulai atau melanjutkan uptrend primer. "
            "Entry berbasis momentum awal (RSI baru masuk zona bullish), bukan overbought. "
            "Stop loss 2.5×ATR bulanan memberikan ruang untuk volatilitas jangka menengah. "
            "Faktor kunci: EMA20>EMA50 bulanan, MACD histogram mulai naik, candle pattern monthly (sangat berbobot), volume bulanan di atas rata-rata."
        ),
    }

    timeframe_rr_note = {
        "1hari":  "R:R minimum yang acceptable untuk day trade adalah 1.5:1. Di bawah itu skip.",
        "1minggu": "R:R minimum yang acceptable untuk swing trade adalah 2:1. Target minimal 3×ATR dari entry.",
        "1bulan":  "R:R minimum yang acceptable untuk position trade adalah 2.5:1. Target minimal 4×ATR dari entry.",
    }

    system_instruction = (
        f"Anda adalah sistem analis otomatis portofolio saham IDX. "
        f"Perspektif analisis: {timeframe_desc[args.trend]}\n\n"
        "KONTEKS DATA TEKNIKAL:\n"
        "- Setiap saham telah melalui screening multi-factor confluence otomatis (6 faktor untuk daily/weekly, 5 faktor untuk monthly).\n"
        "- Faktor mencakup: struktur EMA, RSI+arah, MACD histogram+arah, volume+konfirmasi candle, stochastic (daily/weekly), pola candlestick, dan breakout.\n"
        "- Skor ditampilkan sebagai X/Y — semakin tinggi semakin banyak faktor terkonfirmasi.\n"
        "- Risk/Reward (R:R) dihitung otomatis berbasis ATR. "
        f"{timeframe_rr_note[args.trend]}\n\n"
        "ATURAN VALIDASI HARGA (WAJIB DIPATUHI):\n"
        "- Support: Area lantai harga terdekat yang valid.\n"
        "- Resistance: Area atap harga terdekat yang menahan.\n"
        "- Entry: Harus di atas support, di bawah atau tepat di resistance (untuk breakout).\n"
        "- Stop: WAJIB di bawah support terdekat atau level ATR yang sudah dihitung. Jangan naikkan stop sembarangan.\n"
        "- Target: WAJIB lebih tinggi dari Close saat ini DAN lebih tinggi dari Resistance terdekat.\n"
        "- Jika R:R dari data < 1.5:1, tandai sebagai 'R:R kurang ideal' dalam summary.\n\n"
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
    max_retries = 5
    retry_delays = [10, 30, 60, 120, 180]  # detik antar retry (backoff bertahap)
    response = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt_data,
                config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.2)
            )
            break  # sukses, keluar dari loop
        except Exception as e:
            err_str = str(e)
            is_retryable = any(code in err_str for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"])
            if is_retryable and attempt < max_retries:
                wait = retry_delays[attempt - 1]
                print(f"⚠️ Gemini API error (attempt {attempt}/{max_retries}): {err_str[:120]}")
                print(f"   Menunggu {wait}s sebelum retry...")
                import time
                time.sleep(wait)
            else:
                print(f"❌ Gemini API gagal setelah {attempt} percobaan: {err_str}")
                raise

    print(response.text)

    summary_file_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file_path:
        with open(summary_file_path, "a", encoding="utf-8") as f:
            f.write("\n" + response.text + "\n")

if __name__ == "__main__":
    main()