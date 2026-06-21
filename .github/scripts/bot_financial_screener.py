import os
import sys
import yfinance as yf
from google import genai
from google.genai import types

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
    pool = ["BBCA.JK", "BBRI.JK", "BMRI.JK", "BBNI.JK", "TLKM.JK", "ASII.JK", "UNVR.JK", "ICBP.JK", "AMRT.JK", "KLBF.JK"]
    list_fundamental = []
    for t in pool:
        data = ambil_metrik_fundamental(t)
        if not data["error"] and isinstance(data["pe_ratio"], (int, float)) and isinstance(data["roe"], (int, float)):
            if data["pe_ratio"] > 0 and data["roe"] > 0:
                list_fundamental.append(data)
    return sorted(list_fundamental, key=lambda x: x["roe"], reverse=True)[:3]

def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("❌ Error: GEMINI_API_KEY tidak ditemukan.")
        return
    client = genai.Client(api_key=api_key)

    # Deteksi argumen dari GitHub Actions (Jika ada argumen = Mode Manual)
    args = sys.argv[1:]
    data_saham = []
    
    if args and args[0].strip() != "":
        mode_analisis = "MANUAL"
        print(f"✍️ Mode Manual diaktifkan mendeteksi saham: {args}")
        for ticker in args[:3]: # Maksimal 3 saham
            res = ambil_metrik_fundamental(ticker.strip().upper())
            if not res["error"]: data_saham.append(res)
    else:
        mode_analisis = "OTOMATIS"
        print("🤖 Mode Otomatis aktif. Mencari 3 Saham Fundamental Terbaik...")
        data_saham = dapatkan_top_3_fundamental_ihsg()

    if not data_saham:
        print("❌ Tidak ada data saham yang berhasil diproses.")
        return

    # Tampilkan log data mentah di terminal GitHub
    print("\n--- DATA MENTAH SAHAM ---")
    prompt_data = ""
    for s in data_saham:
        print(f"[{s['ticker']}] {s['nama']} | Harga: {s['harga_terakhir']} | P/E: {s['pe_ratio']} | ROE: {s['roe']}")
        prompt_data += f"\nSaham: {s['ticker']} ({s['nama']})\nHarga: {s['harga_terakhir']}\nP/E: {s['pe_ratio']}\nPBV: {s['pbv_ratio']}\nROE: {s['roe']}\n"

    system_instruction = (
        "Anda adalah Analis Ekuitas Senior. Berikan rekomendasi akhir mutlak BUY, HOLD, atau SELL untuk setiap saham berdasarkan data fundamentalnya." 
        if mode_analisis == "MANUAL" else 
        "Anda adalah Financial Advisor. 3 saham ini adalah hasil filter otomatis terbaik (Sinyal BUY). Ulas mendalam tesis investasi dan potensinya."
    )

    print("\n🤖 Mengirimkan data ke Gemini AI...")
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=f"Mode: {mode_analisis}\nData:\n{prompt_data}",
        config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.7)
    )
    
    print("\n=== 📊 HASIL ANALISIS EKUATAS GEMINI ===")
    print(response.text)

if __name__ == "__main__":
    main()