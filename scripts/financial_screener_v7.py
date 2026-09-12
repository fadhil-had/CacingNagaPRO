"""V7 daily research candidate: momentum after volatility contraction.

Unlike V5/V6 pullback recovery, V7 looks for a strong stock consolidating near
its highs and waits for a volume-confirmed breakout. Weekly and monthly remain
the V5 models; the V7 decision concerns daily only.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


_V5_PATH = Path(__file__).with_name("financial_screener_v5.py")
_SPEC = importlib.util.spec_from_file_location("financial_screener_v7_base", _V5_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load V5 screener: {_V5_PATH}")
_V5 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _V5
_SPEC.loader.exec_module(_V5)

for _name in dir(_V5):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_V5, _name)


VARIANT_ID = "V7_DAILY_MOMENTUM_CONTRACTION"
RANKING_MODELS = {
    "daily_swing": {
        "rs_63": 0.30,
        "trend_quality_20": 0.25,
        "contraction_quality": 0.25,
        "high_52": 0.20,
    },
    "weekly_position": dict(_V5.RANKING_MODELS["weekly_position"]),
    "monthly_long_term": dict(_V5.RANKING_MODELS["monthly_long_term"]),
}
BACKTEST_HOLD_GRID = {
    "daily_swing": [(5, 5)],
    "weekly_position": [(8, 40)],
    "monthly_long_term": list(_V5.BACKTEST_HOLD_GRID["monthly_long_term"]),
}

CONTRACTION_SESSIONS = 10
MAX_CONTRACTION_RANGE_PCT = 0.12
MIN_HIGH52_POSITION = 0.80
MAX_EXTENSION_ATR = 2.0
MIN_BREAKOUT_VOLUME_RATIO = 1.20


def _daily_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    turnover20: float,
) -> dict:
    current = frame.iloc[-1]
    close = float(current["Close"])
    atr = _V5._V4._finite(current["ATR"])
    rs63, stock_return = _V5._V4._excess_return(frame, benchmark, 63)
    prior = frame.iloc[-(CONTRACTION_SESSIONS + 1):-1]

    prior_high = _V5._V4._finite(prior["High"].max(), close)
    prior_low = _V5._V4._finite(prior["Low"].min(), close)
    range_pct = (prior_high - prior_low) / close if close > 0 else np.inf
    prior_atr_pct = _V5._V4._finite(prior["ATR_Pct"].iloc[-1])
    atr_baseline = _V5._V4._finite(frame["ATR_Pct"].iloc[-61:-1].median())
    contraction_quality = (
        atr_baseline / prior_atr_pct
        if prior_atr_pct > 0 and np.isfinite(atr_baseline) else np.nan
    )
    atr_contracting = bool(
        np.isfinite(prior_atr_pct)
        and np.isfinite(atr_baseline)
        and prior_atr_pct <= atr_baseline
    )

    ema20 = _V5._V4._finite(current["EMA20"], close)
    ema50 = _V5._V4._finite(current["EMA50"], close)
    extension_atr = (close - ema20) / atr if atr > 0 else np.inf
    high52 = _V5._V4._finite(current["High52_Position"])
    rsi = _V5._V4._finite(current["RSI"])
    trend_quality = _V5._V4._finite(current["Trend_Quality20"])
    volume = _V5._V4._finite(current["Volume"], 0)
    volume_ma = _V5._V4._finite(current["Vol_MA20"], 0)
    volume_ratio = volume / volume_ma if volume_ma > 0 else 0.0
    candle_range = max(_V5._V4._finite(current["Candle_Range"], 0), 1e-9)
    close_location = (close - _V5._V4._finite(current["Low"], close)) / candle_range

    setup_valid = bool(
        len(prior) == CONTRACTION_SESSIONS
        and np.isfinite(rs63)
        and rs63 > 0
        and ema20 > ema50
        and trend_quality > 0
        and high52 >= MIN_HIGH52_POSITION
        and range_pct <= MAX_CONTRACTION_RANGE_PCT
        and atr_contracting
        and 50 <= rsi <= 72
        and extension_atr <= MAX_EXTENSION_ATR
    )
    breakout = bool(
        setup_valid
        and close > prior_high
        and close > float(current["Open"])
        and close_location >= 0.70
        and volume_ratio >= MIN_BREAKOUT_VOLUME_RATIO
    )
    return {
        "components": {
            "rs_63": rs63,
            "trend_quality_20": trend_quality,
            "contraction_quality": contraction_quality,
            "high_52": high52,
        },
        "primary_rs": rs63,
        "stock_return": stock_return,
        "momentum_valid": bool(np.isfinite(rs63) and rs63 > 0),
        "setup_valid": setup_valid,
        "trigger_active": breakout,
        "trigger_reference": prior_high,
        "setup_name": (
            "Daily Contraction Breakout" if breakout
            else "Daily Contraction - Wait Breakout" if setup_valid
            else "No Valid Daily Contraction"
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
    return _V5._timeframe_metrics(frame, benchmark, mode, turnover20)


# Reused V4 functions resolve these names in the original V4 module.
_V5._V4.TIMEFRAME_METRICS = _timeframe_metrics
_V5._V4.RANKING_MODELS = RANKING_MODELS
_V5._V4.VARIANT_ID = VARIANT_ID


if __name__ == "__main__":
    raise SystemExit(main())
