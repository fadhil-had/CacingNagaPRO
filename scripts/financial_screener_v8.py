"""V8 research candidate: weekly momentum selection with daily entry timing.

The daily model sees only completed weekly candles. Weekly and monthly remain
the frozen V5 models; V8 changes the daily candidate family and ten-session
holding contract only.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


_V5_PATH = Path(__file__).with_name("financial_screener_v5.py")
_SPEC = importlib.util.spec_from_file_location("financial_screener_v8_base", _V5_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load V5 screener: {_V5_PATH}")
_V5 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _V5
_SPEC.loader.exec_module(_V5)

for _name in dir(_V5):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_V5, _name)


VARIANT_ID = "V8_WEEKLY_SELECTION_DAILY_TIMING"
RANKING_MODELS = {
    "daily_swing": {
        "weekly_rs_26": 0.35,
        "weekly_rs_13": 0.20,
        "weekly_risk_adjusted_26": 0.15,
        "daily_recovery_strength": 0.20,
        "daily_entry_quality": 0.10,
    },
    "weekly_position": dict(_V5.RANKING_MODELS["weekly_position"]),
    "monthly_long_term": dict(_V5.RANKING_MODELS["monthly_long_term"]),
}
BACKTEST_HOLD_GRID = {
    "daily_swing": [(10, 10)],
    "weekly_position": [(8, 40)],
    "monthly_long_term": list(_V5.BACKTEST_HOLD_GRID["monthly_long_term"]),
}

MIN_COMPLETED_WEEKS = 52
MIN_WEEKLY_HIGH52_POSITION = 0.80
MAX_WEEKLY_EXTENSION_ATR = 2.0
DAILY_PULLBACK_SESSIONS = 5
MAX_PULLBACK_ABOVE_EMA20_ATR = 0.50
MAX_DAILY_EXTENSION_ATR = 1.50
MIN_DAILY_VOLUME_RATIO = 1.00

_BASE_PREPARE = _V5.siapkan_data_untuk_timeframe


def _weekly_context(daily: pd.DataFrame) -> pd.DataFrame:
    """Map completed-week features onto daily rows without partial-week data."""
    weekly = _V5._V4._BASE.resample_timeframe(daily, "weekly_position")
    close = weekly["Close"]
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            weekly["High"] - weekly["Low"],
            (weekly["High"] - previous_close).abs(),
            (weekly["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = _V5._V4._BASE.wilder_rma(true_range, 14)
    return26 = close / close.shift(26) - 1.0
    volatility26 = close.pct_change().rolling(26).std().replace(0, np.nan)
    context = pd.DataFrame({
        "W_Close": close,
        "W_EMA20": close.ewm(span=20, adjust=False).mean(),
        "W_EMA50": close.ewm(span=50, adjust=False).mean(),
        "W_RSI": _V5._V4._BASE.hitung_rsi(close, 14),
        "W_ATR": atr,
        "W_Return13": close / close.shift(13) - 1.0,
        "W_Return26": return26,
        "W_RiskAdjusted26": return26 / volatility26,
        "W_High52_Position": close / close.rolling(52).max(),
        "W_ObservationCount": np.arange(1, len(weekly) + 1),
    }, index=weekly.index)
    return context.reindex(daily.index, method="ffill")


def siapkan_data_untuk_timeframe(
    df_saham: pd.DataFrame,
    mode_tren: str,
) -> pd.DataFrame:
    """Prepare V5 features and attach causal completed-week context to daily."""
    mode = normalize_timeframe(mode_tren)
    prepared = _BASE_PREPARE(df_saham, mode)
    if mode != "daily_swing":
        return prepared
    context = _weekly_context(prepared)
    return prepared.join(context)


def _daily_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    turnover20: float,
) -> dict:
    current, previous = frame.iloc[-1], frame.iloc[-2]
    benchmark_current = benchmark.iloc[-1]
    close = float(current["Close"])
    atr = _V5._V4._finite(current["ATR"])
    ema20 = _V5._V4._finite(current["EMA20"], close)
    weekly_rs13 = (
        _V5._V4._finite(current.get("W_Return13"))
        - _V5._V4._finite(benchmark_current.get("W_Return13"))
    )
    weekly_rs26 = (
        _V5._V4._finite(current.get("W_Return26"))
        - _V5._V4._finite(benchmark_current.get("W_Return26"))
    )
    weekly_atr = _V5._V4._finite(current.get("W_ATR"))
    weekly_close = _V5._V4._finite(current.get("W_Close"))
    weekly_ema20 = _V5._V4._finite(current.get("W_EMA20"))
    weekly_ema50 = _V5._V4._finite(current.get("W_EMA50"))
    weekly_extension = (
        (weekly_close - weekly_ema20) / weekly_atr
        if weekly_atr > 0 else np.inf
    )
    weekly_rsi = _V5._V4._finite(current.get("W_RSI"))
    weekly_high52 = _V5._V4._finite(current.get("W_High52_Position"))
    weekly_observations = _V5._V4._finite(current.get("W_ObservationCount"), 0)

    weekly_valid = bool(
        weekly_observations >= MIN_COMPLETED_WEEKS
        and weekly_close > weekly_ema20 > weekly_ema50
        and np.isfinite(weekly_rs13)
        and np.isfinite(weekly_rs26)
        and weekly_rs13 > 0
        and weekly_rs26 > 0
        and weekly_high52 >= MIN_WEEKLY_HIGH52_POSITION
        and 50 <= weekly_rsi <= 72
        and weekly_extension <= MAX_WEEKLY_EXTENSION_ATR
    )

    recent = frame.iloc[-(DAILY_PULLBACK_SESSIONS + 1):-1]
    recent_atr = recent["ATR"].replace(0, np.nan)
    distance_to_ema20 = (recent["Low"] - recent["EMA20"]) / recent_atr
    touched_ema20 = bool(
        len(recent) == DAILY_PULLBACK_SESSIONS
        and distance_to_ema20.le(MAX_PULLBACK_ABOVE_EMA20_ATR).any()
    )
    held_daily_trend = bool((recent["Close"] >= recent["EMA50"]).all())
    rsi = _V5._V4._finite(current["RSI"])
    daily_extension = (close - ema20) / atr if atr > 0 else np.inf
    setup_valid = bool(
        weekly_valid
        and touched_ema20
        and held_daily_trend
        and 45 <= rsi <= 68
        and daily_extension <= MAX_DAILY_EXTENSION_ATR
    )

    candle_range = max(_V5._V4._finite(current["Candle_Range"], 0), 1e-9)
    close_location = (close - _V5._V4._finite(current["Low"], close)) / candle_range
    volume_ma = _V5._V4._finite(current["Vol_MA20"], 0)
    volume_ratio = (
        _V5._V4._finite(current["Volume"], 0) / volume_ma
        if volume_ma > 0 else 0.0
    )
    recovered = bool(
        setup_valid
        and close > _V5._V4._finite(previous["High"], close)
        and close > _V5._V4._finite(current["EMA9"], close)
        and close > _V5._V4._finite(current["Open"], close)
        and close_location >= 0.70
        and volume_ratio >= MIN_DAILY_VOLUME_RATIO
    )
    recent_low = _V5._V4._finite(recent["Low"].min(), close)
    recovery_strength = (close - recent_low) / atr if atr > 0 else np.nan
    entry_quality = -abs(daily_extension) if np.isfinite(daily_extension) else np.nan
    return {
        "components": {
            "weekly_rs_26": weekly_rs26,
            "weekly_rs_13": weekly_rs13,
            "weekly_risk_adjusted_26": _V5._V4._finite(
                current.get("W_RiskAdjusted26")
            ),
            "daily_recovery_strength": recovery_strength,
            "daily_entry_quality": entry_quality,
        },
        "primary_rs": weekly_rs26,
        "stock_return": _V5._V4._finite(current.get("W_Return26")),
        "momentum_valid": weekly_valid,
        "setup_valid": setup_valid,
        "trigger_active": recovered,
        "trigger_reference": _V5._V4._finite(previous["High"], close),
        "setup_name": (
            "Weekly Trend - Daily Recovery Confirmed" if recovered
            else "Weekly Trend - Wait Daily Recovery" if setup_valid
            else "No Valid Weekly-to-Daily Setup"
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


# Reused V4 functions resolve these collaborators in the original V4 module.
_V5._V4.siapkan_data_untuk_timeframe = siapkan_data_untuk_timeframe
_V5._V4.TIMEFRAME_METRICS = _timeframe_metrics
_V5._V4.RANKING_MODELS = RANKING_MODELS
_V5._V4.VARIANT_ID = VARIANT_ID


if __name__ == "__main__":
    raise SystemExit(main())
