import os
import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
from google import genai
from google.genai import types

# Mengambil API Key dari Environment Variable
api_key = os.environ.get("GEMINI_API_KEY", "ISI_API_KEY_ANDA_DISINI")
client = genai.Client(api_key=api_key)

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
            st.error(f"❌ File '{file_path}' tidak ditemukan! Pastikan folder 'resource' dan file Excel sudah benar.")
            return []
            
        df_excel = pd.read_excel(file_path)
        if 'Kode' not in df_excel.columns:
            st.error("❌ Kolom 'Kode' tidak ditemukan di file Excel! Pastikan nama kolom sesuai.")
            return []
            
        tickers = df_excel['Kode'].dropna().astype(str).str.strip().str.upper()
        return (tickers + ".JK").tolist()
    except Exception as e:
        st.error(f"❌ Gagal membaca Excel: {str(e)}")
        return []

def analisa_saham_confluence(ticker_code, df_saham, mode_tren):
    """Melakukan screening teknikal cepat menggunakan data dari unduhan massal"""
    try:
        df = df_saham.dropna(subset=['Close'])
        if df.empty or len(df) < 200:
            return {"error": True, "alasan": "Data tidak cukup untuk indikator"}
            
        # --- 1. HITUNG INDIKATOR UTAMA ---
        df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
        df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()
        df['EMA10'] = df['Close'].ewm(span=10, adjust=False).mean()
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['RSI'] = hitung_rsi(df['Close'], period=14)

        # MACD
        ema12 = df['Close'].ewm(span=12, adjust=False).mean()
        ema26 = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = ema12 - ema26
        df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['Histogram'] = df['MACD'] - df['Signal_Line']

        # Volume
        df['Vol_MA20'] = df['Volume'].rolling(window=20).mean()

        hari_ini = df.iloc[-1]
        kemarin = df.iloc[-2]
        
        # --- 2. EVALUASI FAKTOR CONFLUENCE ---
        skor = 0
        kondisi_teks = []

        # Faktor 1: Trend
        if mode_tren == "1hari":
            f1_cond = (hari_ini['Close'] > hari_ini['EMA10'])
            label_f1 = "Close > EMA10"
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

        # Faktor 2: Momentum (RSI)
        if 40 <= hari_ini['RSI'] <= 65:
            skor += 1
            kondisi_teks.append(f"✅ Momentum (40 <= RSI:{hari_ini['RSI']:.1f} <= 65)")
        else:
            kondisi_teks.append(f"❌ Momentum (RSI:{hari_ini['RSI']:.1f} di luar 40-65)")

        # Faktor 3: Convergence (MACD)
        if hari_ini['MACD'] > hari_ini['Signal_Line'] and hari_ini['Histogram'] > 0:
            skor += 1
            kondisi_teks.append("✅ Convergence (MACD > Signal)")
        else:
            kondisi_teks.append("❌ Convergence")

        # Faktor 4: Volume
        lonjakan_vol = hari_ini['Volume'] / hari_ini['Vol_MA20'] if hari_ini['Vol_MA20'] > 0 else 0
        if lonjakan_vol > 1.5:
            skor += 1
            kondisi_teks.append(f"✅ Volume (Lonjakan: {lonjakan_vol:.2f}x)")
        else:
            kondisi_teks.append(f"❌ Volume (Lonjakan: {lonjakan_vol:.2f}x)")

        return {
            "ticker": ticker_code,
            "harga_terakhir": hari_ini['Close'],
            "skor": skor,
            "status": "Strong Buy" if skor == 4 else "Watchlist",
            "kondisi_detail": ", ".join(kondisi_teks),
            "lonjakan_volume": lonjakan_vol,
            "error": False
        }
    except Exception as e:
        return {"error": True, "alasan": str(e)}

# --- UI STREAMLIT ---
st.set_page_config(page_title="CacingNagaPRO - Stock Screener", page_icon="📈", layout="wide")
st.title("📈 CacingNagaPRO Multi-Factor Stock Screener")
st.caption("Aplikasi screening saham IHSG berbasis Multi-Factor Confluence (Trend, RSI, MACD, Volume) terintegrasi Gemini AI")

# Pilihan Timeframe / Perspektif Trading
mode_tren = st.radio(
    "Pilih Perspektif Jangka Waktu Trading:",
    options=["1hari", "1minggu", "1bulan"],
    format_func=lambda x: "Harian (Day Trading / Momentum Cepat)" if x == "1hari" else (
        "Mingguan (Swing Trading Jangka Pendek)" if x == "1minggu" else "Bulanan (Position Trading / Swing Panjang)"
    ),
    horizontal=True
)

mode_manual = st.checkbox("Saya ingin input kode saham secara manual (Maksimal 3 Saham)", value=False)
data_saham = []

if mode_manual:
    col1, col2, col3 = st.columns(3)
    input1 = col1.text_input("Saham 1:", value="BBRI.JK").strip().upper()
    input2 = col2.text_input("Saham 2:", value="TLKM.JK").strip().upper()
    input3 = col3.text_input("Saham 3:", value="ASII.JK").strip().upper()
    
    if st.button("🚀 Mulai Analisis Manual"):
        tickers_input = filter(None, list(set([input1, input2, input3])))
        with st.spinner("Mengunduh data pasar..."):
            # Download massal lokal untuk saham manual
            data_massal = yf.download(list(tickers_input), period="1y", interval="1d", group_by='ticker', progress=False)
            
            for t in tickers_input:
                if t in data_massal and not data_massal[t].empty:
                    res = analisa_saham_confluence(t, data_massal[t], mode_tren)
                    if not res["error"]:
                        data_saham.append(res)
            
            # Ambil semua data tanpa batasan top 3 jika manual
            data_saham = sorted(data_saham, key=lambda x: (x["skor"], x["lonjakan_volume"]), reverse=True)
