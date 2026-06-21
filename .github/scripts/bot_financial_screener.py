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
        print("❌ Error: GEMINI_API_KEY tidak ditemukan di Repository Secrets.")
        return
    client = genai.Client(api_key=api_key)

    args = sys.argv[1:]
    data_saham = []
    
    if args and args[0].strip() != "":
        mode_analisis = "MANUAL"
        print(f"✍️ [MODE MANUAL] Memproses kode input: {args}")
        for ticker in args[:3]:
            res = ambil_metrik_fundamental(ticker.strip().upper())
            if not res["error"]: data_saham.append(res)
    else:
        mode_analisis = "OTOMATIS"
        print("🤖 [MODE OTOMATIS] Mencari 3 Saham Berbasis Nilai Fundamental ROE Tertinggi...")
        data_saham = dapatkan_top_3_fundamental_ihsg()

    if not data_saham:
        print("❌ Tidak ada data saham yang berhasil diproses.")
        return

    prompt_data = ""
    for s in data_saham:
        prompt_data += f"\nSaham: {s['ticker']} ({s['nama']})\nHarga: {s['harga_terakhir']}\nP/E: {s['pe_ratio']}\nPBV: {s['pbv_ratio']}\nROE: {s['roe']}\n"

    # --- KONFIGURASI EXECUTIVE SUMMARY (TO THE POINT) ---
    if mode_analisis == "MANUAL":
        system_instruction = (
            "Anda adalah seorang Analis Saham senior yang sangat efisien. "
            "Tugas Anda adalah membuat 'EXECUTIVE SUMMARY' dari data fundamental saham pilihan user. "
            "Format output Anda WAJIB mengikuti struktur ringkas ini tanpa basa-basi pengantar:\n\n"
            "1. **[KODE SAHAM] - KEPUTUSAN: [BUY/HOLD/SELL]**\n"
            "   * **Alasan Utama:** (Maksimal 2 kalimat analisis valuasi P/E atau profitabilitas ROE).\n"
            "   * **Risiko Kunci:** (1 risiko harian utama yang wajib diwaspadai).\n\n"
            "Langsung berikan kesimpulan, akhiri dengan 1 baris kalimat disclaimer singkat."
        )
    else:
        system_instruction = (
            "Anda adalah seorang Financial Advisor senior yang sangat efisien. "
            "Data yang diberikan adalah 3 saham otomatis terbaik hasil filter internal dengan sinyal kuat untuk BUY. "
            "Tugas Anda adalah membuat 'EXECUTIVE SUMMARY TOP 3 BUY PICKS' menggunakan struktur ringkas ini tanpa kalimat pembuka:\n\n"
            "1. **[KODE SAHAM] - REKOMENDASI: BUY**\n"
            "   * **Tesis Investasi:** (Maksimal 2 kalimat mengapa layak dibeli berdasarkan kekuatan ROE harian).\n"
            "   * **Faktor Risiko:** (1 risiko makro/mikro krusial emiten ini).\n\n"
            "Langsung berikan kesimpulan, akhiri dengan 1 baris kalimat disclaimer singkat."
        )

    print("🤖 Mengirimkan data dan menyusun ringkasan eksekutif via Gemini AI...")
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=f"Mode: {mode_analisis}\nData:\n{prompt_data}",
        config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.5)
    )
    
    print("\n=== 📊 HASIL ANALISIS EKUATAS GEMINI ===")
    print(response.text)

if __name__ == "__main__":
    main()