# CacingNagaPRO — Screening Result Validation Plan

**Status:** READY FOR SCREENING PERFORMANCE TESTING  
**Tanggal:** 2026-09-03  
**Revisi:** FOCUSED - Test screening results through backtesting to improve stock selection accuracy  
**Scope:** Validate that the screener identifies profitable stocks, improve screening methodology based on backtest results

---

## 1. Tujuan: Validasi Hasil Screening

**Pertanyaan Utama:** Apakah screener kita benar-benar mengidentifikasi saham yang profitable?

**Tujuan Spesifik:**
1. **Validasi akurasi screening:** Apakah saham yang direkomendasikan screener menghasilkan profit?
2. **Identifikasi kriteria terbaik:** Faktor apa yang paling akurat untuk memilih saham?
3. **Optimasi methodology:** Bagaimana meningkatkan akurasi screening?
4. **Bandingkan timeframe:** Timeframe mana yang memberikan hasil terbaik?
5. **Risk-return analysis:** Apakah R:R ratio yang digunakan optimal?

---

## 2. Metrik Keberhasilan Screening

**Primary Metrics (Fokus Utama):**
- **Screening Accuracy:** Persentase rekomendasi yang menghasilkan profit
- **Win Rate:** Persentase trade yang profit (target > 50% ideal)
- **Expectancy (R):** Average R-multiple per trade yang direkomendasikan (> 1.0R ideal)
- **Profit Factor:** Ratio gross profit vs gross loss dari rekomendasi (> 2.0 ideal)

**Secondary Metrics (Untuk Diagnosis):**
- **Signal-to-Trade Conversion:** Persentase screening yang menjadi trade aktual
- **Target Hit Rate:** Persentase rekomendasi yang mencapai target
- **Average Hold Duration:** Rata-rata holding period untuk rekomendasi
- **Maximum Drawdown:** Drawdown maksimal dari following screening recommendations

**Screening Quality Metrics:**
- **False Positive Rate:** Persentase rekomendasi yang ternyata loss
- **True Positive Rate:** Persentase rekomendasi yang benar-benar profit
- **Recommendation Consistency:** Konsistensi rekomendasi across market conditions

## 3. Metodologi Testing Screening

### Step 1: Jalankan 3 Command untuk Setiap Timeframe
```bash
# Backtest ketiga timeframe secara terpisah
python3 tests/test_financial_screener.py --trend 1hari --start 2022-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1hari
python3 tests/test_financial_screener.py --trend 1minggu --start 2020-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1minggu
python3 tests/test_financial_screener.py --trend 1bulan --start 2018-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1bulan
```

### Step 2: Analisis Hasil Screening secara Komprehensif
Buka semua file yang dihasilkan dan bandingkan:

**Screening Accuracy Comparison:**
- `BACKTEST_REPORT_1hari.md` vs `BACKTEST_REPORT_1minggu.md` vs `BACKTEST_REPORT_1bulan.md`
- Timeframe mana yang paling akurat?

**Factor Analysis:**
- `summary_setup_1hari.csv` vs `summary_setup_1minggu.csv` vs `summary_setup_1bulan.csv`
- Setup type mana yang paling profitable across timeframes?

**Market Condition:**
- `summary_regime_1hari.csv` vs `summary_regime_1minggu.csv` vs `summary_regime_1bulan.csv`
- Regime mana yang paling/least akurat?

### Step 3: Identifikasi Improvement Areas
Dari comprehensive analysis, tentukan:

**Jika semua timeframe accuracy < 45%:**
- Screening methodology fundamental need adjustment
- Adjust factor weights secara drastis
- Consider ubah timeframe prioritization

**Jika satu timeframe jauh lebih baik:**
- Focus pada timeframe tersebut untuk live trading
- Understand why timeframe tersebut lebih akurat
- Apply lessons dari timeframe tersebut ke timeframe lain

**Jika specific factors consistently accurate:**
- Increase weights untuk factors tersebut
- Reduce/remove factors yang noisy
- Simplify methodology hanya dengan faktor yang terbukti akurat

### Step 4: Optimasi dan Re-test
Modifikasi parameter di `financial_screener.py` lalu jalankan ulang:

