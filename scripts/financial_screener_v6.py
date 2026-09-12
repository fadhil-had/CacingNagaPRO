"""V6 research candidate for occasional top-three screener use.

Daily V5 recovery candles become conditional follow-through setups instead of
automatic next-open entries. Weekly keeps V5 momentum and adds only a hard
sanity guard for unusually wide planned stops. Monthly remains frozen at V5.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


_V5_PATH = Path(__file__).with_name("financial_screener_v5.py")
_SPEC = importlib.util.spec_from_file_location("financial_screener_v6_base", _V5_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load V5 screener: {_V5_PATH}")
_V5 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _V5
_SPEC.loader.exec_module(_V5)

for _name in dir(_V5):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_V5, _name)


VARIANT_ID = "V6_OCCASIONAL_TOP3_FOLLOW_THROUGH"
RANKING_MODELS = {mode: dict(weights) for mode, weights in _V5.RANKING_MODELS.items()}
BACKTEST_HOLD_GRID = {
    "daily_swing": [(5, 5)],
    "weekly_position": [(8, 40)],
    "monthly_long_term": list(_V5.BACKTEST_HOLD_GRID["monthly_long_term"]),
}
WEEKLY_MAX_STOP_DISTANCE_PCT = 25.0

_V5_ANALYZE = _V5.analisa_saham_confluence


def _daily_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    turnover20: float,
) -> dict:
    """Require a V5 recovery candle, then wait for a later high breakout."""
    base = _V5._daily_metrics(frame, benchmark, turnover20)
    recovery_confirmed = bool(base["trigger_active"])
    current = frame.iloc[-1]
    return {
        **base,
        "setup_valid": recovery_confirmed,
        "trigger_active": False,
        "trigger_reference": float(current["High"]),
        "setup_name": (
            "Daily Recovery - Wait Follow-Through"
            if recovery_confirmed else "No Valid Daily Follow-Through Setup"
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


def analisa_saham_confluence(
    ticker_code: str,
    df_saham: pd.DataFrame | None,
    ihsg_tf: pd.DataFrame,
    mode_tren: str,
    min_turnover: float,
    min_price: float,
    *,
    prepared_tf: pd.DataFrame | None = None,
    liquidity: tuple[float, float] | None = None,
) -> dict:
    candidate = _V5_ANALYZE(
        ticker_code,
        df_saham,
        ihsg_tf,
        mode_tren,
        min_turnover,
        min_price,
        prepared_tf=prepared_tf,
        liquidity=liquidity,
    )
    if candidate.get("error"):
        return candidate

    mode = normalize_timeframe(mode_tren)
    if mode == "daily_swing" and candidate.get("v4_setup_valid"):
        frame = prepared_tf
        if frame is None and df_saham is not None:
            frame = siapkan_data_untuk_timeframe(df_saham, mode)
        if frame is not None and not frame.empty:
            candidate["early_exit_rule"] = "close_below_recovery_low"
            candidate["early_exit_price"] = float(frame.iloc[-1]["Low"])

    if mode == "weekly_position":
        entry = float(candidate.get("planned_entry", np.nan))
        stop = float(candidate.get("stop_level", np.nan))
        distance = (entry - stop) / entry * 100 if entry > 0 else np.nan
        candidate["planned_stop_distance_pct"] = distance
        risk_ok = bool(np.isfinite(distance) and distance <= WEEKLY_MAX_STOP_DISTANCE_PCT)
        candidate["v6_weekly_risk_guard"] = risk_ok
        if not risk_ok:
            candidate["hard_pass"] = False
            reasons = list(candidate.get("hard_fail_reasons", []))
            reasons.append(f"jarak stop > {WEEKLY_MAX_STOP_DISTANCE_PCT:.0f}%")
            candidate["hard_fail_reasons"] = reasons
    return candidate


# Reused V4 functions resolve collaborators in the original V4 module.
_V5._V4.TIMEFRAME_METRICS = _timeframe_metrics
_V5._V4.RANKING_MODELS = RANKING_MODELS
_V5._V4.VARIANT_ID = VARIANT_ID
_V5._V4.analisa_saham_confluence = analisa_saham_confluence


if __name__ == "__main__":
    raise SystemExit(main())
