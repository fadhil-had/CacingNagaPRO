"""Resolve matured paper picks with the backtest's execution semantics."""
from __future__ import annotations

import argparse
import importlib.util
import json
import pickle
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


IDX_TZ = ZoneInfo("Asia/Jakarta")
ROOT = Path(__file__).resolve().parents[1]
OUTCOME_COLUMNS = [
    "outcome_status", "order_status", "trade_status", "entry_date",
    "entry_price", "exit_date", "exit_price", "exit_reason", "holding_sessions",
    "net_return_pct", "ihsg_return_pct", "net_excess_vs_ihsg_pct", "resolved_at",
]


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_execution_engine() -> ModuleType:
    return _load_module(ROOT / "tests" / "backtest_screener_v2.py", "paper_execution_engine")


def _normalize_market_data(
    engine: ModuleType,
    ihsg: pd.DataFrame,
    stocks: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    benchmark = engine.V1.normalize_daily(ihsg)
    normalized = {
        ticker: engine.V1.normalize_daily(frame)
        for ticker, frame in stocks.items()
    }
    return benchmark, {
        ticker: frame for ticker, frame in normalized.items() if not frame.empty
    }


def load_cached_market_data(
    engine: ModuleType,
    path: Path,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    with path.open("rb") as handle:
        payload = pickle.load(handle)
    ihsg, stocks = _normalize_market_data(
        engine,
        payload.get("ihsg", pd.DataFrame()),
        payload.get("stocks", {}),
    )
    if ihsg.empty or not stocks:
        raise ValueError(f"Market-data cache kosong atau tidak valid: {path}")
    return ihsg, stocks


def download_market_data(
    engine: ModuleType,
    tickers: list[str],
    period: str,
    batch_size: int,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    screener = engine.V1.load_screener(ROOT / "scripts" / "financial_screener.py")
    ihsg = screener.download_ihsg(period)
    stocks = screener.download_saham_batch(tickers, period, batch_size)
    ihsg, stocks = _normalize_market_data(engine, ihsg, stocks)
    missing = sorted(set(tickers) - set(stocks))
    if ihsg.empty:
        raise RuntimeError("Data IHSG tidak tersedia")
    if missing:
        print(f"Warning: data saham tidak tersedia: {', '.join(missing)}")
    return ihsg, stocks


def _blank_or_value(value: object) -> object:
    if value is None or value is pd.NaT:
        return ""
    if isinstance(value, pd.Timestamp):
        return str(value.date())
    if isinstance(value, float) and np.isnan(value):
        return ""
    return value


def summarize(ledger: pd.DataFrame) -> pd.DataFrame:
    completed = ledger[ledger["outcome_status"].eq("COMPLETED")].copy()
    if completed.empty:
        return pd.DataFrame(columns=[
            "model_id", "timeframe", "completed_trades", "completed_runs",
            "positive_run_rate_pct", "mean_basket_return_pct",
            "median_basket_return_pct", "mean_basket_excess_vs_ihsg_pct",
            "individual_profit_factor",
        ])
    for column in ["net_return_pct", "net_excess_vs_ihsg_pct"]:
        completed[column] = pd.to_numeric(completed[column], errors="coerce")
    rows = []
    for (model_id, timeframe), group in completed.groupby(["model_id", "timeframe"]):
        baskets = group.groupby("as_of", dropna=False).agg(
            basket_return_pct=("net_return_pct", "mean"),
            basket_excess_pct=("net_excess_vs_ihsg_pct", "mean"),
        )
        returns = group["net_return_pct"].dropna()
        wins, losses = returns[returns > 0], returns[returns < 0]
        profit_factor = (
            wins.sum() / abs(losses.sum()) if len(losses)
            else np.inf if len(wins) else np.nan
        )
        rows.append({
            "model_id": model_id,
            "timeframe": timeframe,
            "completed_trades": len(returns),
            "completed_runs": len(baskets),
            "positive_run_rate_pct": (baskets["basket_return_pct"] > 0).mean() * 100,
            "mean_basket_return_pct": baskets["basket_return_pct"].mean(),
            "median_basket_return_pct": baskets["basket_return_pct"].median(),
            "mean_basket_excess_vs_ihsg_pct": baskets["basket_excess_pct"].mean(),
            "individual_profit_factor": profit_factor,
        })
    return pd.DataFrame(rows)


def write_summary(ledger_path: Path, ledger: pd.DataFrame) -> pd.DataFrame:
    summary = summarize(ledger)
    summary.to_csv(ledger_path.with_name("summary.csv"), index=False)
    lines = [
        "# Forward Paper Summary",
        "",
        "Capital CAGR is intentionally omitted; one recorded screen is one run.",
        "",
    ]
    if summary.empty:
        lines.append("No matured completed trades yet.")
    else:
        lines.extend([
            "| Model | Timeframe | Trades | Runs | Positive runs | Mean basket | Median basket | IHSG excess | PF |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for row in summary.itertuples(index=False):
            lines.append(
                f"| {row.model_id} | {row.timeframe} | {int(row.completed_trades)} | "
                f"{int(row.completed_runs)} | {row.positive_run_rate_pct:.1f}% | "
                f"{row.mean_basket_return_pct:.3f}% | {row.median_basket_return_pct:.3f}% | "
                f"{row.mean_basket_excess_vs_ihsg_pct:.3f}% | "
                f"{row.individual_profit_factor:.3f} |"
            )
    ledger_path.with_name("paper_report.md").write_text("\n".join(lines) + "\n")
    return summary


def resolve_pending(
    ledger_path: Path,
    ihsg: pd.DataFrame,
    stocks: dict[str, pd.DataFrame],
    *,
    cutoff: str,
    engine: ModuleType | None = None,
    resolved_at: str | None = None,
) -> tuple[pd.DataFrame, int]:
    if not ledger_path.is_file():
        raise FileNotFoundError(ledger_path)
    execution = engine or load_execution_engine()
    # Object dtype permits resolved numeric values while preserving the
    # immutable source fields exactly as read from CSV.
    ledger = pd.read_csv(ledger_path, dtype=str).fillna("").astype(object)
    required = {
        "source_signal_date", "ticker", "entry_type", "trigger_price", "stop",
        "target", "entry_window", "max_holding_bars", "max_hold_days",
        "outcome_status",
    }
    missing = sorted(required - set(ledger.columns))
    if missing:
        raise ValueError(f"Ledger belum memiliki kolom resolver: {', '.join(missing)}")
    for column in OUTCOME_COLUMNS:
        if column not in ledger:
            ledger[column] = ""

    cutoff_date = pd.Timestamp(cutoff).normalize()
    benchmark = execution.V1.normalize_daily(ihsg).loc[:cutoff_date]
    normalized_stocks = {
        ticker: execution.V1.normalize_daily(frame).loc[:cutoff_date]
        for ticker, frame in stocks.items()
    }
    cfg = execution.ExecutionConfig(0.2, 0.1, "stop")
    timestamp = resolved_at or datetime.now(IDX_TZ).isoformat(timespec="seconds")
    resolved = 0

    for index, row in ledger.iterrows():
        if row["outcome_status"] != "PENDING":
            continue
        ticker = row["ticker"]
        stock = normalized_stocks.get(ticker)
        if stock is None or stock.empty:
            continue
        screen_date = pd.Timestamp(row["source_signal_date"])
        entry_type = row["entry_type"]
        entry_window = int(float(row["entry_window"]))
        needed_entry_sessions = 1 if entry_type == "active" else entry_window
        if len(benchmark.index[benchmark.index > screen_date]) < needed_entry_sessions:
            continue
        signal = {
            "screen_date": screen_date,
            "ticker": ticker,
            "entry_type": entry_type,
            "entry_window": entry_window,
            "trigger_price": float(row["trigger_price"]),
            "stop_price": float(row["stop"]),
            "target_price": float(row["target"]),
        }
        bars = int(float(row["max_holding_bars"]))
        days = int(float(row["max_hold_days"]))
        outcome = execution.simulate_signal(
            signal, stock, benchmark, bars, days, cfg,
        )
        if outcome["trade_status"] in {"INCOMPLETE_HOLD_DATA", "NO_ENTRY_SESSION"}:
            continue
        final_status = (
            "COMPLETED" if outcome["trade_status"] == "COMPLETED" else "CANCELLED"
        )
        values = {
            "outcome_status": final_status,
            "order_status": outcome["order_status"],
            "trade_status": outcome["trade_status"],
            "entry_date": outcome["entry_date"],
            "entry_price": outcome["entry_price"],
            "exit_date": outcome["exit_date"],
            "exit_price": outcome["exit_price"],
            "exit_reason": outcome["exit_reason"],
            "holding_sessions": outcome["holding_sessions"],
            "net_return_pct": outcome["net_return_pct"],
            "ihsg_return_pct": outcome["ihsg_return_pct"],
            "net_excess_vs_ihsg_pct": outcome["net_excess_vs_ihsg_pct"],
            "resolved_at": timestamp,
        }
        for column, value in values.items():
            ledger.at[index, column] = _blank_or_value(value)
        resolved += 1

    ledger.to_csv(ledger_path, index=False)
    write_summary(ledger_path, ledger)
    return ledger, resolved


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve matured forward paper picks")
    parser.add_argument("--ledger", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--market-data-cache-file", type=Path)
    source.add_argument("--download", action="store_true")
    parser.add_argument("--period", choices=["10y", "max"], default="10y")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument(
        "--cutoff", default=str(datetime.now(IDX_TZ).date()),
        help="Do not use prices after this date",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    engine = load_execution_engine()
    ledger = pd.read_csv(args.ledger, dtype=str).fillna("")
    pending_tickers = sorted(set(
        ledger.loc[ledger["outcome_status"].eq("PENDING"), "ticker"]
    ))
    if not pending_tickers:
        write_summary(args.ledger, ledger)
        print("No pending paper picks")
        return 0
    if args.market_data_cache_file:
        ihsg, stocks = load_cached_market_data(engine, args.market_data_cache_file)
    else:
        ihsg, stocks = download_market_data(
            engine, pending_tickers, args.period, args.batch_size,
        )
    _, count = resolve_pending(
        args.ledger, ihsg, stocks, cutoff=args.cutoff, engine=engine,
    )
    print(f"Resolved {count} paper picks; ledger: {args.ledger.resolve()}")
    print(f"Report: {args.ledger.with_name('paper_report.md').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
