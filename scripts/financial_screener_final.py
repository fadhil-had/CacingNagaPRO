"""Single entrypoint for the frozen weekly and monthly paper candidates."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


TARGETS = {
    "weekly": "financial_screener_weekly.py",
    "weekly_position": "financial_screener_weekly.py",
    "monthly": "financial_screener_monthly.py",
    "monthly_long_term": "financial_screener_monthly.py",
}


def target_for(timeframe: str) -> Path:
    if timeframe in {"daily", "daily_swing"}:
        raise ValueError(
            "Daily belum memiliki model tervalidasi; gunakan weekly_position "
            "atau monthly_long_term"
        )
    filename = TARGETS.get(timeframe)
    if filename is None:
        raise ValueError(f"Timeframe tidak dikenal: {timeframe}")
    return Path(__file__).with_name(filename)


def parse_timeframe(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeframe", "--trend", dest="timeframe", required=True)
    args, _ = parser.parse_known_args(argv)
    return args.timeframe


def main(argv: list[str] | None = None) -> int:
    forwarded = list(sys.argv[1:] if argv is None else argv)
    timeframe = parse_timeframe(forwarded)
    try:
        target = target_for(timeframe)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    completed = subprocess.run([sys.executable, str(target), *forwarded], check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