else:
    if st.button("🔍 Jalankan Screening Massal (900+ Saham Excel)"):
        pool = ambil_semua_ticker_dari_excel("resource/daftar-saham.xlsx")
        
        if pool:
            progress_bar = st.progress(0)
            st.info(f"📥 Mengunduh dan memproses data historis untuk {len(pool)} saham dari Excel. Mohon tunggu...")
            
            # Download massal multi-threading super cepat
            data_massal = yf.download(pool, period="1y", interval="1d", group_by='ticker', progress=False, threads=True)
            progress_bar.progress(50)
            
            saham_lolos = []
            for t in pool:
                if t in data_massal and not data_massal[t].empty:
                    res = analisa_saham_confluence(t, data_massal[t], mode_tren)
                    # Filter awal: Hanya ambil yang masuk Watchlist (3/4) atau Strong Buy (4/4)
                    if not res["error"] and res["skor"] >= 3:
                        saham_lolos.append(res)
            
            # Ambil hanya TOP 3 dengan kombinasi skor tertinggi dan lonjakan volume terbesar
            data_saham = sorted(saham_lolos, key=lambda x: (x["skor"], x["lonjakan_volume"]), reverse=True)[:3]
            progress_bar.progress(100)
            progress_bar.empty()

# --- VALIDASI DAN OUTPUT HASIL ---
if data_saham:
    st.success(f"✅ Berhasil menyeleksi {len(data_saham)} saham terbaik!")
    
    # Menampilkan ringkasan tabel data teknikal mentah di Streamlit UI
    df_tampil = pd.DataFrame(data_saham)[["ticker", "status", "harga_terakhir", "skor", "lonjakan_volume"]]
    df_tampil["harga_terakhir"] = df_tampil["harga_terakhir"].apply(lambda x: f"Rp {x:,.2f}")
    df_tampil["lonjakan_volume"] = df_tampil["lonjakan_volume"].apply(lambda x: f"{x:.2f}x")
    st.subheader("📋 Hasil Filter Matriks Konfluensi")
    st.table(df_tampil)
    
    # 3. LAZY-LOADING BERITA: Ambil berita hanya untuk saham yang masuk Top Picks
    with st.spinner("📰 Mengambil data berita katalis pasar harian..."):
        prompt_data = f"PERSPEKTIF TREN TRADING: {mode_tren.upper()}\n\n"
        for s in data_saham:
            ticker_obj = yf.Ticker(s['ticker'])
            berita_terbaru = ticker_obj.news
            teks_berita = ""
            if berita_terbaru:
                for item in berita_terbaru[:2]:
                    teks_berita += f"- {item.get('title')} ({item.get('publisher')})\n"
            else:
                teks_berita = "- Tidak ada berita terbaru harian.\n"

            prompt_data += f"""
            Ticker: {s['ticker']} | Status Teknikal: {s['status']} (Score {s['skor']}/4)
            Harga Terakhir: Rp {s['harga_terakhir']:,.2f}
            Kondisi Faktor: {s['kondisi_detail']}
            Berita Terkini:
            {teks_berita}
            ---
            """

    # 4. KONEKSI KE GEMINI AI UNTUK EXECUTIVE REPORT
    timeframe_desc = {
        "1hari": "Day Trader kilat (target keluar-masuk 1-2 hari).",
        "1minggu": "Swing Trader jangka pendek (target hold 1 minggu/5 hari bursa).",
        "1bulan": "Position Trader / Swing Jangka Menengah (target hold 2-4 minggu)."
    }

    system_instruction = (
        f"Anda adalah sistem analis otomatis portofolio saham. Sesuaikan gaya analisis Anda untuk perspektif {timeframe_desc[mode_tren]}\n"
        "Tugas Anda memvalidasi data teknikal serta ulasan berita yang dikirimkan untuk menghasilkan keputusan pasar final.\n\n"
        "Format output Anda WAJIB langsung menghasilkan tabel rekapitulasi seperti format markdown berikut tanpa basa-basi kata pengantar:\n\n"
        "### 📊 IDX Stock Report (Top Picks)\n"
        "| Ticker | Status | Close | Support | Resist | Target |\n"
        "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
        "| [KODE] | [Strong Buy / Watchlist] | [Harga] | [Angka] | [Angka] | [Angka] |\n\n"
        "**Analisis Taktis Per Ticker:**\n"
        "- **[KODE SAHAM]**: (Berikan 2 kalimat ringkas gabungan alasan teknikal/volume dan dampak sentimen berita terhadap target harga tersebut).\n\n"
        "Akhiri dengan 1 baris kalimat disclaimer trading pendek."
    )

    with st.spinner("🤖 [Gemini AI] Sedang memvalidasi tren & menyusun rekomendasi harga..."):
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt_data,
            config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.2)
        )
        
        st.divider()
        st.subheader("📊 Executive Summary AI (Support, Resist & Target)")
        st.markdown(response.text)
        
elif mode_manual or not mode_manual:
    if 'data_saham' in locals() and not data_saham and st.status:
        st.info("💡 Klik tombol di atas untuk memulai pemindaian saham bursa harian.")