"""Final weekly and monthly IDX momentum screener.

It reuses the shared causal features, candidate construction, reporting, and
CLI while supplying the frozen weekly/monthly metrics and ranking weights.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


_RANKING_PATH = Path(__file__).with_name("screener_ranking.py")
_SPEC = importlib.util.spec_from_file_location("screener_ranking_base", _RANKING_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load ranking core: {_RANKING_PATH}")
_RANKING = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RANKING
_SPEC.loader.exec_module(_RANKING)

for _name in dir(_RANKING):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_RANKING, _name)


VARIANT_ID = "WEEKLY_MONTHLY_MOMENTUM"
RANKING_MODELS = {
    "weekly_position": {
        "rs_13": 0.20,
        "rs_26": 0.40,
        "high_52": 0.20,
        "risk_adjusted_26": 0.20,
    },
    "monthly_long_term": dict(_RANKING.RANKING_MODELS["monthly_long_term"]),
}
BACKTEST_HOLD_GRID = {
    "weekly_position": [(8, 40)],
    "monthly_long_term": [(6, 126)],
}

_BASE_TIMEFRAME_METRICS = _RANKING._timeframe_metrics
_BASE_MARKET_REGIME = _RANKING.analisa_market_regime
_BASE_RANKING_CANDIDATES = _RANKING.ranking_candidates
_BASE_MAIN = _RANKING.main


def _weekly_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    turnover20: float,
) -> dict:
    base = _BASE_TIMEFRAME_METRICS(frame, benchmark, "weekly_position", turnover20)
    current, previous = frame.iloc[-1], frame.iloc[-2]
    close = float(current["Close"])
    atr = _RANKING._finite(current["ATR"])
    extension_atr = (close - _RANKING._finite(current["EMA20"])) / atr if atr > 0 else np.inf
    rsi = _RANKING._finite(current["RSI"])
    setup_valid = bool(
        base["momentum_valid"]
        and base["components"]["high_52"] >= 0.80
        and extension_atr <= 2.0
        and 50 <= rsi <= 72
    )
    active = bool(
        setup_valid
        and close > _RANKING._finite(current["EMA10"], close)
        and close > _RANKING._finite(previous["Close"], close)
    )
    return {
        **base,
        "setup_valid": setup_valid,
        "trigger_active": active,
        "trigger_reference": (
            close if active else _RANKING._finite(current["Prev_High4"], close)
        ),
        "setup_name": (
            "Weekly Momentum Rebalance" if active
            else "Weekly Momentum - Wait Recovery" if setup_valid
            else "No Valid Weekly Momentum"
        ),
    }


def _timeframe_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    mode: str,
    turnover20: float,
) -> dict:
    if mode == "weekly_position":
        return _weekly_metrics(frame, benchmark, turnover20)
    if mode == "monthly_long_term":
        return _BASE_TIMEFRAME_METRICS(frame, benchmark, mode, turnover20)
    raise ValueError(f"Timeframe tidak didukung: {mode}")


def analisa_market_regime(ihsg_daily: pd.DataFrame, mode_tren: str, breadth: dict) -> dict:
    regime = _BASE_MARKET_REGIME(ihsg_daily, mode_tren, breadth)
    mode = normalize_timeframe(mode_tren)
    if mode == "weekly_position":
        regime["market_trend_ok"] = bool(
            regime.get("market_trend_ok", False) and regime.get("regime") != "BEARISH"
        )
    return regime


def ranking_candidates(candidates: list[dict], limit: int = 3):
    """Weekly publishes Ready candidates only; monthly keeps its frozen policy."""
    weekly = any(item.get("timeframe") == "weekly_position" for item in candidates)
    eligible = (
        [item for item in candidates if item.get("status") == STATUS_READY]
        if weekly else candidates
    )
    return _BASE_RANKING_CANDIDATES(eligible, limit)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = normalize_timeframe(args.timeframe)
    if mode not in BACKTEST_HOLD_GRID:
        raise ValueError("Screener final hanya mendukung weekly_position dan monthly_long_term")
    if not 1 <= args.top <= 3:
        raise ValueError("--top harus antara 1 dan 3")
    return _BASE_MAIN(argv)


# Shared ranking functions resolve these collaborators in their own module.
_RANKING.TIMEFRAME_METRICS = _timeframe_metrics
_RANKING.RANKING_MODELS = RANKING_MODELS
_RANKING.VARIANT_ID = VARIANT_ID
_RANKING.analisa_market_regime = analisa_market_regime
_RANKING.ranking_candidates = ranking_candidates


if __name__ == "__main__":
    raise SystemExit(main())
