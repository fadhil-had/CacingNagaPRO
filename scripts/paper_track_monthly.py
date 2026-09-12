"""Capture immutable monthly top-three recommendations for forward review."""
from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


_CORE_PATH = Path(__file__).with_name("paper_track_weekly.py")
_SPEC = importlib.util.spec_from_file_location("paper_track_monthly_core", _CORE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load paper tracker core: {_CORE_PATH}")
_CORE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CORE
_SPEC.loader.exec_module(_CORE)


IDX_TZ = ZoneInfo("Asia/Jakarta")
DEFAULT_MODEL_ID = "MONTHLY_V1_MOMENTUM_126D"
DEFAULT_LEDGER = Path("output/paper_monthly_v1/picks.csv")


def capture_snapshot(
    source: Path,
    ledger: Path = DEFAULT_LEDGER,
    *,
    as_of: str,
    model_id: str = DEFAULT_MODEL_ID,
    top: int = 3,
    recorded_at: str | None = None,
):
    return _CORE.capture_snapshot(
        source,
        ledger,
        as_of=as_of,
        model_id=model_id,
        top=top,
        recorded_at=recorded_at,
        timeframe="monthly_long_term",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Catat maksimal 3 monthly picks untuk paper forward"
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument(
        "--as-of", default=str(datetime.now(IDX_TZ).date()),
        help="Tanggal pencatatan YYYY-MM-DD (default: hari ini Jakarta)",
    )
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--top", type=int, default=3)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    picks, created = capture_snapshot(
        args.source,
        args.ledger,
        as_of=args.as_of,
        model_id=args.model_id,
        top=args.top,
    )
    action = "Recorded" if created else "Already recorded"
    print(f"{action}: {len(picks)} monthly picks in {args.ledger.resolve()}")
    if not picks.empty:
        columns = ["rank", "ticker", "status", "planned_entry", "stop", "target"]
        print(picks[columns].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
