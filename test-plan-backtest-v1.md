TEST PLAN BACKTEST SCREENER SAHAM V1
===================================

1. TUJUAN
---------

Memastikan bahwa:

- Perhitungan sinyal tidak menggunakan data masa depan.
- Entry, stop, target, dan max hold disimulasikan secara realistis.
- Ranking tiga rekomendasi bekerja sesuai penggunaan screener.
- Strategi tetap menghasilkan performa positif setelah fee dan slippage.
- Hasil stabil pada periode dan kondisi pasar berbeda.


2. RUANG LINGKUP
----------------

Backtest dilakukan terpisah untuk setiap profil:

- Daily Swing
  Candle   : Daily
  Max hold : 5, 10, dan 15 trading days

- Weekly Position
  Candle   : Weekly
  Max hold : 4, 8, dan 12 minggu

- Monthly Long-term
  Candle   : Monthly
  Max hold : 3, 6, dan 9 bulan

Mode pengujian:

1. Signal-level
   Menguji semua sinyal dengan status Ready to Enter secara independen.

2. Portfolio-level
   Menguji penggunaan sebenarnya: maksimal tiga rekomendasi/posisi aktif.


3. PEMBAGIAN DATA
-----------------

- Development : Januari 2016 - Desember 2021
  Fungsi      : Debug dan validasi logika.

- Validation  : Januari 2022 - Desember 2023
  Fungsi      : Memilih max hold dan parameter final.

- Holdout     : Januari 2024 - Agustus 2026
  Fungsi      : Pengujian final tanpa tuning.

Holdout hanya dijalankan setelah seluruh parameter dikunci. Parameter tidak
boleh diubah berdasarkan hasil holdout. Jika hasil holdout gagal, hasil tersebut
dicatat sebagai kekurangan V1 dan menjadi input pengembangan V2.


4. KONFIGURASI BASELINE
-----------------------

Sebelum backtest dimulai, bekukan konfigurasi berikut:

- Bobot setiap faktor.
- Threshold quality score.
- RSI minimum dan maksimum.
- ATR minimum dan maksimum.
- Volume ratio.
- Aturan breakout dan pullback.
- Stop ATR dan target risk-reward.
- Minimum harga dan turnover.
- Aturan market regime.
- Position sizing.
- Fee beli, fee jual, dan slippage.

Pada eksperimen pertama, hanya max_holding_bars yang dibandingkan agar hasilnya
tidak tercampur dengan perubahan parameter lain.


5. ATURAN TRANSAKSI
-------------------

5.1 Entry

- Sinyal hanya dihitung menggunakan candle yang sudah selesai.
- Entry dilakukan pada harga Open candle berikutnya.
- Daily: sinyal setelah market tutup, entry pada hari bursa berikutnya.
- Weekly: sinyal setelah minggu selesai, entry pada minggu berikutnya.
- Monthly: sinyal setelah bulan selesai, entry pada sesi berikutnya.
- Hanya status Ready to Enter yang boleh membuka posisi.
- Wait for Trigger hanya dicatat dan tidak dihitung sebagai transaksi.
- Ticker yang sedang dimiliki tidak boleh dibeli kembali.

5.2 Exit

Posisi ditutup berdasarkan kondisi yang terjadi lebih dahulu:

1. Stop-loss tersentuh.
2. Target tersentuh.
3. Setup atau tren terinvalidasi.
4. Max hold tercapai.

Ketentuan tambahan:

- Jika stop dan target tersentuh pada candle yang sama, gunakan asumsi
  konservatif: stop dianggap tersentuh lebih dahulu.
- Jika harga Open gap melewati stop, exit menggunakan harga Open aktual.
- Invalidation yang terkonfirmasi pada Close dieksekusi pada Open berikutnya.
- Max hold dihitung mulai dari candle entry dan harus bebas dari kesalahan
  off-by-one.
- Seluruh return dihitung setelah fee dan slippage.
- Entry, stop, target, dan fill mengikuti fraksi harga BEI.


6. TEST CASE VALIDASI ENGINE
----------------------------

BT-01 - Candle belum selesai
Skenario : Candle hari ini/minggu ini/bulan ini belum selesai.
Expected : Candle tidak digunakan untuk menghasilkan sinyal.

BT-02 - Resample weekly
Skenario : Data daily diubah menjadi weekly.
Expected : Open, High, Low, Close, dan Volume sesuai agregasi minggu yang
           selesai.