```bash
# Test screening methodology yang dioptimasi
python3 tests/test_financial_screener.py --trend 1hari --start 2022-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v2/1hari
python3 tests/test_financial_screener.py --trend 1minggu --start 2020-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v2/1minggu
python3 tests/test_financial_screener.py --trend 1bulan --start 2018-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v2/1bulan
```

Bandingkan hasil sebelum vs sesudah improvement.

## 4. Analisis Detail Screening Results

### File Output untuk Screening Analysis
Setiap backtest menghasilkan file-file untuk screening analysis:

**Primary Files:**
- `signals_<timeframe>.csv` - Semua rekomendasi screener yang di-generate
- `trades_<timeframe>.csv` - Rekomendasi yang ter-trigger dan dieksekusi
- `BACKTEST_REPORT_<timeframe>.md` - Ringkasan performa screening
- `config_snapshot.json` - Konfigurasi screening untuk reproducibility

**Analysis Files untuk Factor Analysis:**
- `summary_regime_<timeframe>.csv` - Screening accuracy per market regime
- `summary_setup_<timeframe>.csv` - Screening accuracy per setup type
- `summary_score_<timeframe>.csv` - Screening accuracy per quality score range
- `summary_rs_<timeframe>.csv` - Screening accuracy per RS percentile

### Cara Membaca Hasil Screening

**1. BACKTEST_REPORT - Screening Accuracy Summary**
```markdown
- Signals: 150           # Total rekomendasi screener
- Triggered trades: 45   # Rekomendasi yang dieksekusi
- Entry rate: 30%        # Persentase rekomendasi yang menjadi trade
- Win rate: 51%          # Persentase rekomendasi yang profit
- Target hit rate: 38%   # Persentase rekomendasi yang mencapai target
- Expectancy: 0.8R       # Average R-multiple per rekomendasi
- Profit factor: 1.8     # Ratio profit/loss dari rekomendasi
- Max drawdown: -8.5R    # Drawdown maksimal dari following screening
- Average hold: 8 sessions # Rata-rata holding rekomendasi
```

**2. CSV Analysis Files - Factor Performance Analysis**
- **summary_regime:** Cari regime mana yang screening paling akurat
- **summary_setup:** Cari setup type mana yang paling profitable
- **summary_score:** Cari quality score range mana yang paling akurat
- **summary_rs:** Cari RS percentile mana yang paling presiktif

### Decision Making dari Screening Results

**Jika Screening Accuracy < 40%:**
- Screener menghasilkan terlalu banyak false signals
- Review criteria screening - mungkin terlalu longgar
- Check factor weights - mungkin faktor yang kurang akurat diberi bobot tinggi
- Analyze market regime - mungkin screener gagal di kondisi tertentu

**Jika Win Rate Tinggi tapi Expectancy Rendah:**
- Screener mengidentifikasi saham profitable tapi R:R tidak optimal
- Check stop loss - mungkin terlalu longgar atau placement tidak akurat
- Review target taking - mungkin terlalu dekat atau tidak memaksimalkan profit
- Analyze hold duration - mungkin exit terlalu cepat/terlambat

**Jika Performance Sangat Beragam:**
- Screener tidak konsisten di berbagai kondisi
- Analyze regime performance - add regime-based adjustment
- Check setup types - prefer setup yang lebih konsisten
- Review timeframe - mungkin timeframe tertentu lebih akurat

## 5. Iterative Screening Improvement

### Siklus Screening Optimization

```
BASELINE → ANALYZE → OPTIMIZE → VALIDATE → REPEAT
```

**Step 1: Baseline Screening Performance**
- Jalankan backtest dengan criteria screening current
- Catat screening accuracy, win rate, expectancy, profit factor
- Simpan sebagai baseline comparison

**Step 2: Analyze Screening Results**
- Identifikasi faktor yang paling/least akurat dalam stock selection
- Cari pola dalam rekomendasi profitable vs unprofitable
- Analyze performance across market conditions dan timeframes
- Hypothesis perubahan yang bisa meningkatkan akurasi screening

**Step 3: Optimize Screening Methodology**
- Implement perubahan berdasarkan analysis
- Adjust factor weights dan thresholds
- Modify screening criteria untuk faktor yang underperform
- Add/remove confirmation factors untuk reduce false signals

