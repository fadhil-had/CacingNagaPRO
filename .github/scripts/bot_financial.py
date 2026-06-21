import os
import sys
import yfinance as yf
from google import genai
from google.genai import types

def hitung_rekomendasi_dasar(harga_sekarang, harga_kemarin):
    if harga_sekarang > harga_kemarin:
        return "BUY", "🟢 Harga naik dibandingkan hari sebelumnya."
    elif harga_sekarang < harga_kemarin:
        return "SELL", "🔴 Harga turun dibandingkan hari sebelumnya."
    else:
        return "HOLD", "🟡 Harga stabil/tidak berubah dari hari sebelumnya."

def analisis_saham(ticker_input):
    ticker_input = ticker_input.strip().upper()
    print(f" Mengambil data untuk {ticker_input}...")
    
    # Ambil data 5 hari terakhir
    ticker_data = yf.Ticker(ticker_input)
    df = ticker_data.history(period="5d")
    
    if df.empty:
        print(f"⚠️ Kesalahan: Kode '{ticker_input}' tidak ditemukan atau tidak memiliki data.")
        return False

    harga_sekarang = df['Close'].iloc[-1]
    harga_kemarin = df['Close'].iloc[-2]
    perubahan_harga = harga_sekarang - harga_kemarin
    persentase_ubah = (perubahan_harga / harga_kemarin) * 100
    
    info = ticker_data.info
    nama_perusahaan = info.get('longName', ticker_input)

    sinyal, alasan_sinyal = hitung_rekomendasi_dasar(harga_sekarang, harga_kemarin)

    print(f"\n📊 [DATA MENTAH] {nama_perusahaan} ({ticker_input})")
    print(f"Harga: {harga_sekarang:,.2f} | Perubahan: {perubahan_harga:+,.2f} ({persentase_ubah:+.2f}%)")
    print(f"Sinyal Sistem: {sinyal} ({alasan_sinyal})\n")

    # Inisialisasi Gemini Client
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("❌ Error: GEMINI_API_KEY tidak ditemukan di Environment Variables.")
        return False
        
    client = genai.Client(api_key=api_key)

    prompt_narasi = f"""
    Berikan analisis untuk saham/indeks berikut:
    - Nama Perusahaan/Indeks: {nama_perusahaan} ({ticker_input})
    - Harga Penutupan Terakhir: {harga_sekarang:,.2f}
    - Harga Hari Sebelumnya: {harga_kemarin:,.2f}
    - Perubahan: {perubahan_harga:+,.2f} ({persentase_ubah:+.2f}%)
    - Sinyal Sistem Internal Dasar: {sinyal} ({alasan_sinyal})
    """

    print("🤖 Menghubungi Gemini AI Financial Advisor...")
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt_narasi,
        config=types.GenerateContentConfig(
            system_instruction=(
                "Anda adalah seorang Financial Advisor profesional yang ramah dan edukatif. "
                "Jelaskan angka pergerakan saham ini ke bahasa yang mudah dipahami investor pemula. "
                "Sebutkan tren (bullish/bearish), risiko, dan edukasi sinyal dasar ini. Sertakan disclaimer."
            ),
            temperature=0.7,
        ),
    )
    
    print("\n=== HASIL ANALISIS AI ===")
    print(response.text)
    return True

if __name__ == "__main__":
    # Jika dijalankan langsung via terminal (e.g., python bot_financial.py BBRI.JK)
    if len(sys.argv) > 1:
        ticker = sys.argv[1]
    else:
        # Jika tidak ada argumen, minta input di terminal
        ticker = input("Masukkan Kode Saham / Indeks: ")
    
    analisis_saham(ticker)