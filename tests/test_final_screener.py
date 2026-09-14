from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_final_screener_exposes_only_weekly_and_monthly_horizons():
    screener = load(ROOT / "scripts" / "financial_screener.py", "final_screener_test")
    assert screener.BACKTEST_HOLD_GRID == {
        "weekly_position": [(8, 40)],
        "monthly_long_term": [(6, 126)],
    }
    assert set(screener.RANKING_MODELS) == {"weekly_position", "monthly_long_term"}
    with np.testing.assert_raises(SystemExit):
        screener.main(["--timeframe", "daily_swing"])


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