**Step 4: Validate Screening Improvement**
- Jalankan backtest dengan screening methodology baru
- Bandingkan dengan baseline
- Pastikan improvement signifikan dan bukan kebetulan
- Validate improvement tidak hanya datanya tapi juga methodology

**Step 5: Repeat sampai Target Tercapai**
- Jika improvement → lanjut ke optimasi berikutnya
- Jika tidak → revert dan coba hypothesis lain
- Terus iterate sampai screening accuracy target tercapai

### Target Screening Accuracy yang Realistis

**Short-term (1-2 weeks):**
- Screening accuracy > 45% (dari baseline mungkin 35-40%)
- Win rate > 45% (dari baseline mungkin 35-40%)
- Expectancy > 0.6R (dari baseline mungkin 0.3-0.5R)

**Medium-term (1-2 months):**
- Screening accuracy > 50%
- Win rate > 50%
- Expectancy > 0.8R
- Profit factor > 1.5

**Long-term (3-6 months):**
- Screening accuracy > 55%
- Win rate > 55%
- Expectancy > 1.0R
- Profit factor > 2.0

## 6. Simple Testing Commands

### 3 Command untuk Semua Timeframes (Universe Screening)
```bash
# Backtest semua timeframes untuk universe screening
python3 tests/test_financial_screener.py --trend 1hari --start 2022-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1hari
python3 tests/test_financial_screener.py --trend 1minggu --start 2020-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1minggu
python3 tests/test_financial_screener.py --trend 1bulan --start 2018-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1bulan
```

**Catatan Penting (Sesuai README.md):**
- `--trend all` HANYA untuk 1 saham spesifik (harus dengan `--ticker`)
- Untuk universe screening semua saham, gunakan `--trend 1hari/1minggu/1bulan` tanpa `--ticker`

### Live Screening (Harian)
```bash
# Daily screening untuk hari ini
python3 scripts/financial_screener.py --trend 1hari
```

## 7. Analysis of Screening Results

### Fokus pada Hasil Screening, Bukan Fitur

**Yang TIDAK perlu dites:**
❌ Apakah fungsi RSI calculation benar?
❌ Apakah EMA cross-over bekerja?
❌ Apakah volume ratio calculation akurat?
❌ Apakah code error-free?

**Yang PERLU dites:**
✅ Apakah screener mengidentifikasi saham yang profitable?
✅ Apakah rekomendasi screener menghasilkan profit?
✅ Faktor mana yang paling akurat untuk stock selection?
✅ Bagaimana meningkatkan akurasi screening?

### Framework Analisis Screening Results

**1. Screening Accuracy Analysis**
```python
# Questions yang harus dijawab:
- Apakah % rekomendasi yang profit > 50%?
- Apakah screener lebih akurat dari random selection?
- Apakah screener menghasilkan alpha di atas market?
- Berapa banyak false signals yang dihasilkan?
```

**2. Factor Contribution Analysis**
```python
# Dari summary files, identify:
- Faktor mana yang paling korelasi dengan profitable outcomes?
- Bobot faktor saat ini optimal atau perlu adjustment?
- Apakah ada faktor yang sebenarnya noisy/irrelevant?
- Kombinasi faktor mana yang memberikan hasil terbaik?
```

**3. Market Condition Analysis**
```python
# Performance berdasarkan kondisi:
- Apakah screener bekerja baik di bull/bear/sideways market?
- Regime mana yang screener paling/least akurat?
- Apakah perlu regime-based adjustment untuk screening?
- Bagaimana screener perform di high vs low volatility periods?
```

**4. Timeframe Comparison**
```python
# Bandingkan accuracy across timeframes:
- Timeframe mana yang paling akurat untuk stock selection?
- Apakah daily/weekly/monthly memberikan hasil yang berbeda?
- Apakah multi-timeframe confirmation meningkatkan accuracy?
- Timeframe mana yang paling practical untuk implementasi?
```

### Practical Analysis Workflow

**Step 1: Baca Primary Results**
```bash
# Baca BACKTEST_REPORT untuk melihat:
- Screening accuracy keseluruhan
- Win rate dari rekomendasi
- Expectancy per rekomendasi
- Profit factor dari screening results
```

