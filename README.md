# 📈 CacingNagaPRO - Multi-Factor IDX Stock Screener

Sistem automasi pemindaian saham Bursa Efek Indonesia (IHSG) berbasis strategi **Multi-Factor Confluence** yang terintegrasi secara *native* dengan **Gemini AI** dan berjalan otomatis menggunakan **GitHub Actions**.

## 📂 Struktur Proyek

```text
CacingNagaPRO/
├── .github/
│   └── workflows/
│       └── financial_screener.yml  # Pengatur jadwal otomatis harian GitHub
├── resource/
│   └── daftar-saham.xlsx          # Database list kode saham IHSG (900+)
├── scripts/
│   ├── financial_screener.py      # Skrip utama backend (GitHub Actions)
│   └── financial_screener_app.py  # Skrip antarmuka web visual (Streamlit lokal)
├── README.md                      # Dokumentasi proyek
└── requirements.txt               # Daftar pustaka / dependensi Python