BT-03 - Resample monthly
Skenario : Data daily diubah menjadi monthly.
Expected : Hanya candle bulan yang sudah selesai yang digunakan.

BT-04 - No look-ahead indikator
Skenario : Indikator dihitung pada tanggal T.
Expected : Perhitungan hanya menggunakan data sampai tanggal T.

BT-05 - No look-ahead RS dan breadth
Skenario : RS percentile dan market breadth dihitung pada tanggal T.
Expected : Universe dan data setelah tanggal T tidak digunakan.

BT-06 - Entry berikutnya
Skenario : Ready to Enter muncul pada candle T.
Expected : Posisi masuk pada Open candle T+1, bukan Close candle T.

BT-07 - Stop-loss
Skenario : Low menyentuh stop.
Expected : Posisi ditutup pada harga stop setelah tick rounding.

BT-08 - Target
Skenario : High menyentuh target.
Expected : Posisi ditutup pada harga target.

BT-09 - Stop dan target pada candle sama
Skenario : High menyentuh target dan Low menyentuh stop.
Expected : Stop diprioritaskan sebagai hasil konservatif.

BT-10 - Gap melewati stop
Skenario : Open berada di bawah stop.
Expected : Exit menggunakan harga Open aktual.

BT-11 - Invalidation
Skenario : Setup/tren dinyatakan tidak valid pada Close.
Expected : Exit dilakukan pada Open candle berikutnya.

BT-12 - Max hold
Skenario : Stop, target, dan invalidation tidak terjadi hingga batas waktu.
Expected : Posisi keluar tepat ketika max hold tercapai.

BT-13 - Exit sebelum max hold
Skenario : Stop atau target tercapai sebelum batas waktu.
Expected : Posisi langsung keluar dan tidak menunggu max hold.

BT-14 - Fee dan slippage
Skenario : Transaksi beli dan jual selesai.
Expected : Fee dan slippage dikurangkan dari hasil transaksi.

BT-15 - Fraksi harga
Skenario : Harga hasil kalkulasi tidak sesuai tick BEI.
Expected : Harga dibulatkan sesuai aturan fraksi yang ditentukan.

BT-16 - Data kosong atau NaN
Skenario : Sebagian data OHLCV/indikator tidak tersedia.
Expected : Ticker dilewati tanpa menghentikan seluruh backtest.

BT-17 - Saham suspend
Skenario : Tidak ada transaksi pada tanggal yang seharusnya menjadi entry/exit.
Expected : Engine tidak menciptakan harga transaksi palsu atau melakukan
           forward-fill sebagai fill transaksi.

BT-18 - Status hard filter
Skenario : Kandidat gagal pada tren, likuiditas, harga, ATR, extension, atau wick.
Expected : Tidak boleh berstatus Ready to Enter.

BT-19 - Ranking
Skenario : Terdapat beberapa kandidat pada tanggal yang sama.
Expected : Ranking menggunakan quality score, RS percentile, breakout, volume,
           dan turnover sesuai urutan yang ditentukan screener.

BT-20 - Top tiga
Skenario : Terdapat lebih dari tiga kandidat Ready to Enter.
Expected : Hanya tiga kandidat dengan ranking tertinggi yang dipilih.

BT-21 - Slot portfolio penuh
Skenario : Sudah terdapat tiga posisi aktif.
Expected : Tidak ada posisi baru yang dibuka.

BT-22 - Tidak ada Ready to Enter
Skenario : Hanya terdapat Wait for Trigger atau Skip.
Expected : Tidak ada transaksi dan modal tetap menjadi cash.

BT-23 - Modal tidak cukup
Skenario : Modal yang tersedia tidak cukup untuk membeli satu lot.
Expected : Transaksi tidak dijalankan.

BT-24 - Posisi duplikat
Skenario : Ticker yang sama muncul kembali ketika masih dimiliki.
Expected : Tidak membuka posisi tambahan untuk ticker tersebut.


7. TAHAPAN EKSEKUSI
-------------------

TAHAP 1 - Validasi logika

- Jalankan BT-01 sampai BT-24 menggunakan fixture/data kecil yang hasilnya
  dapat dihitung manual.
- Seluruh test case harus lulus sebelum pengujian performa dimulai.

TAHAP 2 - Baseline

Jalankan konfigurasi awal berikut tanpa tuning:

- Daily  : max hold 10 bar.
- Weekly : max hold 8 bar.
- Monthly: max hold 6 bar.

