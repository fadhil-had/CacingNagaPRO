import os
import sys
import yfinance as yf
from google import genai
from google.genai import types

def ambil_metrik_fundamental_tren_dan_berita(ticker_code):
    try:
        ticker = yf.Ticker(ticker_code)
        info = ticker.info
        
        # 1. Ambil data historis 3 bulan terakhir
        df_3mo = ticker.history(period="3mo")
        if df_3mo.empty or len(df_3mo) < 2:
            return {"ticker": ticker_code, "error": True}
            
        harga_terakhir = df_3mo['Close'].iloc[-1]
        harga_3_bulan_lalu = df_3mo['Close'].iloc[0]
        
        performa_3_bulan = ((harga_terakhir - harga_3_bulan_lalu) / harga_3_bulan_lalu) * 100
        harga_tertinggi_3mo = df_3mo['High'].max()
        harga_terendah_3mo = df_3mo['Low'].min()
        
        # 2. Ambil sentimen berita terbaru (3 berita teratas)
        berita_terbaru = ticker.news
        teks_berita = ""
        if berita_terbaru:
            for item in berita_terbaru[:3]:
                teks_berita += f"- {item.get('title')} (Source: {item.get('publisher')})\n"
        else:
            teks_berita = "- Tidak ada berita terbaru yang signifikan harian.\n"
        
        return {
            "ticker": ticker_code,
            "nama": info.get("longName", ticker_code),
            "pe_ratio": info.get("trailingPE", "N/A"),
            "pbv_ratio": info.get("priceToBook", "N/A"),
            "roe": info.get("returnOnEquity", "N/A"),
            "harga_terakhir": harga_terakhir,
            "performa_3_bulan": performa_3_bulan,
            "tertinggi_3mo": harga_tertinggi_3mo,
            "terendah_3mo": harga_terendah_3mo,
            "berita": teks_berita,
            "error": False
        }
    except Exception:
        return {"ticker": ticker_code, "error": True}

def dapatkan_top_3_fundamental_ihsg():
    pool = ["BBCA.JK", "BBRI.JK", "BMRI.JK", "BBNI.JK", "TLKM.JK", "ASII.JK", "UNVR.JK", "ICBP.JK", "AMRT.JK", "KLBF.JK"]
    list_fundamental = []
    for t in pool:
        data = ambil_metrik_fundamental_tren_dan_berita(t)
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
            res = ambil_metrik_fundamental_tren_dan_berita(ticker.strip().upper())
            if not res["error"]: data_saham.append(res)
    else:
        mode_analisis = "OTOMATIS"
        print("🤖 [MODE OTOMATIS] Mencari 3 Saham Fundamental Kuat & Menganalisis Sentimen...")
        data_saham = dapatkan_top_3_fundamental_ihsg()

    if not data_saham:
        print("❌ Tidak ada data saham yang berhasil diproses.")
        return

    # Menyusun prompt data mentah komprehensif termasuk berita
    prompt_data = ""
    for s in data_saham:
        prompt_data += f"""
        Saham: {s['ticker']} ({s['nama']})
        - Harga Terakhir: Rp {s['harga_terakhir']:,.2f}
        - P/E Ratio: {s['pe_ratio']} | PBV Ratio: {s['pbv_ratio']} | ROE: {s['roe']}
        - Performa 3 Bulan Terakhir: {s['performa_3_bulan']:+.2f}%
        - Range Harga 3 Bulan: Rp {s['terendah_3mo']:,.2f} - Rp {s['tertinggi_3mo']:,.2f}
        - Berita Utama Terkini:
        {s['berita']}
        """

    # --- KONFIGURASI EXECUTIVE SUMMARY 3 IN 1 (FUNDAMENTAL, TREN, SENTIMEN) ---
    if mode_analisis == "MANUAL":
        system_instruction = (
            "Anda adalah seorang Analis Saham senior senior. Tugas Anda membuat 'EXECUTIVE SUMMARY' multi-analisis. "
            "Evaluasi data fundamental, teknikal, tren harga 3 bulan, dan judul berita terkini untuk menentukan sentimen pasar.\n"
            "Format output Anda WAJIB mengikuti struktur ringkas ini tanpa basa-basi pengantar:\n\n"
            "1. **[KODE SAHAM] - KEPUTUSAN: [BUY/HOLD/SELL]**\n"
            "   * **Alasan Utama:** (Maksimal 2 kalimat analisis kombinasi kesehatan fundamental dan posisi teknikal harga).\n"
            "   * **Sentimen Berita Terkini:** (Ulas singkat apakah headlines berita mengarah ke sentimen Positif/Negatif/Netral beserta dampaknya).\n"
            "   * **Risiko Kunci:** (1 risiko operasional atau eksternal paling krusial saat ini).\n\n"
            "Langsung berikan kesimpulan, akhiri dengan 1 baris kalimat disclaimer singkat."
        )
    else:
        system_instruction = (
            "Anda adalah seorang Financial Advisor profesional. Data berikut adalah 3 saham otomatis terbaik hasil screening nilai fundamental (Sinyal BUY). "
            "Tugas Anda mengintegrasikan fundamental, teknikal, ulasan berita dan tren harga ke dalam 'EXECUTIVE SUMMARY TOP 3 BUY PICKS' berikut:\n"
            "Format output Anda WAJIB mengikuti struktur ringkas ini tanpa kalimat pembuka:\n\n"
            "1. **[KODE SAHAM] - REKOMENDASI: BUY**\n"
            "   * **Tesis Investasi:** (Maksimal 2 kalimat mengapa layak akumulasi berbasis ROE dan momentum harga 3 bulan).\n"
            "   * **Analisis Kabar & Sentimen Pasar:** (Ringkas muatan berita terkini yang memperkuat alasan beli emiten ini).\n"
            "   * **Faktor Risiko:** (1 risiko psikologis pasar atau makroekonomi yang perlu diperhatikan).\n\n"
            "Langsung berikan kesimpulan, akhiri dengan 1 baris kalimat disclaimer singkat."
        )

    print("🤖 Mengirimkan data komprehensif ke Gemini AI...")
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=f"Mode: {mode_analisis}\nData:\n{prompt_data}",
        config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.4)
    )
    
    print("\n=== 📊 HASIL ANALISIS EKUATAS GEMINI ===")
    print(response.text)

    summary_file_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file_path:
        with open(summary_file_path, "a", encoding="utf-8") as f:
            f.write("\n### 📊 HASIL ANALISIS EKUATAS GEMINI (EXECUTIVE SUMMARY MULTI-DIMENSI)\n")
            f.write(response.text)
            f.write("\n\n---\n")

if __name__ == "__main__":
    main()