**Step 2: Deep Dive Analysis**
```bash
# Buka CSV files untuk detail analysis:
# summary_regime_<tf>.csv → regime performance
# summary_setup_<tf>.csv → setup type performance  
# summary_score_<tf>.csv → quality score performance
# summary_rs_<tf>.csv → relative strength performance
```

**Step 3: Identifikasi Improvement Areas**
```python
# Dari analysis, tentukan:
- Faktor yang paling akurat → pertahankan/optimize
- Faktor yang kurang akurat → adjust bobot atau remove
- Market condition yang profitable → focus pada kondisi tersebut
- Timeframe yang terbaik → prioritize untuk implementation
```

**Step 4: Implement Changes dan Re-test**
```bash
# Modify screening methodology → jalankan ulang → bandingkan hasil
# Repeat cycle sampai screening accuracy target tercapai
```

## 8. Common Screening Issues & Solutions

### Issue: Low Screening Accuracy (< 35%)
**Symptoms:**
- Screener menghasilkan banyak rekomendasi yang loss
- Win rate jauh di bawah random selection
- Banyak false signals

**Possible Causes:**
- Screening criteria terlalu longgar
- Faktor teknikal kurang akurat untuk market saat ini
- Faktor weights tidak optimal
- Market condition tidak sesuai dengan methodology

**Solutions:**
- Naikkan quality score threshold
- Tighten individual factor thresholds (RSI range, ATR% range, dll)
- Adjust factor weights based on contribution analysis
- Add regime filter untuk avoid kondisi market yang tidak cocok

### Issue: High Screening Accuracy tapi Low Expectancy
**Symptoms:**
- Screener sering benar dalam mengidentifikasi saham profitable
- Tapi R:R ratio kurang optimal
- Profit per trade tidak maksimal

**Possible Causes:**
- Entry timing tidak optimal
- Stop loss placement tidak akurat
- Target taking terlalu cepat/terlambat
- Risk management parameters tidak optimal

**Solutions:**
- Adjust entry trigger untuk lebih presisi
- Optimize stop loss berdasarkan structure/volatility
- Improve target taking dengan trail stop atau multi-level targets
- Adjust ATR multiplier untuk stop/target

### Issue: Sangat Variasi Performance
**Symptoms:**
- Screener sangat akurat di kondisi tertentu tapi sangat buruk di kondisi lain
- Performance tidak konsisten across time
- Tidak bisa dipercaya untuk live trading

**Possible Causes:**
- Methodology tidak robust untuk berbagai market conditions
- Tidak ada regime-based adjustment
- Faktor yang cocok untuk kondisi tertentu tapi tidak untuk yang lain

**Solutions:**
- Add regime-based screening adjustment
- Prefer timeframes/factors yang lebih konsisten
- Add market condition filter untuk screening
- Reduce exposure di kondisi yang less favorable

### Issue: Over-Optimization (Curve Fitting)
**Symptoms:**
- Sangat akurat di backtest period tapi gagal di live
- Performance tidak reproducible di period berbeda
- Terlalu banyak parameter adjustments

**Solutions:**
- Test di multiple time periods (train/test/validation)
- Avoid over-tuning parameter
- Keep methodology simple dan robust
- Validate di out-of-sample period

## 9. Success Criteria for Screening

### Screening Accuracy Targets

**Minimum Acceptable Performance:**
- **Screening Accuracy:** > 40% (rekomendasi yang profit)
- **Win Rate:** > 45% (trade yang dieksekusi yang profit)
- **Expectancy:** > 0.5R (average profit per rekomendasi)
- **Profit Factor:** > 1.5 (ratio profit/loss)

**Good Performance:**
- **Screening Accuracy:** > 50%
- **Win Rate:** > 50%
- **Expectancy:** > 0.8R
- **Profit Factor:** > 1.8

**Excellent Performance:**
- **Screening Accuracy:** > 55%
- **Win Rate:** > 55%
- **Expectancy:** > 1.0R
- **Profit Factor:** > 2.0

### Practical Trading Criteria

