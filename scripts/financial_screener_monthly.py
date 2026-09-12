"""Frozen monthly research candidate using V4/V5 momentum and a 126-day hold."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_V5_PATH = Path(__file__).with_name("financial_screener_v5.py")
_SPEC = importlib.util.spec_from_file_location("financial_screener_monthly_base", _V5_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load V5 screener: {_V5_PATH}")
_V5 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _V5
_SPEC.loader.exec_module(_V5)

for _name in dir(_V5):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_V5, _name)


VARIANT_ID = "MONTHLY_V1_MOMENTUM_126D"
BACKTEST_HOLD_GRID = {"monthly_long_term": [(6, 126)]}
_V5_MAIN = _V5.main


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if normalize_timeframe(args.timeframe) != "monthly_long_term":
        raise ValueError(
            "financial_screener_monthly.py hanya untuk "
            "--timeframe monthly_long_term"
        )
    return _V5_MAIN(argv)


_V5._V4.VARIANT_ID = VARIANT_ID


if __name__ == "__main__":
    raise SystemExit(main())
