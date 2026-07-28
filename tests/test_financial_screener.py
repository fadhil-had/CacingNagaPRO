import sys
from pathlib import Path
import numpy as np
import pandas as pd
import unittest

sys.path.append(str(Path(__file__).resolve().parents[1]))
from scripts.financial_screener import analisa_saham_confluence, siapkan_data_untuk_timeframe


class TestTimeframePreparation(unittest.TestCase):
    def test_weekly_and_monthly_data_gain_indicator_columns(self):
        idx = pd.date_range("2024-01-01", periods=80, freq="D")
        df = pd.DataFrame(
            {
                "Open": 100.0,
                "High": 102.0,
                "Low": 99.0,
                "Close": 101.0,
                "Volume": 1000,
            },
            index=idx,
        )

        weekly = siapkan_data_untuk_timeframe(df.copy(), "1minggu")
        monthly = siapkan_data_untuk_timeframe(df.copy(), "1bulan")

        self.assertFalse(weekly.empty)
        self.assertFalse(monthly.empty)
        self.assertIn("EMA20", weekly.columns)
        self.assertIn("EMA50", weekly.columns)
        self.assertIn("RSI", monthly.columns)
        self.assertIn("MACD", monthly.columns)

    def test_weekly_requires_higher_score_to_be_strong_buy(self):
        idx = pd.date_range("2024-01-01", periods=140, freq="D")
        close = 100 + np.linspace(0, 6, len(idx))
        open_ = close - 0.2
        high = close + 0.8
        low = close - 0.8
        volume = np.full(len(idx), 800)
        df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=idx)

        result = analisa_saham_confluence("TEST.JK", df, "1minggu")

        self.assertEqual(result["status"], "Watchlist")

    def test_weekly_analysis_returns_support_resistance_and_target(self):
        idx = pd.date_range("2024-01-01", periods=160, freq="D")
        close = 100 + np.linspace(0, 8, len(idx))
        open_ = close - 0.3
        high = close + 1.0
        low = close - 1.0
        volume = np.full(len(idx), 1000)
        df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=idx)

        result = analisa_saham_confluence("TEST.JK", df, "1minggu")

        self.assertGreater(result["target_price"], result["harga_terakhir"])
        self.assertGreater(result["target_price"], result["resistance_level"])
        self.assertIn("Risiko", result["risk_note"])

    def test_daily_analysis_can_return_strong_buy_for_bullish_setup(self):
        idx = pd.date_range("2024-01-01", periods=140, freq="D")
        close = 100 + np.linspace(0, 8, len(idx))
        close[-14:] += np.array([0.2, -0.3, 0.4, -0.1, 0.5, -0.2, 0.3, 0.0, 0.6, -0.4, 0.7, -0.1, 0.2, 0.4])
        close[-1] = 110.0
        open_ = close - 0.25
        high = close + 0.8
        low = close - 0.8
        volume = np.full(len(idx), 1200)
        volume[-5:] = np.array([1800, 2200, 2600, 3000, 3200])
        df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=idx)

        result = analisa_saham_confluence("TEST.JK", df, "1hari")

        self.assertEqual(result["status"], "Strong Buy")


if __name__ == "__main__":
    unittest.main()