Simpan hasil baseline sebagai pembanding seluruh eksperimen berikutnya.

TAHAP 3 - Eksperimen max hold

Pada dataset validation, bandingkan:

- Daily  : 5 vs 10 vs 15 bar.
- Weekly : 4 vs 8 vs 12 bar.
- Monthly: 3 vs 6 vs 9 bar.

Pilih max hold berdasarkan:

1. Expectancy setelah biaya.
2. Profit factor.
3. Maximum drawdown.
4. Konsistensi tahunan.
5. Jumlah transaksi.
6. Persentase exit karena max hold.
7. Rata-rata penggunaan modal.

Win rate tidak digunakan sebagai satu-satunya dasar pemilihan.

TAHAP 4 - Eksperimen RS monthly

Setelah max hold dipilih, bandingkan rs_lookback monthly:

- 6 bulan.
- 9 bulan.
- 12 bulan.

Parameter lainnya tetap dikunci. Jika perbedaannya tidak material atau jumlah
transaksi terlalu kecil, pertahankan konfigurasi yang paling sederhana.

TAHAP 5 - Final holdout

- Kunci seluruh parameter.
- Jalankan satu kali pada periode Januari 2024 - Agustus 2026.
- Jangan melakukan tuning berdasarkan hasil holdout.
- Catat kegagalan atau kelemahan sebagai input untuk V2.

TAHAP 6 - Portfolio top-3

Gunakan parameter final untuk mensimulasikan penggunaan sebenarnya:

- Maksimal tiga posisi aktif.
- Tidak membuka posisi duplikat.
- Slot kosong diisi pada jadwal screening berikutnya.
- Position sizing mengikuti risk budget dan ketersediaan modal.
- Cash yang tidak terpakai tetap dicatat.


8. METRIK HASIL
---------------

Metrik wajib dihitung secara keseluruhan dan dikelompokkan berdasarkan
timeframe, tahun, market regime, setup, serta rentang quality score:

- Total signal.
- Total trade.
- Win rate.
- Average dan median win.
- Average dan median loss.
- Expectancy dalam R.
- Profit factor.
- Net return setelah biaya.
- Maximum drawdown.
- Average holding period.
- Maximum Adverse Excursion (MAE).
- Maximum Favorable Excursion (MFE).
- Persentase exit karena stop.
- Persentase exit karena target.
- Persentase exit karena invalidation.
- Persentase exit karena max hold.
- Exposure dan penggunaan modal.
- Turnover.
- CAGR untuk portfolio-level backtest.


9. KRITERIA KELULUSAN
---------------------

V1 dianggap layak dilanjutkan jika pada dataset holdout:

- Expectancy setelah biaya positif.
- Profit factor minimal 1.15.
- Maximum drawdown berada di bawah batas risiko yang ditetapkan sebelum test.
- Hasil tidak bergantung pada satu saham atau satu tahun tertentu.
- Strategi tidak hanya positif ketika market bullish.
- Kelompok quality score tinggi menunjukkan hasil lebih baik daripada kelompok
  score rendah.
- Jumlah transaksi cukup untuk mendukung kesimpulan.

Minimum sampel yang disarankan:

- Daily  : minimal 100 transaksi.
- Weekly : minimal 50 transaksi.
- Monthly: minimal 30 transaksi.

Jika jumlah transaksi belum mencukupi, hasil diberi status INCONCLUSIVE dan
tidak langsung dianggap berhasil atau gagal.


10. OUTPUT BACKTEST
-------------------

File output minimum:

- backtest_trades.csv
- backtest_summary.csv
- backtest_by_year.csv
- backtest_by_regime.csv
- backtest_by_setup.csv
- backtest_by_score.csv
- max_hold_comparison.csv
- equity_curve.png
- drawdown_curve.png
- backtest_final_report.txt

Laporan final harus mencantumkan:

- Konfigurasi yang diuji.
- Max hold terpilih per timeframe.
- Hasil development, validation, dan holdout secara terpisah.
- Perbandingan signal-level dengan portfolio top-3.
- Kelemahan dan keterbatasan V1.
- Daftar perubahan yang direkomendasikan untuk V2.


11. URUTAN FINAL
----------------

Validasi engine
-> Baseline
-> Eksperimen max hold
-> Eksperimen RS monthly
-> Kunci parameter
-> Final holdout
-> Simulasi portfolio top-3
-> Dokumentasikan kekurangan V1 untuk V2

