"""Frozen weekly paper candidate: V5 momentum, Ready-only, 40-day hold.

This module deliberately returns at most three actionable candidates. It does
not fill missing slots with conditional Wait-for-Trigger setups.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_V5_PATH = Path(__file__).with_name("financial_screener_v5.py")
_SPEC = importlib.util.spec_from_file_location("financial_screener_weekly_base", _V5_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load V5 screener: {_V5_PATH}")
_V5 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _V5
_SPEC.loader.exec_module(_V5)

for _name in dir(_V5):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_V5, _name)


VARIANT_ID = "WEEKLY_V1_READY_ONLY_40D"
BACKTEST_HOLD_GRID = {
    "weekly_position": [(8, 40)],
}

_V5_RANKING_CANDIDATES = _V5.ranking_candidates
_V5_MAIN = _V5.main


def ranking_candidates(candidates: list[dict], limit: int = 3):
    """Return actionable weekly candidates only; never pad with Wait setups."""
    ready = [
        candidate for candidate in candidates
        if candidate.get("status") == STATUS_READY
    ]
    return _V5_RANKING_CANDIDATES(ready, limit)


# V4's reused CLI resolves these collaborators in the original V4 module.
_V5._V4.ranking_candidates = ranking_candidates
_V5._V4.VARIANT_ID = VARIANT_ID


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if normalize_timeframe(args.timeframe) != "weekly_position":
        raise ValueError(
            "financial_screener_weekly.py hanya untuk --timeframe weekly_position"
        )
    return _V5_MAIN(argv)


if __name__ == "__main__":
    raise SystemExit(main())
