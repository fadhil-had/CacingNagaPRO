"""Capture immutable weekly V5 top-three recommendations for forward review."""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


IDX_TZ = ZoneInfo("Asia/Jakarta")
ACCEPTED_STATUSES = {"Ready to Enter"}
DEFAULT_MODEL_ID = "WEEKLY_V1_READY_ONLY_40D"
DEFAULT_LEDGER = Path("output/paper_weekly_v5/picks.csv")

PICK_COLUMNS = [
    "as_of", "model_id", "timeframe", "source_signal_date", "ticker", "rank",
    "status", "close", "entry_type", "planned_entry", "trigger_price", "stop",
    "target", "entry_window", "max_holding_bars", "max_hold_days", "setup",
    "outcome_status", "order_status", "trade_status", "entry_date",
    "entry_price", "exit_date", "exit_price", "exit_reason", "holding_sessions",
    "net_return_pct", "ihsg_return_pct", "net_excess_vs_ihsg_pct", "resolved_at",
    "source_sha256", "recorded_at",
]
RUN_COLUMNS = [
    "as_of", "model_id", "timeframe", "source_signal_date", "pick_count",
    "source_sha256", "recorded_at",
]


def _read_or_empty(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.is_file() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    frame = pd.read_csv(path, dtype=str)
    for column in columns:
        if column not in frame:
            frame[column] = ""
    return frame[columns]


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _truthy(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().isin({"true", "1", "yes"})


def _validate_source(frame: pd.DataFrame, expected_timeframe: str) -> None:
    required = {
        "ticker", "date", "timeframe", "status", "universe_rank", "close",
        "entry_type", "planned_entry", "trigger_price", "stop", "target",
        "entry_window", "max_holding_bars", "max_hold_days", "setup", "hard_pass",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Kolom source tidak lengkap: {', '.join(missing)}")
    timeframes = set(frame["timeframe"].dropna().astype(str))
    if timeframes != {expected_timeframe}:
        raise ValueError(
            f"Paper tracker mengharapkan timeframe {expected_timeframe}"
        )


def _top_picks(frame: pd.DataFrame, top: int) -> pd.DataFrame:
    rank = pd.to_numeric(frame["universe_rank"], errors="coerce")
    valid = frame[
        frame["status"].isin(ACCEPTED_STATUSES)
        & _truthy(frame["hard_pass"])
        & rank.notna()
    ].copy()
    valid["_rank"] = pd.to_numeric(valid["universe_rank"], errors="coerce")
    return valid.sort_values(["_rank", "ticker"]).head(top)


def capture_snapshot(
    source: Path,
    ledger: Path = DEFAULT_LEDGER,
    *,
    as_of: str,
    model_id: str = DEFAULT_MODEL_ID,
    top: int = 3,
    recorded_at: str | None = None,
    timeframe: str = "weekly_position",
) -> tuple[pd.DataFrame, bool]:
    """Append one auditable run; return its picks and whether it was new."""
    if top < 1:
        raise ValueError("top harus positif")
    if not source.is_file():
        raise FileNotFoundError(source)

    as_of_date = pd.Timestamp(as_of).normalize()
    if pd.isna(as_of_date):
        raise ValueError("as_of tidak valid")
    frame = pd.read_csv(source)
    _validate_source(frame, timeframe)
    source_dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    if source_dates.empty:
        raise ValueError("Source tidak memiliki tanggal sinyal yang valid")
    source_signal_date = source_dates.max().normalize()
    if source_signal_date > as_of_date:
        raise ValueError("Tanggal sinyal tidak boleh lebih baru daripada as_of")

    digest = _source_hash(source)
    runs_path = ledger.with_name("runs.csv")
    runs = _read_or_empty(runs_path, RUN_COLUMNS)
    same_run = runs[
        runs["as_of"].eq(str(as_of_date.date()))
        & runs["model_id"].eq(model_id)
    ]
    if not same_run.empty:
        if not same_run["source_sha256"].eq(digest).all():
            raise RuntimeError(
                "Tanggal/model ini sudah direkam dengan source berbeda; "
                "paper record tidak boleh ditimpa"
            )
        picks = _read_or_empty(ledger, PICK_COLUMNS)
        existing = picks[
            picks["as_of"].eq(str(as_of_date.date()))
            & picks["model_id"].eq(model_id)
        ]
        return existing.copy(), False

    selected = _top_picks(frame, top)
    timestamp = recorded_at or datetime.now(IDX_TZ).isoformat(timespec="seconds")
    rows = []
    for _, candidate in selected.iterrows():
        rows.append({
            "as_of": str(as_of_date.date()),
            "model_id": model_id,
            "timeframe": timeframe,
            "source_signal_date": str(source_signal_date.date()),
            "ticker": str(candidate["ticker"]),
            "rank": int(candidate["_rank"]),
            "status": str(candidate["status"]),
            "close": candidate.get("close", ""),
            "entry_type": candidate.get("entry_type", ""),
            "planned_entry": candidate.get("planned_entry", ""),
            "trigger_price": candidate.get("trigger_price", ""),
            "stop": candidate.get("stop", ""),
            "target": candidate.get("target", ""),
            "entry_window": candidate.get("entry_window", ""),
            "max_holding_bars": candidate.get("max_holding_bars", ""),
            "max_hold_days": candidate.get("max_hold_days", ""),
            "setup": candidate.get("setup", ""),
            "outcome_status": "PENDING",
            "order_status": "",
            "trade_status": "",
            "entry_date": "",
            "entry_price": "",
            "exit_date": "",
            "exit_price": "",
            "exit_reason": "",
            "holding_sessions": "",
            "net_return_pct": "",
            "ihsg_return_pct": "",
            "net_excess_vs_ihsg_pct": "",
            "resolved_at": "",
            "source_sha256": digest,
            "recorded_at": timestamp,
        })
    new_picks = pd.DataFrame(rows, columns=PICK_COLUMNS)
    existing_picks = _read_or_empty(ledger, PICK_COLUMNS)
    updated_picks = pd.concat([existing_picks, new_picks], ignore_index=True)
    run = pd.DataFrame([{
        "as_of": str(as_of_date.date()),
        "model_id": model_id,
        "timeframe": timeframe,
        "source_signal_date": str(source_signal_date.date()),
        "pick_count": len(new_picks),
        "source_sha256": digest,
        "recorded_at": timestamp,
    }], columns=RUN_COLUMNS)
    updated_runs = pd.concat([runs, run], ignore_index=True)

    ledger.parent.mkdir(parents=True, exist_ok=True)
    snapshot_dir = ledger.parent / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    safe_model = "".join(char if char.isalnum() else "_" for char in model_id)
    snapshot_path = snapshot_dir / f"{as_of_date.date()}_{safe_model}.csv"
    new_picks.to_csv(snapshot_path, index=False)
    updated_picks.to_csv(ledger, index=False)
    updated_runs.to_csv(runs_path, index=False)
    return new_picks, True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Catat maksimal 3 weekly Ready picks untuk paper forward"
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
    print(f"{action}: {len(picks)} weekly picks in {args.ledger.resolve()}")
    if not picks.empty:
        print(picks[["rank", "ticker", "status", "planned_entry", "stop", "target"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
