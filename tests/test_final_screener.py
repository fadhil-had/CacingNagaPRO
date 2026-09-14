from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_final_screener_exposes_all_three_frozen_horizons():
    screener = load(ROOT / "scripts" / "financial_screener.py", "final_screener_test")
    assert screener.BACKTEST_HOLD_GRID == {
        "daily_swing": [(10, 10)],
        "weekly_position": [(8, 40)],
        "monthly_long_term": [(6, 126)],
    }
    assert screener.RANKING_MODELS["daily_swing"] == {
        "ret60": 1 / 3,
        "atr_pct": 1 / 3,
        "near_sma20": 1 / 3,
    }
    assert screener.parse_args(["--timeframe", "daily_swing"]).timeframe == "daily_swing"


def test_daily_score_uses_u0_universe_and_ignores_market_gate():
    screener = load(ROOT / "scripts" / "financial_screener.py", "daily_policy_test")

    def candidate(ticker: str, value: float, *, trend: bool = True):
        return {
            "ticker": ticker,
            "timeframe": "daily_swing",
            "daily_u0_pass": True,
            "hard_pass": trend,
            "hard_fail_reasons": [] if trend else ["SMA50 belum naik"],
            "conditions": {"rising_sma50": trend},
            "v4_components": {
                "ret60": value,
                "atr_pct": value,
                "near_sma20": value,
            },
        }

    candidates = [
        candidate("AAA.JK", 5),
        candidate("BBB.JK", 4),
        candidate("CCC.JK", 3),
        candidate("DDD.JK", 2, trend=False),
        candidate("EEE.JK", 1),
    ]
    screener.finalisasi_score_dan_status(
        candidates,
        "daily_swing",
        {"market_trend_ok": False},
    )

    assert candidates[0]["quality_score"] == 100
    assert candidates[1]["quality_score"] == 80
    assert candidates[0]["status"] == screener.STATUS_DAILY_WATCHLIST
    assert candidates[1]["status"] == screener.STATUS_DAILY_WATCHLIST
    assert candidates[2]["status"] == screener.STATUS_SKIP_SETUP
    assert candidates[3]["status"] == screener.STATUS_SKIP_TREND


def test_daily_top_three_is_displayed_as_equal_watchlist_without_trade_levels():
    screener = load(ROOT / "scripts" / "financial_screener.py", "daily_report_test")
    candidates = [
        {
            "ticker": ticker,
            "timeframe": "daily_swing",
            "status": screener.STATUS_DAILY_WATCHLIST,
            "quality_score": score,
            "harga_terakhir": 1_000,
            "rank_percentiles": {
                "ret60": score,
                "atr_pct": score,
                "near_sma20": score,
            },
        }
        for ticker, score in [("ZZZ.JK", 99), ("AAA.JK", 98), ("MMM.JK", 97)]
    ]
    selected, status = screener.ranking_candidates(candidates, limit=3)
    report = screener.deterministic_report(
        selected,
        "daily_swing",
        {"regime": "BEARISH", "close": 7_000, "breadth50": 0.4},
    )

    assert [item["ticker"] for item in selected] == ["AAA.JK", "MMM.JK", "ZZZ.JK"]
    assert all(item["selected_top3"] for item in selected)
    assert status == screener.STATUS_DAILY_WATCHLIST
    assert "tingkat keyakinan setara" in report
    assert "3% / 5% / 10%" in report
    assert "2% / 5%" in report
    assert "harga entry aktual" in report
    assert "| Entry |" not in report


def test_daily_candidate_uses_validated_features_and_has_no_execution_levels():
    screener = load(ROOT / "scripts" / "financial_screener.py", "daily_feature_test")
    index = pd.bdate_range("2024-01-02", periods=300)
    close = pd.Series(np.linspace(500, 800, len(index)), index=index)
    raw = pd.DataFrame({
        "Open": close * 0.999,
        "High": close * 1.01,
        "Low": close * 0.99,
        "Close": close,
        "Volume": 30_000_000,
    })
    prepared = screener.siapkan_data_untuk_timeframe(raw, "daily_swing")
    result = screener.analisa_saham_confluence(
        "TEST.JK",
        raw,
        prepared,
        "daily_swing",
        1_000_000_000,
        100,
        prepared_tf=prepared,
    )

    assert not result.get("error")
    assert result["daily_u0_pass"]
    assert result["hard_pass"]
    assert result["v4_components"]["ret60"] == close.iloc[-1] / close.iloc[-61] - 1
    assert 0.015 <= result["v4_components"]["atr_pct"] <= 0.04
    assert result["avg_turnover20_prev"] >= screener.DAILY_AVG_TURNOVER20_MIN
    assert result["median_turnover20_prev"] >= screener.DAILY_MEDIAN_TURNOVER20_MIN
    assert result["min_turnover"] == screener.DAILY_AVG_TURNOVER20_MIN
    assert result["min_price"] == screener.DAILY_PRICE_MIN
    assert result["take_profit_options"] == (0.03, 0.05, 0.10)
    assert result["stop_loss_options"] == (0.02, 0.05)
    assert np.isnan(result["entry_level"])
    assert np.isnan(result["stop_level"])
    assert np.isnan(result["target_price"])


def test_weekly_only_publishes_ready_candidates():
    screener = load(ROOT / "scripts" / "financial_screener.py", "weekly_policy_test")
    candidates = [
        {"ticker": "READY.JK", "timeframe": "weekly_position",
         "status": screener.STATUS_READY, "quality_score": 70.0,
         "rs_percentile": 60.0, "turnover20": 2_000_000_000},
        {"ticker": "WAIT.JK", "timeframe": "weekly_position",
         "status": screener.STATUS_WAIT, "quality_score": 99.0,
         "rs_percentile": 99.0, "turnover20": 9_000_000_000},
    ]
    selected, status = screener.ranking_candidates(candidates, limit=3)
    assert [candidate["ticker"] for candidate in selected] == ["READY.JK"]
    assert status == screener.STATUS_READY


def test_monthly_keeps_validated_ranking_formula():
    screener = load(ROOT / "scripts" / "financial_screener.py", "monthly_policy_test")
    assert screener.RANKING_MODELS["monthly_long_term"] == {
        "rs_12_1": 0.50,
        "high_52": 0.30,
        "risk_adjusted_6": 0.20,
    }


def test_backtest_engine_self_test():
    engine = load(ROOT / "tests" / "backtest_engine.py", "backtest_engine_test")
    engine.run_self_test()
