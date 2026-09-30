"""Representative deterministic fixtures — no live network calls (Phase 0, 6).

Three scenario builders over synthetic OHLCV frames:

- ``bullish_frame``  : steady uptrend into a breakout candidate
- ``conflict_frame`` : technical uptrend with distribution-style flow
- ``stale_frame``    : mostly flat/decaying series with missing flow evidence

All builders are pure functions of their seed values, so replay is identical.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 320


def _index(days: int = TRADING_DAYS) -> pd.DatetimeIndex:
    # Business days ending in the past so the completed-candle cutoff keeps all.
    return pd.bdate_range(end="2026-09-18", periods=days)


def _frame(close: np.ndarray, *, volume: np.ndarray | None = None) -> pd.DataFrame:
    index = _index(len(close))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    if volume is None:
        volume = np.full(len(close), 1_000_000.0)
    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
        },
        index=index,
    )


def bullish_frame() -> pd.DataFrame:
    """Uptrend: EMA20 > EMA50 > EMA200, price above EMA20, rising volume."""
    base = np.linspace(800.0, 1050.0, TRADING_DAYS)
    noise = np.sin(np.arange(TRADING_DAYS) / 7.0) * 6.0  # large enough for down days
    close = base + noise
    volume = np.linspace(900_000.0, 2_400_000.0, TRADING_DAYS)
    return _frame(close, volume=volume)


def conflict_frame() -> pd.DataFrame:
    """Technical-bullish / flow-distribution divergence fixture.

    Long uptrend, then a shallow ~3.5% fade over the final 20 bars: close ends
    below EMA20 (a pullback-like entry zone) while EMA20>EMA50>EMA200 keep the
    trend label bullish. Flow reads distribution because every down bar carries
    ~3.3x the volume of an up bar (MFI low, CMF negative, up/down ratio < 1).
    """
    uptrend = np.linspace(700.0, 900.0, TRADING_DAYS - 20)
    fade = 900.0 * (1.0 - 0.0012 * np.arange(1, 21))  # ~2.4% drift; EMAs stay bullish
    close = np.r_[uptrend, fade]
    steps = np.sign(np.diff(np.r_[close[0], close]))
    volume = np.where(steps < 0, 4_000_000.0, 1_200_000.0)
    return _frame(close, volume=volume)


def stale_frame() -> pd.DataFrame:
    """Low-drift sideways series; still exerciseable by all flow indicators.

    (A perfectly flat series yields zero volatility, which breaks ATR/RSI
    math downstream — real data is never perfectly flat, so neither are the
    fixtures. Missing-data behavior is tested with an empty frame instead.)
    """
    close = 420.0 + np.sin(np.arange(TRADING_DAYS) / 11.0) * 1.2 + np.linspace(0.0, 6.0, TRADING_DAYS)
    volume = np.linspace(3_600_000.0, 2_600_000.0, TRADING_DAYS)
    return _frame(close, volume=volume)


def market_frame() -> pd.DataFrame:
    """Synthetic IHSG series covering the same period."""
    base = np.linspace(6500.0, 7800.0, TRADING_DAYS)
    noise = np.sin(np.arange(TRADING_DAYS) / 5.0) * 30.0
    return _frame(base + noise)


def default_frames() -> dict[str, pd.DataFrame]:
    """Standard fixture pool: two strong candidates, one conflict, one stale."""
    return {
        "BKSL.JK": bullish_frame(),
        "ERTX.JK": bullish_frame(),
        "CUAN.JK": conflict_frame(),
        "DEWA.JK": stale_frame(),
    }
