import os
import streamlit as str
import yfinance as yf
import pandas as pd
from google import genai
from google.genai import types

# Inisialisasi Gemini Client
api_key = os.environ.get("GEMINI_API_KEY", "ISI_API_KEY_ANDA_DISINI")
client = genai.Client(api_key=api_key)

def ambil_metrik_fundamental(ticker_code):
    try:
        ticker = yf.Ticker(ticker_code)
        info = ticker.info
        return {
            "ticker": ticker_code,
            "nama": info.get("longName", ticker_code),
            "pe_ratio": info.get("trailingPE", "N/A"),
            "pbv_ratio": info.get("priceToBook", "N/A"),
            "roe": info.get("returnOnEquity", "N/A"),
            "harga_terakhir": info.get("currentPrice", info.get("previousClose", "N/A")),
            "error": False
        }
    except Exception:
        return {"ticker": ticker_code, "error": True}

def dapatkan_top_3_fundamental_ihsg():
    pool_saham = ["BBCA.JK", "BBRI.JK", "BMRI.JK", "BBNI.JK", "TLKM.JK", "ASII.JK", "UNVR.JK", "ICBP.JK", "AMRT.JK", "KLBF.JK"]
    list_fundamental = []
    for t in pool_saham:
        data = ambil_metrik_fundamental(t)
        if not data["error"] and isinstance(data["pe_ratio"], (int, float)) and isinstance(data["roe"], (int, float)):
            if data["pe_ratio"] > 0 and data["roe"] > 0:
                list_fundamental.append(data)
    # Urutkan berdasarkan ROE tertinggi (Profitabilitas terbaik)
    list_fundamental = sorted(list_fundamental, key=lambda x: x["roe"], reverse=True)
    return list_fundamental[:3]

# --- UI STREAMLIT ---
str.set_page_config(page_title="AI Fundamental Stock Advisor", page_icon="🏦")
str.title("🏦 AI Fundamental Stock Advisor (Local App)")

mode_manual = str.checkbox("Saya ingin input kode saham secara manual (Maksimal 3 Saham)", value=False)
data_saham = []
mode_analisis = ""

if mode_manual:
    mode_analisis = "MANUAL"
    col1, col2, col3 = str.columns(3)
    input1 = col1.text_input("Saham 1:", value="BBRI.JK").strip().upper()
    input2 = col2.text_input("Saham 2:", value="TLKM.JK").strip().upper()
    input3 = col3.text_input("Saham 3:", value="ASII.JK").strip().upper()
    
    if str.button("Mulai Analisis Manual"):
        with str.spinner("Mengunduh data..."):
            for ticker in filter(None, list(set([input1, input2, input3]))):
                res = ambil_metrik_fundamental(ticker)
                if not res["error"]: data_saham.append(res)
                else: str.error(f"Gagal memuat {ticker}")
else:
    mode_analisis = "OTOMATIS"
    if str.button("Scan & Analisa Otomatis"):
        with str.spinner("Memindai fundamental bursa..."):
            data_saham = dapatkan_top_3_fundamental_ihsg()

if data_saham:
    str.divider()
    df_tampil = pd.DataFrame(data_saham)[["ticker", "nama", "harga_terakhir", "pe_ratio", "pbv_ratio", "roe"]]
    df_tampil["roe"] = df_tampil["roe"].apply(lambda x: f"{x*100:.2f}%" if isinstance(x, (int, float)) else x)
    str.table(df_tampil)
    
    prompt_data = "".join([f"\nSaham {i+1}: {s['ticker']} ({s['nama']})\nHarga: {s['harga_terakhir']}\nP/E: {s['pe_ratio']}\nPBV: {s['pbv_ratio']}\nROE: {s['roe']}\n" for i, s in enumerate(data_saham)])
    
    system_instruction = (
        "Anda adalah Analis Ekuitas Senior. Berikan rekomendasi akhir mutlak BUY, HOLD, atau SELL untuk setiap saham berdasarkan data fundamentalnya." 
        if mode_analisis == "MANUAL" else 
        "Anda adalah Financial Advisor. 3 saham ini adalah hasil filter otomatis terbaik (Sinyal BUY). Ulas mendalam tesis investasi dan potensinya."
    )

    with str.spinner("🤖 Menyusun analisis ekuitas bersama Gemini..."):
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Mode: {mode_analisis}\nData:\n{prompt_data}",
            config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.7)
        )
        str.markdown(response.text)