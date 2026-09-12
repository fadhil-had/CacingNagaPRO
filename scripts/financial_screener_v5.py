"""V5 research candidate for daily recovery and weekly momentum.

Monthly remains exactly the V4 model.  V5 is intentionally small: it reuses
V4's causal features, candidate construction, reporting, and CLI, replacing
only the timeframe metrics and ranking weights under test.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


_V4_PATH = Path(__file__).with_name("financial_screener_v4.py")
_SPEC = importlib.util.spec_from_file_location("financial_screener_v5_base", _V4_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load V4 screener: {_V4_PATH}")
_V4 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _V4
_SPEC.loader.exec_module(_V4)

for _name in dir(_V4):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_V4, _name)


VARIANT_ID = "V5_DAILY_RECOVERY_WEEKLY_MOMENTUM"
RANKING_MODELS = {
    "daily_swing": {
        "rs_63": 0.35,
        "trend_quality_20": 0.35,
        "recovery_strength": 0.20,
        "volume_confirmation": 0.10,
    },
    "weekly_position": {
        "rs_13": 0.20,
        "rs_26": 0.40,
        "high_52": 0.20,
        "risk_adjusted_26": 0.20,
    },
    "monthly_long_term": dict(_V4.RANKING_MODELS["monthly_long_term"]),
}
BACKTEST_HOLD_GRID = {
    "daily_swing": [(5, 5), (10, 10), (15, 15)],
    "weekly_position": [(4, 20), (8, 40), (10, 50), (12, 60)],
    "monthly_long_term": [(3, 63), (6, 126), (9, 189)],
}

_V4_TIMEFRAME_METRICS = _V4._timeframe_metrics
_V4_MARKET_REGIME = _V4.analisa_market_regime


def _daily_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    turnover20: float,
) -> dict:
    current, previous = frame.iloc[-1], frame.iloc[-2]
    close = float(current["Close"])
    rs63, stock_return = _V4._excess_return(frame, benchmark, 63)
    prior_pullback = frame.iloc[-6:-1]
    pullback_depth = _V4._finite(prior_pullback["Z20"].min())
    pullback_rsi5 = _V4._finite(prior_pullback["RSI5"].min())
    rsi5 = _V4._finite(current["RSI5"])
    previous_rsi5 = _V4._finite(previous["RSI5"])
    rsi14 = _V4._finite(current["RSI"])
    trend_quality = _V4._finite(current["Trend_Quality20"])
    candle_range = max(_V4._finite(current["Candle_Range"], 0), 1e-9)
    strong_close = (close - _V4._finite(current["Low"], close)) / candle_range >= 0.70
    bullish = close > _V4._finite(current["Open"], close)
    vol_ma20 = _V4._finite(current["Vol_MA20"], 0)
    volume_ratio = _V4._finite(current["Volume"], 0) / vol_ma20 if vol_ma20 > 0 else 0.0
    volume_z = _V4._finite(current["Vol_Z"], 0)

    setup_valid = bool(
        np.isfinite(rs63)
        and rs63 > 0
        and trend_quality > 0.02
        and -1.75 <= pullback_depth <= -0.50
        and pullback_rsi5 < 40
        and 55 <= rsi14 <= 78
    )
    recovered = bool(
        setup_valid
        and bullish
        and strong_close
        and close > _V4._finite(previous["High"], close)
        and close > _V4._finite(current["EMA9"], close)
        and rsi5 > previous_rsi5
        and (volume_ratio >= 1.10 or volume_z >= 0.50)
    )
    return {
        "components": {
            "rs_63": rs63,
            "trend_quality_20": trend_quality,
            "recovery_strength": rsi5 - pullback_rsi5,
            "volume_confirmation": max(volume_ratio, 1 + volume_z),
        },
        "primary_rs": rs63,
        "stock_return": stock_return,
        "momentum_valid": bool(np.isfinite(rs63) and rs63 > 0),
        "setup_valid": setup_valid,
        "trigger_active": recovered,
        "trigger_reference": _V4._finite(previous["High"], close),
        "setup_name": (
            "Daily Pullback Recovery Confirmed" if recovered
            else "Daily Pullback - Wait Recovery" if setup_valid
            else "No Valid Daily Recovery"
        ),
    }


def _weekly_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    turnover20: float,
) -> dict:
    base = _V4_TIMEFRAME_METRICS(frame, benchmark, "weekly_position", turnover20)
    current, previous = frame.iloc[-1], frame.iloc[-2]
    close = float(current["Close"])
    atr = _V4._finite(current["ATR"])
    extension_atr = (close - _V4._finite(current["EMA20"])) / atr if atr > 0 else np.inf
    rsi = _V4._finite(current["RSI"])
    setup_valid = bool(
        base["momentum_valid"]
        and base["components"]["high_52"] >= 0.80
        and extension_atr <= 2.0
        and 50 <= rsi <= 72
    )
    active = bool(
        setup_valid
        and close > _V4._finite(current["EMA10"], close)
        and close > _V4._finite(previous["Close"], close)
    )
    return {
        **base,
        "setup_valid": setup_valid,
        "trigger_active": active,
        "trigger_reference": (
            close if active else _V4._finite(current["Prev_High4"], close)
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
    if mode == "daily_swing":
        return _daily_metrics(frame, benchmark, turnover20)
    if mode == "weekly_position":
        return _weekly_metrics(frame, benchmark, turnover20)
    return _V4_TIMEFRAME_METRICS(frame, benchmark, mode, turnover20)


def analisa_market_regime(ihsg_daily: pd.DataFrame, mode_tren: str, breadth: dict) -> dict:
    regime = _V4_MARKET_REGIME(ihsg_daily, mode_tren, breadth)
    mode = normalize_timeframe(mode_tren)
    if mode in {"daily_swing", "weekly_position"}:
        regime["market_trend_ok"] = bool(
            regime.get("market_trend_ok", False) and regime.get("regime") != "BEARISH"
        )
    return regime


# V4's reused functions resolve these names in the loaded V4 module.
_V4.TIMEFRAME_METRICS = _timeframe_metrics
_V4.RANKING_MODELS = RANKING_MODELS
_V4.VARIANT_ID = VARIANT_ID
_V4.analisa_market_regime = analisa_market_regime


if __name__ == "__main__":
    raise SystemExit(main())