**Untuk Live Trading:**
- Screener harus menghasilkan minimum 2-3 rekomendasi per minggu
- Screening accuracy harus konsisten across berbagai market conditions
- Maximum drawdown dari following screener harus terkendali (< -15R)
- Signal-to-trade conversion harus reasonable (20-40%)

### Consistency Requirements

**Screening methodology harus:**
- Konsisten performanya di berbagai time periods
- Tidak over-fitted ke data tertentu
- Reproducible dengan parameter yang sama
- Robust terhadap perubahan market conditions

## 10. Implementation Roadmap

### Step 1: Baseline Performance (Sekarang)
**Target:** Dapatkan baseline performance screener saat ini

**3 Commands untuk Baseline:**
```bash
python3 tests/test_financial_screener.py --trend 1hari --start 2022-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1hari
python3 tests/test_financial_screener.py --trend 1minggu --start 2020-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1minggu
python3 tests/test_financial_screener.py --trend 1bulan --start 2018-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1bulan
```

**Analisis:** Baca semua reports dan summary files untuk baseline performance

### Step 2: Optimasi Berdasarkan Data (Setelah Baseline)
**Target:** Improve screening methodology berdasarkan hasil analysis

**Process:**
1. Modifikasi parameter di `financial_screener.py` (thresholds, weights, dll)
2. Jalankan ulang dengan nama folder baru
3. Bandingkan hasil dengan baseline

**Commands:**
```bash
python3 tests/test_financial_screener.py --trend 1hari --start 2022-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v2/1hari
python3 tests/test_financial_screener.py --trend 1minggu --start 2020-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v2/1minggu
python3 tests/test_financial_screener.py --trend 1bulan --start 2018-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v2/1bulan
```

**Ulangi sampai target tercapai:**
- `output/screening_v3/1hari`, `output/screening_v3/1minggu`, `output/screening_v3/1bulan`
- `output/screening_v4/1hari`, `output/screening_v4/1minggu`, `output/screening_v4/1bulan`
- dst.

### Success Criteria:
- Screening accuracy > 50% untuk best timeframe
- Win rate > 50% untuk best timeframe
- Expectancy > 0.8R untuk best timeframe

## 11. Summary & Next Steps

### Key Philosophy
**Fokus 100% pada Hasil Screening:**
- Tidak peduli apakah code "perfect" atau fitur "berfungsi"
- Yang penting: apakah screener mengidentifikasi saham yang profitable?
- Backtest adalah tool untuk validasi screening methodology
- Improvement berdasarkan data, bukan engineering perfection

### Immediate Next Steps

**1. Jalankan Baseline Screening Test (Sekarang):**
```bash
python3 tests/test_financial_screener.py --trend 1hari --start 2022-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1hari
python3 tests/test_financial_screener.py --trend 1minggu --start 2020-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1minggu
python3 tests/test_financial_screener.py --trend 1bulan --start 2018-01-01 --end 2024-12-31 --top 3 --signal-step 1 --output-dir output/screening_v1/1bulan
```

**2. Review Semua Hasil Sekaligus:**
- Baca semua `BACKTEST_REPORT_*.md` files - bandingkan performance 3 timeframe
- Buka semua `summary_*.csv` files - identify factors yang paling akurat
- Tentukan timeframe mana yang paling akurat untuk live trading

**3. Decide on Next Action:**
- Jika salah satu timeframe sudah >50% accuracy → use untuk live trading
- Jika semua timeframe masih kurang → adjust methodology dan re-test sebagai `screening_v2`
- Jika ingin improve further → optimize parameters dan re-test

### Success Definition
**Screening Methodology Sukses Jika:**
- Screening accuracy > 50% (minimal 40% acceptable)
- Win rate > 50% (minimal 45% acceptable)
- Expectancy > 0.8R (minimal 0.5R acceptable)
- Consistent performance across market conditions
- Reproducible dengan parameter yang sama

### Final Note
**Ini bukan software testing - ini trading methodology validation:**
- Backtest adalah tool untuk mengukur akurasi screening
- Focus adalah mengidentifikasi saham profitable, bukan testing code
- Improvement berdasarkan hasil trading, bukan engineering metrics
- Success diukur dengan win rate dan profit, bukan test coverage

---

**Start with the baseline test dan let the screening results guide your improvements!**
