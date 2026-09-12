"""V3-A research variant: rank without score credit for mandatory gates.

This module deliberately reuses the V1/V2 indicator and execution inputs.  It
changes only the number used to rank eligible candidates: volatility and setup
remain eligibility rules, but contribute no rank points because they are
already mandatory for a Ready-to-Enter candidate.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


_BASE_PATH = Path(__file__).with_name("financial_screener.py")
_SPEC = importlib.util.spec_from_file_location("financial_screener_v3_base", _BASE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load baseline screener: {_BASE_PATH}")
_BASE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BASE
_SPEC.loader.exec_module(_BASE)

# Export the baseline API unchanged except for finalisation below.
for _name in dir(_BASE):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_BASE, _name)


VARIANT_ID = "V3-A_RANK_EXCLUDES_MANDATORY_GATES"
RANKING_WEIGHTS = {
    "trend": 20,
    "relative_strength": 20,
    "momentum": 10,
    "macd": 10,
    "volume": 15,
    "setup": 0,
    "volatility": 0,
}


def finalisasi_score_dan_status(candidates: list[dict], mode_tren: str, market_regime: dict):
    """Preserve V2 eligibility/status; replace only the rank score.

    ``eligibility_score`` reproduces the baseline score and therefore preserves
    the Ready/Wait population. ``quality_score`` is the ranking-only score
    exposed to ``ranking_candidates``.
    """
    mode_tren = normalize_timeframe(mode_tren)
    valid_rs = [c["rs_excess"] for c in candidates if np.isfinite(c.get("rs_excess", np.nan))]
    if valid_rs:
        rs_series = pd.Series(valid_rs)
        for candidate in candidates:
            rs = candidate.get("rs_excess", np.nan)
            candidate["rs_percentile"] = float((rs_series <= rs).mean() * 100) if np.isfinite(rs) else 0.0
    else:
        for candidate in candidates:
            candidate["rs_percentile"] = 0.0

    base_threshold = get_ready_score(mode_tren)
    regime = market_regime["regime"]
    for candidate in candidates:
        rs_ok = (
            candidate["rs_excess"] > 0
            and candidate["rs_trend_up"]
            and candidate["rs_percentile"] >= 60
        )
        candidate["conditions"]["relative_strength"] = bool(rs_ok)
        eligibility_score = sum(
            FACTOR_WEIGHTS[name] * int(bool(candidate["conditions"][name]))
            for name in FACTOR_WEIGHTS
        )
        rank_score = sum(
            RANKING_WEIGHTS[name] * int(bool(candidate["conditions"][name]))
            for name in RANKING_WEIGHTS
        )
        candidate["eligibility_score"] = float(eligibility_score)
        candidate["quality_score"] = float(rank_score)

        if regime == "BULLISH":
            required_score, min_rs_pct = base_threshold, 60
        elif regime == "NEUTRAL":
            required_score, min_rs_pct = min(base_threshold + 5, 90), 70
        else:
            required_score, min_rs_pct = 101, 80
        ready = (
            candidate["hard_pass"]
            and candidate["conditions"]["setup"]
            and eligibility_score >= required_score
            and candidate["rs_percentile"] >= min_rs_pct
            and regime != "BEARISH"
        )
        if ready:
            candidate["status"] = STATUS_READY
        elif candidate["hard_pass"] and eligibility_score >= 50:
            candidate["status"] = STATUS_WAIT
        else:
            candidate["status"] = tentukan_status_skip(candidate.get("hard_fail_reasons", []))
        candidate["kondisi_detail"] = ", ".join(
            f"{'✅' if candidate['conditions'][name] else '❌'} {name}"
            for name in FACTOR_WEIGHTS
        )
    return candidates
