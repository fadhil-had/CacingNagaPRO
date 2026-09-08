#!/usr/bin/env python3
"""Point-in-time backtester for the frozen IDX screener V1.

The script deliberately imports and reuses the V1 screener instead of copying
its indicator logic. This keeps V1 frozen and makes the output suitable for
deciding which indicators or thresholds should be changed in V2.

Full V1 evaluation for V2 research:
    python tests/backtest_screener_v1.py \
        --screener-file scripts/financial_screener.py \
        --excel resource/daftar-saham.xlsx \
        --timeframe all \
        --start 2016-01-01 \
        --holdout-start 2024-01-01 \
        --end 2026-08-31

The screener file may also have a .txt extension.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import pickle
import sys
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
import numpy as np
import pandas as pd


TIMEFRAME_HOLDS = {
    "daily_swing": [(5, 5), (10, 10), (15, 15)],
    "weekly_position": [(4, 20), (8, 40), (12, 60)],
    "monthly_long_term": [(3, 63), (6, 126), (9, 189)],
}

BACKTEST_ENGINE_VERSION = "2.1.0"

REQUIRED_SCREENER_API = (
    "ambil_semua_ticker_dari_excel",
    "download_ihsg",
    "download_saham_batch",
    "hitung_market_breadth_from_frames",
    "resample_timeframe",
    "siapkan_data_untuk_timeframe",
    "analisa_market_regime",
    "analisa_saham_confluence",
    "finalisasi_score_dan_status",
    "ranking_candidates",
)

FACTOR_NAMES = (
    "trend",
    "relative_strength",
    "momentum",
    "macd",
    "volume",
    "setup",
    "volatility",
)


@dataclass(frozen=True)
class RunConfig:
    start: pd.Timestamp
    end: pd.Timestamp
    holdout_start: pd.Timestamp
    min_turnover: float
    min_price: float
    top: int
    min_selection_trades: int


def load_screener(path: Path) -> ModuleType:
    """Load a Python source file even when its extension is .txt."""
    if not path.is_file():
        raise FileNotFoundError(f"Screener file not found: {path}")

    loader = SourceFileLoader("idx_screener_v1", str(path.resolve()))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise ImportError(f"Unable to create an import spec for {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    try:
        loader.exec_module(module)
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"Unable to load the screener because dependency '{exc.name}' is missing. "
            "Install the screener dependencies first, for example: "
            "pip install numpy pandas openpyxl yfinance"
        ) from exc

    missing = [name for name in REQUIRED_SCREENER_API if not hasattr(module, name)]
    if missing:
        raise AttributeError(
            "The screener is missing required functions: " + ", ".join(missing)
        )
    return module


def normalize_daily(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.dropna(subset=["Open", "High", "Low", "Close"])


def normalize_tickers(raw: str) -> list[str]:
    tickers = []
    for value in raw.split(","):
        ticker = value.strip().upper()
        if not ticker:
            continue
        tickers.append(ticker if ticker.endswith(".JK") else f"{ticker}.JK")
    return sorted(set(tickers))


def cache_path_for(
    cache_dir: Path,
    pool: list[str],
    period: str,
    requested_end: pd.Timestamp,
) -> Path:
    identity = "|".join([period, str(requested_end.date()), *sorted(pool)])
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"market_data_{digest}.pkl"


def load_market_data(
    screener: ModuleType,
    pool: list[str],
    period: str,
    batch_size: int,
    cache_dir: Path,
    requested_end: pd.Timestamp,
    use_cache: bool,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], Path]:
    """Load raw OHLCV once; the cache only contains downloaded market data."""
    path = cache_path_for(cache_dir, pool, period, requested_end)
    if use_cache and path.is_file():
        with path.open("rb") as handle:
            payload = pickle.load(handle)
        print(f"Using market-data cache: {path}")
        ihsg = normalize_daily(payload["ihsg"])
        stocks = {
            ticker: normalize_daily(frame)
            for ticker, frame in payload["stocks"].items()
        }
        return ihsg, stocks, path

    print(f"Downloading IHSG and {len(pool):,} stock histories...")
    ihsg = normalize_daily(screener.download_ihsg(period))
    stocks = {
        ticker: normalize_daily(frame)
        for ticker, frame in screener.download_saham_batch(
            pool, period, batch_size
        ).items()
    }
    stocks = {ticker: frame for ticker, frame in stocks.items() if not frame.empty}

    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            pickle.dump({"ihsg": ihsg, "stocks": stocks}, handle, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"Saved market-data cache: {path}")
    return ihsg, stocks, path


def normalize_status(screener: ModuleType, value: str) -> str:
    aliases = getattr(screener, "STATUS_ALIAS", {})
    return aliases.get(value, value)


def ready_status(screener: ModuleType) -> str:
    return getattr(screener, "STATUS_READY", "Ready to Enter")


def wait_status(screener: ModuleType) -> str:
    return getattr(screener, "STATUS_WAIT", "Wait for Trigger")


def candidate_sort_key(candidate: dict) -> tuple:
    """Match the screener's ranking_candidates ordering."""
    return (
        float(candidate.get("quality_score", 0) or 0),
        float(candidate.get("rs_percentile", 0) or 0),
        int(bool(candidate.get("breakout_ok", False))),
        float(candidate.get("vol_z", 0) or 0),
        float(candidate.get("turnover20", 0) or 0),
    )


def evaluation_dates(
    screener: ModuleType,
    ihsg: pd.DataFrame,
    timeframe: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[pd.Timestamp]:
    """Return only dates on which the screener sees a new completed TF candle."""
    market_dates = ihsg.index[(ihsg.index >= start) & (ihsg.index <= end)]
    selected: list[pd.Timestamp] = []
    last_label = None

    for date in market_dates:
        try:
            raw_tf = screener.resample_timeframe(ihsg.loc[:date], timeframe)
            if raw_tf.empty:
                continue
            current_label = pd.Timestamp(raw_tf.index[-1])
        except Exception:
            continue
        if current_label.normalize() < start.normalize():
            last_label = current_label
            continue
        if current_label != last_label:
            selected.append(pd.Timestamp(date))
            last_label = current_label
    return selected


def rank_candidates(screener: ModuleType, candidates: list[dict]) -> list[dict]:
    accepted = (ready_status(screener), wait_status(screener))
    ranked: list[dict] = []

    for status in accepted:
        group = [
            candidate
            for candidate in candidates
            if normalize_status(screener, candidate.get("status", "")) == status
        ]
        if group:
            group, _ = screener.ranking_candidates(group, limit=len(group))
        for rank, candidate in enumerate(group, start=1):
            copy = dict(candidate)
            copy["normalized_status"] = status
            copy["status_rank"] = rank
            ranked.append(copy)
    return ranked


def screen_snapshot(
    screener: ModuleType,
    pool: list[str],
    stock_frames: dict[str, pd.DataFrame],
    ihsg: pd.DataFrame,
    screen_date: pd.Timestamp,
    timeframe: str,
    config: RunConfig,
    executor: Executor | None = None,
) -> tuple[list[dict], dict]:
    ihsg_snapshot = ihsg.loc[:screen_date]
    frame_snapshots = {
        ticker: frame.loc[:screen_date]
        for ticker, frame in stock_frames.items()
        if not frame.loc[:screen_date].empty
    }

    breadth = screener.hitung_market_breadth_from_frames(frame_snapshots, pool)
    ihsg_tf = screener.siapkan_data_untuk_timeframe(ihsg_snapshot, timeframe)
    regime = screener.analisa_market_regime(ihsg_snapshot, timeframe, breadth)

    def analyze(ticker: str) -> dict | None:
        stock_snapshot = frame_snapshots.get(ticker)
        if stock_snapshot is None or stock_snapshot.empty:
            return None
        result = screener.analisa_saham_confluence(
            ticker,
            stock_snapshot,
            ihsg_tf,
            timeframe,
            config.min_turnover,
            config.min_price,
        )
        return None if result.get("error") else result

    analyzed = executor.map(analyze, pool) if executor is not None else map(analyze, pool)
    candidates = [result for result in analyzed if result is not None]

    candidates = screener.finalisasi_score_dan_status(candidates, timeframe, regime)
    return rank_candidates(screener, candidates), regime


def signal_record(
    candidate: dict,
    regime: dict,
    timeframe: str,
    screen_date: pd.Timestamp,
    top: int,
) -> dict:
    conditions = candidate.get("conditions", {})
    record = {
        "signal_id": f"{timeframe}|{screen_date.date()}|{candidate['ticker']}",
        "timeframe": timeframe,
        "screen_date": screen_date,
        "signal_bar_date": candidate.get("date"),
        "ticker": candidate["ticker"],
        "status": candidate["normalized_status"],
        "status_rank": int(candidate["status_rank"]),
        "is_top_pick": int(candidate["status_rank"]) <= top,
        "quality_score": float(candidate.get("quality_score", np.nan)),
        "rs_percentile": float(candidate.get("rs_percentile", np.nan)),
        "rs_excess": float(candidate.get("rs_excess", np.nan)),
        "setup": candidate.get("setup_name", ""),
        "market_regime": regime.get("regime", ""),
        "market_regime_score": regime.get("score", np.nan),
        "signal_close": float(candidate.get("harga_terakhir", np.nan)),
        "planned_entry": float(candidate.get("planned_entry", candidate.get("entry_level", np.nan))),
        "entry_type": candidate.get("entry_type", ""),
        "trigger_price": float(candidate.get("trigger_price", candidate.get("entry_level", np.nan))),
        "stop_price": float(candidate.get("stop_level", np.nan)),
        "target_price": float(candidate.get("target_price", np.nan)),
        "rsi": float(candidate.get("rsi", np.nan)),
        "atr_pct": float(candidate.get("atr_pct", np.nan)),
        "volume_ratio": float(candidate.get("vol_ratio", np.nan)),
        "volume_z": float(candidate.get("vol_z", np.nan)),
        "turnover20": float(candidate.get("turnover20", np.nan)),
    }
    for factor in FACTOR_NAMES:
        record[f"factor_{factor}"] = bool(conditions.get(factor, False))
    return record


def next_index_date(frame: pd.DataFrame, after: pd.Timestamp) -> pd.Timestamp | None:
    dates = frame.index[frame.index > after]
    return pd.Timestamp(dates[0]) if len(dates) else None


def ihsg_return(
    ihsg: pd.DataFrame, entry_date: pd.Timestamp, exit_date: pd.Timestamp
) -> float:
    if entry_date not in ihsg.index:
        return np.nan
    exit_rows = ihsg.loc[:exit_date]
    if exit_rows.empty:
        return np.nan
    start = float(ihsg.loc[entry_date, "Open"])
    finish = float(exit_rows.iloc[-1]["Close"])
    if not np.isfinite(start) or start <= 0 or not np.isfinite(finish):
        return np.nan
    return (finish / start - 1.0) * 100.0


def incomplete_outcome(signal: dict, max_bars: int, max_days: int, reason: str) -> dict:
    return {
        **signal,
        "max_hold_bars": max_bars,
        "max_hold_days": max_days,
        "trade_status": reason,
        "entry_date": pd.NaT,
        "entry_price": np.nan,
        "exit_date": pd.NaT,
        "exit_price": np.nan,
        "exit_reason": reason,
        "holding_sessions": np.nan,
        "raw_return_pct": np.nan,
        "r_multiple": np.nan,
        "ihsg_return_pct": np.nan,
        "excess_vs_ihsg_pct": np.nan,
    }


def simulate_signal(
    signal: dict,
    stock: pd.DataFrame,
    ihsg: pd.DataFrame,
    max_bars: int,
    max_days: int,
) -> dict:
    screen_date = pd.Timestamp(signal["screen_date"])
    entry_date = next_index_date(ihsg, screen_date)
    if entry_date is None:
        return incomplete_outcome(signal, max_bars, max_days, "NO_ENTRY_DATA")
    if entry_date not in stock.index or float(stock.loc[entry_date].get("Volume", 1)) <= 0:
        return incomplete_outcome(signal, max_bars, max_days, "NO_NEXT_SESSION_ENTRY")

    entry_price = float(stock.loc[entry_date, "Open"])
    stop = float(signal["stop_price"])
    target = float(signal["target_price"])
    if not all(np.isfinite(value) for value in (entry_price, stop, target)):
        return incomplete_outcome(signal, max_bars, max_days, "INVALID_PRICE_LEVEL")
    if entry_price <= stop:
        result = incomplete_outcome(signal, max_bars, max_days, "ENTRY_BELOW_STOP")
        result.update({"entry_date": entry_date, "entry_price": entry_price})
        return result
    if entry_price >= target:
        result = incomplete_outcome(signal, max_bars, max_days, "ENTRY_ABOVE_TARGET")
        result.update({"entry_date": entry_date, "entry_price": entry_price})
        return result

    path = stock.loc[entry_date:].head(max_days)
    if path.empty:
        return incomplete_outcome(signal, max_bars, max_days, "NO_HOLD_DATA")

    exit_date = None
    exit_price = np.nan
    exit_reason = ""
    holding_sessions = 0

    for holding_sessions, (date, candle) in enumerate(path.iterrows(), start=1):
        open_price = float(candle["Open"])
        high = float(candle["High"])
        low = float(candle["Low"])

        if open_price <= stop:
            exit_date, exit_price, exit_reason = date, open_price, "STOP_GAP"
            break
        if open_price >= target:
            exit_date, exit_price, exit_reason = date, open_price, "TARGET_GAP"
            break

        stop_hit = low <= stop
        target_hit = high >= target
        if stop_hit and target_hit:
            exit_date, exit_price, exit_reason = date, stop, "STOP_SAME_CANDLE"
            break
        if stop_hit:
            exit_date, exit_price, exit_reason = date, stop, "STOP"
            break
        if target_hit:
            exit_date, exit_price, exit_reason = date, target, "TARGET"
            break

    if exit_date is None:
        if len(path) < max_days:
            result = incomplete_outcome(signal, max_bars, max_days, "INCOMPLETE_HOLD_DATA")
            result.update({"entry_date": entry_date, "entry_price": entry_price})
            return result
        exit_date = path.index[-1]
        exit_price = float(path.iloc[-1]["Close"])
        exit_reason = "MAX_HOLD"
        holding_sessions = max_days

    raw_return = (exit_price / entry_price - 1.0) * 100.0
    risk = entry_price - stop
    r_multiple = (exit_price - entry_price) / risk if risk > 0 else np.nan
    benchmark = ihsg_return(ihsg, entry_date, pd.Timestamp(exit_date))

    return {
        **signal,
        "max_hold_bars": max_bars,
        "max_hold_days": max_days,
        "trade_status": "COMPLETED",
        "entry_date": entry_date,
        "entry_price": entry_price,
        "exit_date": pd.Timestamp(exit_date),
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "holding_sessions": holding_sessions,
        "raw_return_pct": raw_return,
        "r_multiple": r_multiple,
        "ihsg_return_pct": benchmark,
        "excess_vs_ihsg_pct": raw_return - benchmark if np.isfinite(benchmark) else np.nan,
    }


def performance_stats(frame: pd.DataFrame) -> dict:
    eligible = frame.get("eligible_for_comparison", pd.Series(True, index=frame.index)).astype(bool)
    completed = frame[(frame["trade_status"] == "COMPLETED") & eligible].copy()
    returns = completed["raw_return_pct"].dropna()
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    count = len(returns)
    if count:
        win_fraction = float((returns > 0).mean())
        z = 1.96
        denominator = 1.0 + z * z / count
        center = (win_fraction + z * z / (2 * count)) / denominator
        margin = (
            z
            * math.sqrt(
                win_fraction * (1 - win_fraction) / count
                + z * z / (4 * count * count)
            )
            / denominator
        )
        win_ci_low, win_ci_high = (center - margin) * 100, (center + margin) * 100
        return_std = float(returns.std(ddof=1)) if count > 1 else np.nan
        return_margin = 1.96 * return_std / math.sqrt(count) if count > 1 else np.nan
        mean_return = float(returns.mean())
    else:
        win_ci_low = win_ci_high = return_std = return_margin = mean_return = np.nan
    return {
        "signal_count": int(len(frame)),
        "boundary_excluded": int((~eligible).sum()),
        "completed_trades": int(len(returns)),
        "incomplete_or_invalid": int(len(frame) - len(returns)),
        "win_rate_pct": float((returns > 0).mean() * 100) if len(returns) else np.nan,
        "win_rate_ci95_low_pct": win_ci_low,
        "win_rate_ci95_high_pct": win_ci_high,
        "average_return_pct": mean_return,
        "average_return_ci95_low_pct": mean_return - return_margin if np.isfinite(return_margin) else np.nan,
        "average_return_ci95_high_pct": mean_return + return_margin if np.isfinite(return_margin) else np.nan,
        "return_std_pct": return_std,
        "median_return_pct": float(returns.median()) if len(returns) else np.nan,
        "average_win_pct": float(wins.mean()) if len(wins) else np.nan,
        "average_loss_pct": float(losses.mean()) if len(losses) else np.nan,
        "expectancy_pct": float(returns.mean()) if len(returns) else np.nan,
        "profit_factor": float(gross_profit / gross_loss) if gross_loss > 0 else np.nan,
        "average_r_multiple": float(completed["r_multiple"].mean()) if len(completed) else np.nan,
        "average_holding_sessions": float(completed["holding_sessions"].mean()) if len(completed) else np.nan,
        "average_ihsg_return_pct": float(completed["ihsg_return_pct"].mean()) if len(completed) else np.nan,
        "average_excess_vs_ihsg_pct": float(completed["excess_vs_ihsg_pct"].mean()) if len(completed) else np.nan,
        "outperform_ihsg_rate_pct": float((completed["excess_vs_ihsg_pct"] > 0).mean() * 100) if len(completed) else np.nan,
        "stop_exit_pct": float(completed["exit_reason"].str.startswith("STOP").mean() * 100) if len(completed) else np.nan,
        "target_exit_pct": float(completed["exit_reason"].str.startswith("TARGET").mean() * 100) if len(completed) else np.nan,
        "max_hold_exit_pct": float((completed["exit_reason"] == "MAX_HOLD").mean() * 100) if len(completed) else np.nan,
    }


def grouped_stats(frame: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
    rows = []
    for keys, group in frame.groupby(group_columns, dropna=False, observed=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = dict(zip(group_columns, keys))
        row.update(performance_stats(group))
        rows.append(row)
    return pd.DataFrame(rows)


def create_breakdown(trades: pd.DataFrame) -> pd.DataFrame:
    work = trades.copy()
    work["rank_bucket"] = np.where(work["status_rank"] <= 3, work["status_rank"].astype(str), "4+")
    work["score_bucket"] = pd.cut(
        work["quality_score"],
        bins=[-np.inf, 49.999, 59.999, 69.999, 79.999, 89.999, np.inf],
        labels=["<50", "50-59", "60-69", "70-79", "80-89", "90+"],
    )

    dimensions = ["rank_bucket", "score_bucket", "setup", "market_regime"]
    rows: list[pd.DataFrame] = []
    # Keep status in every breakdown so factor/setup comparisons are not
    # accidentally driven only by the difference between Ready and Wait.
    base = ["timeframe", "max_hold_bars", "max_hold_days", "sample_split", "status"]
    for dimension in dimensions:
        stats = grouped_stats(work, base + [dimension])
        stats = stats.rename(columns={dimension: "dimension_value"})
        stats.insert(len(base), "dimension", dimension)
        rows.append(stats)

    for factor in FACTOR_NAMES:
        column = f"factor_{factor}"
        stats = grouped_stats(work, base + [column])
        stats = stats.rename(columns={column: "dimension_value"})
        stats.insert(len(base), "dimension", column)
        rows.append(stats)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def select_best_holds(summary: pd.DataFrame, config: RunConfig, ready: str) -> dict[str, tuple[int, int]]:
    selection = summary[
        (summary["sample_split"] == "SELECTION")
        & (summary["status"] == ready)
        & (summary["completed_trades"] >= config.min_selection_trades)
    ].copy()
    best: dict[str, tuple[int, int]] = {}
    for timeframe, group in selection.groupby("timeframe"):
        ranked = group.sort_values(
            ["expectancy_pct", "profit_factor", "completed_trades"],
            ascending=[False, False, False],
        )
        if not ranked.empty:
            row = ranked.iloc[0]
            best[timeframe] = (int(row["max_hold_bars"]), int(row["max_hold_days"]))
    return best


def prepare_portfolio_signals(
    trades: pd.DataFrame,
    timeframe: str,
    max_bars: int,
    ready: str,
    top: int,
    sample_split: str,
) -> list[dict]:
    subset = trades[
        (trades["timeframe"] == timeframe)
        & (trades["max_hold_bars"] == max_bars)
        & (trades["status"] == ready)
        & (trades["status_rank"] <= top)
        & (trades["sample_split"] == sample_split)
    ].copy()
    subset = subset.sort_values(["screen_date", "status_rank"])
    return subset.drop_duplicates("signal_id").to_dict("records")


def simulate_portfolio(
    signals: list[dict],
    stock_frames: dict[str, pd.DataFrame],
    market_dates: pd.DatetimeIndex,
    max_hold_days: int,
    initial_capital: float,
    max_positions: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    market_dates = pd.DatetimeIndex(market_dates).sort_values()
    if market_dates.empty:
        return pd.DataFrame(), pd.DataFrame()

    scheduled: dict[pd.Timestamp, list[dict]] = {}
    for signal in signals:
        stock = stock_frames.get(signal["ticker"], pd.DataFrame())
        next_dates = market_dates[market_dates > pd.Timestamp(signal["screen_date"])]
        entry_date = next_dates[0] if len(next_dates) else None
        if entry_date is not None and market_dates[0] <= entry_date <= market_dates[-1]:
            scheduled.setdefault(entry_date, []).append(signal)

    cash = float(initial_capital)
    active: dict[str, dict] = {}
    closed: list[dict] = []
    curve: list[dict] = []
    last_prices: dict[str, float] = {}

    for date in market_dates:
        # Existing positions that gap through a level exit before new entries.
        for ticker in list(active):
            position = active[ticker]
            stock = stock_frames[ticker]
            if date not in stock.index:
                continue
            candle = stock.loc[date]
            open_price = float(candle["Open"])
            reason = None
            if open_price <= position["stop_price"]:
                reason = "STOP_GAP"
            elif open_price >= position["target_price"]:
                reason = "TARGET_GAP"
            if reason:
                proceeds = position["shares"] * open_price
                cash += proceeds
                closed.append({
                    **position,
                    "exit_date": date,
                    "exit_price": open_price,
                    "exit_reason": reason,
                    "raw_return_pct": (open_price / position["entry_price"] - 1) * 100,
                    "pnl": proceeds - position["allocation"],
                })
                del active[ticker]

        # New positions enter at today's open, subject to available slots and cash.
        for signal in sorted(scheduled.get(date, []), key=lambda row: row["status_rank"]):
            ticker = signal["ticker"]
            if ticker in active or len(active) >= max_positions or cash <= 0:
                continue
            stock = stock_frames[ticker]
            if date not in stock.index:
                continue
            entry_price = float(stock.loc[date, "Open"])
            if float(stock.loc[date].get("Volume", 1)) <= 0:
                continue
            stop = float(signal["stop_price"])
            target = float(signal["target_price"])
            if not all(np.isfinite(v) and v > 0 for v in (entry_price, stop, target)):
                continue
            if entry_price <= stop or entry_price >= target:
                continue

            marked_open = cash
            for open_ticker, position in active.items():
                other = stock_frames[open_ticker]
                price = float(other.loc[date, "Open"]) if date in other.index else last_prices.get(open_ticker, position["entry_price"])
                marked_open += position["shares"] * price
            allocation = min(cash, marked_open / max_positions)
            if allocation <= 0:
                continue
            shares = allocation / entry_price
            cash -= allocation
            active[ticker] = {
                "signal_id": signal["signal_id"],
                "timeframe": signal["timeframe"],
                "sample_split": signal["sample_split"],
                "screen_date": pd.Timestamp(signal["screen_date"]),
                "ticker": ticker,
                "status_rank": int(signal["status_rank"]),
                "quality_score": float(signal["quality_score"]),
                "setup": signal["setup"],
                "market_regime": signal["market_regime"],
                "entry_date": date,
                "entry_price": entry_price,
                "stop_price": stop,
                "target_price": target,
                "allocation": allocation,
                "shares": shares,
                "bars_held": 0,
            }

        # Intraday exits and time stops, including positions opened today.
        for ticker in list(active):
            position = active[ticker]
            stock = stock_frames[ticker]
            if date not in stock.index:
                continue
            candle = stock.loc[date]
            position["bars_held"] += 1
            open_price = float(candle["Open"])
            high = float(candle["High"])
            low = float(candle["Low"])
            close = float(candle["Close"])
            last_prices[ticker] = close

            # Gap exits for old positions were already handled above. A newly
            # opened position can still hit an intraday stop or target.
            stop_hit = low <= position["stop_price"] and open_price > position["stop_price"]
            target_hit = high >= position["target_price"] and open_price < position["target_price"]
            exit_price = None
            reason = None
            if stop_hit and target_hit:
                exit_price, reason = position["stop_price"], "STOP_SAME_CANDLE"
            elif stop_hit:
                exit_price, reason = position["stop_price"], "STOP"
            elif target_hit:
                exit_price, reason = position["target_price"], "TARGET"
            elif position["bars_held"] >= max_hold_days:
                exit_price, reason = close, "MAX_HOLD"

            if reason:
                proceeds = position["shares"] * exit_price
                cash += proceeds
                closed.append({
                    **position,
                    "exit_date": date,
                    "exit_price": exit_price,
                    "exit_reason": reason,
                    "raw_return_pct": (exit_price / position["entry_price"] - 1) * 100,
                    "pnl": proceeds - position["allocation"],
                })
                del active[ticker]

        equity = cash
        for ticker, position in active.items():
            stock = stock_frames[ticker]
            price = float(stock.loc[date, "Close"]) if date in stock.index else last_prices.get(ticker, position["entry_price"])
            last_prices[ticker] = price
            equity += position["shares"] * price
        curve.append({"date": date, "equity": equity, "cash": cash, "active_positions": len(active)})

    curve_df = pd.DataFrame(curve)
    if not curve_df.empty:
        curve_df["equity_peak"] = curve_df["equity"].cummax().clip(lower=initial_capital)
        curve_df["drawdown_pct"] = (curve_df["equity"] / curve_df["equity_peak"] - 1) * 100
    return pd.DataFrame(closed), curve_df


def period_ihsg_return(ihsg: pd.DataFrame, dates: pd.DatetimeIndex) -> float:
    available = ihsg.loc[ihsg.index.intersection(dates)]
    if available.empty:
        return np.nan
    first_open = float(available.iloc[0]["Open"])
    last_close = float(available.iloc[-1]["Close"])
    if first_open <= 0 or not np.isfinite(first_open) or not np.isfinite(last_close):
        return np.nan
    return (last_close / first_open - 1.0) * 100.0


def portfolio_statistics(
    timeframe: str,
    sample_split: str,
    max_bars: int,
    max_days: int,
    initial_capital: float,
    closed: pd.DataFrame,
    curve: pd.DataFrame,
    ihsg: pd.DataFrame,
) -> dict:
    if curve.empty:
        return {}
    final_equity = float(curve.iloc[-1]["equity"])
    total_return = (final_equity / initial_capital - 1.0) * 100.0
    benchmark = period_ihsg_return(ihsg, pd.DatetimeIndex(curve["date"]))
    first_date = pd.Timestamp(curve.iloc[0]["date"])
    last_date = pd.Timestamp(curve.iloc[-1]["date"])
    years = max((last_date - first_date).days / 365.25, 1.0 / 365.25)
    cagr = ((final_equity / initial_capital) ** (1.0 / years) - 1.0) * 100.0
    ihsg_cagr = (
        ((1.0 + benchmark / 100.0) ** (1.0 / years) - 1.0) * 100.0
        if np.isfinite(benchmark) and benchmark > -100
        else np.nan
    )
    return {
        "timeframe": timeframe,
        "sample_split": sample_split,
        "max_hold_bars": max_bars,
        "max_hold_days": max_days,
        "start_equity": initial_capital,
        "final_equity": final_equity,
        "total_return_pct": total_return,
        "cagr_pct": cagr,
        "ihsg_return_pct": benchmark,
        "ihsg_cagr_pct": ihsg_cagr,
        "excess_vs_ihsg_pct": total_return - benchmark if np.isfinite(benchmark) else np.nan,
        "cagr_excess_vs_ihsg_pct": cagr - ihsg_cagr if np.isfinite(ihsg_cagr) else np.nan,
        "max_drawdown_pct": float(curve["drawdown_pct"].min()),
        "closed_trades": int(len(closed)),
        "win_rate_pct": float((closed["pnl"] > 0).mean() * 100) if len(closed) else np.nan,
        "profit_factor": (
            float(closed.loc[closed.pnl > 0, "pnl"].sum() / -closed.loc[closed.pnl < 0, "pnl"].sum())
            if len(closed) and (closed.pnl < 0).any()
            else (np.inf if len(closed) and (closed.pnl > 0).any() else np.nan)
        ),
        "open_positions_at_end": int(curve.iloc[-1]["active_positions"]),
    }


def write_analysis(
    path: Path,
    trades: pd.DataFrame,
    summary: pd.DataFrame,
    best_holds: dict[str, tuple[int, int]],
    portfolio_summary: pd.DataFrame,
    ready: str,
    config: RunConfig,
    screen_error_count: int,
) -> None:
    lines = [
        "# V1 Screener Backtest Analysis",
        "",
        "## Scope",
        "",
        "- Results are raw price returns; fees and extra slippage are excluded.",
        "- V1 indicators and thresholds were not changed.",
        "- Entry uses the next daily open after a completed signal candle.",
        "- Daily, weekly, and monthly snapshots follow actual completed candles; no fixed-day sampling is used.",
        "- Current-universe data may contain survivorship bias.",
        "- Signal comparisons use a common date cohort with room for the longest holding horizon in each split.",
        "- Incomplete stock histories are excluded from completed-trade statistics; inspect boundary_excluded and incomplete_or_invalid counts.",
        "- Portfolio decisions never filter on future trade outcomes; open positions remain marked to market at the endpoint.",
        "- Wait entries are counterfactual next-open purchases, not executions of the screener's planned triggers.",
        "- Prices inherit the screener downloader's adjustments; raw means before fees, not unadjusted historical prices.",
        "- Confidence intervals assume independent trades; overlapping ticker/date outcomes make them descriptive, not validation evidence.",
        "- Best holds are selection candidates, not validated strategies; a negative best candidate can still be simulated for diagnosis.",
        "- Portfolio uses fractional shares, equal equity per slot, no same-ticker overlap, and separate starting cash per split.",
        "- Selection outcomes are capped before the holdout boundary, so V2 choices do not use holdout prices.",
        "",
        "## Sample",
        "",
        f"- Selection period: {config.start.date()} to {(config.holdout_start - pd.Timedelta(days=1)).date()}",
        f"- Holdout period: {config.holdout_start.date()} to {config.end.date()}",
        f"- Generated signal outcomes: {len(trades):,}",
        f"- Completed signal outcomes: {(trades['trade_status'] == 'COMPLETED').sum():,}",
        f"- Snapshot-level errors skipped: {screen_error_count:,}",
        "",
        "## Selected Maximum Holding Periods",
        "",
        "| Timeframe | Bars | Trading sessions | Selection expectancy | Holdout expectancy | Holdout profit factor |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    for timeframe, (bars, days) in best_holds.items():
        selected = summary[
            (summary["timeframe"] == timeframe)
            & (summary["max_hold_bars"] == bars)
            & (summary["status"] == ready)
        ]
        selection = selected[selected["sample_split"] == "SELECTION"]
        holdout = selected[selected["sample_split"] == "HOLDOUT"]
        selection_exp = selection.iloc[0]["expectancy_pct"] if not selection.empty else np.nan
        holdout_exp = holdout.iloc[0]["expectancy_pct"] if not holdout.empty else np.nan
        holdout_pf = holdout.iloc[0]["profit_factor"] if not holdout.empty else np.nan
        lines.append(
            f"| {timeframe} | {bars} | {days} | {selection_exp:.3f}% | {holdout_exp:.3f}% | {holdout_pf:.3f} |"
        )

    if not best_holds:
        lines.append("| No timeframe met the minimum selection-trade requirement | - | - | - | - | - |")

    lines.extend(["", "## Portfolio Results", ""])
    if portfolio_summary.empty:
        lines.append("No portfolio result was produced because no timeframe had enough selection trades.")
    else:
        lines.append("| Timeframe | Sample | Hold bars | Final equity | CAGR | IHSG CAGR | CAGR excess | Max drawdown | Closed | Open |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for _, row in portfolio_summary.iterrows():
            lines.append(
                f"| {row['timeframe']} | {row['sample_split']} | {int(row['max_hold_bars'])} | "
                f"{row['final_equity']:.2f} | {row['cagr_pct']:.2f}% | "
                f"{row['ihsg_cagr_pct']:.2f}% | {row['cagr_excess_vs_ihsg_pct']:.2f}% | "
                f"{row['max_drawdown_pct']:.2f}% | {int(row['closed_trades'])} | "
                f"{int(row['open_positions_at_end'])} |"
            )
        lines.extend(["", "### Top-pick closed-trade metrics", "",
                      "| Timeframe | Sample | Win rate | Profit factor |",
                      "|---|---|---:|---:|"])
        for _, row in portfolio_summary.iterrows():
            lines.append(f"| {row['timeframe']} | {row['sample_split']} | {row['win_rate_pct']:.2f}% | {row['profit_factor']:.3f} |")

    lines.extend([
        "",
        "## V2 Review Checklist",
        "",
        "Use `backtest_breakdown.csv` to review:",
        "",
        "1. Whether Ready-to-Enter signals outperform Wait-for-Trigger candidates.",
        "2. Whether higher scores and ranks produce higher expectancy.",
        "3. Which setup and market regime combinations have negative expectancy.",
        "4. Which factor passes fail to improve outcomes and may need new weights or thresholds.",
        "5. Whether holdout performance confirms or rejects the selection-period result.",
        "",
        "Do not tune V2 on the holdout sample. Preserve this result and use a new validation window for V2.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_self_test() -> None:
    dates = pd.date_range("2025-01-02", periods=4, freq="B")
    base_signal = {
        "signal_id": "test",
        "timeframe": "daily_swing",
        "screen_date": dates[0] - pd.Timedelta(days=1),
        "signal_bar_date": dates[0] - pd.Timedelta(days=1),
        "ticker": "TEST.JK",
        "status": "Ready to Enter",
        "status_rank": 1,
        "is_top_pick": True,
        "quality_score": 80.0,
        "rs_percentile": 80.0,
        "rs_excess": 0.1,
        "setup": "Synthetic",
        "market_regime": "BULLISH",
        "market_regime_score": 5,
        "signal_close": 100.0,
        "planned_entry": 100.0,
        "entry_type": "active",
        "trigger_price": 100.0,
        "stop_price": 95.0,
        "target_price": 110.0,
        "rsi": 60.0,
        "atr_pct": 0.02,
        "volume_ratio": 1.5,
        "volume_z": 1.2,
        "turnover20": 1e9,
        **{f"factor_{factor}": True for factor in FACTOR_NAMES},
    }
    ihsg = pd.DataFrame({"Open": [100] * 4, "High": [101] * 4, "Low": [99] * 4, "Close": [100] * 4}, index=dates)

    target_stock = pd.DataFrame({"Open": [100] * 4, "High": [111, 101, 101, 101], "Low": [99] * 4, "Close": [105] * 4}, index=dates)
    target_result = simulate_signal(base_signal, target_stock, ihsg, 4, 4)
    assert target_result["exit_reason"] == "TARGET"
    assert math.isclose(target_result["raw_return_pct"], 10.0)

    same_bar = target_stock.copy()
    same_bar.loc[dates[0], ["High", "Low"]] = [111, 94]
    stop_result = simulate_signal(base_signal, same_bar, ihsg, 4, 4)
    assert stop_result["exit_reason"] == "STOP_SAME_CANDLE"

    time_stock = pd.DataFrame({"Open": [100] * 4, "High": [101] * 4, "Low": [99] * 4, "Close": [100, 101, 102, 103]}, index=dates)
    time_result = simulate_signal(base_signal, time_stock, ihsg, 4, 4)
    assert time_result["exit_reason"] == "MAX_HOLD"
    assert time_result["holding_sessions"] == 4

    portfolio_signal = {
        **base_signal,
        "sample_split": "SELECTION",
    }
    closed, curve = simulate_portfolio(
        [portfolio_signal],
        {"TEST.JK": target_stock},
        dates,
        max_hold_days=4,
        initial_capital=100_000.0,
        max_positions=3,
    )
    assert len(closed) == 1
    assert closed.iloc[0]["exit_reason"] == "TARGET"
    assert not curve.empty and "drawdown_pct" in curve
    # A first-day loss must be measured against initial cash, not itself.
    _, loss_curve = simulate_portfolio([portfolio_signal], {"TEST.JK": same_bar}, dates, 4, 100_000, 3)
    assert loss_curve.iloc[0]["drawdown_pct"] < 0
    # An unresolved candidate still gets a portfolio slot and is marked to market.
    unresolved = {**portfolio_signal, "max_hold_bars": 4,
                  "trade_status": "INCOMPLETE_HOLD_DATA", "eligible_for_comparison": False}
    picked = prepare_portfolio_signals(pd.DataFrame([unresolved]), "daily_swing", 4,
                                      "Ready to Enter", 3, "SELECTION")
    assert len(picked) == 1
    open_trades, open_curve = simulate_portfolio(picked, {"TEST.JK": time_stock}, dates[:2], 4, 100_000, 3)
    assert open_trades.empty and open_curve.iloc[-1].active_positions == 1
    # Missing next-session prices must not defer an order until an arbitrary later date.
    missing = simulate_signal(base_signal, time_stock.iloc[1:], ihsg, 4, 4)
    assert missing["trade_status"] == "NO_NEXT_SESSION_ENTRY"
    truncated = simulate_signal(base_signal, time_stock.iloc[:2], ihsg.iloc[:2], 4, 4)
    assert truncated["trade_status"] == "INCOMPLETE_HOLD_DATA"
    # First-day loss / no position / all-cash accounting.
    empty_closed, flat = simulate_portfolio([], {}, dates, 4, 100_000, 3)
    assert empty_closed.empty and (flat.equity == 100_000).all()
    stats = portfolio_statistics("daily_swing", "SELECTION", 4, 4, 100_000, closed, curve, ihsg)
    assert stats["win_rate_pct"] == 100 and np.isinf(stats["profit_factor"])
    print(
        "Self-test passed: target, conservative same-candle stop, max-hold, "
        "and top-position portfolio accounting rules."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Point-in-time backtester for the IDX stock screener V1")
    parser.add_argument("--screener-file", default="scripts/financial_screener.py", help="Path to the existing V1 screener (.py or .txt)")
    parser.add_argument("--excel", default="resource/daftar-saham.xlsx", help="Ticker universe Excel file used by the screener")
    parser.add_argument("--tickers", default="", help="Optional comma-separated universe, for example BBCA,BBRI,TLKM")
    parser.add_argument("--timeframe", "--trend", dest="timeframe", choices=["daily_swing", "weekly_position", "monthly_long_term", "all"], default="all")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    parser.add_argument("--holdout-start", default="2024-01-01")
    parser.add_argument("--period", choices=["10y", "max"], default="max")
    parser.add_argument("--min-turnover", type=float, default=1_000_000_000)
    parser.add_argument("--min-price", type=float, default=100)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-tickers", "--ticker-limit", dest="max_tickers", type=int, default=0, help="Optional universe limit for smoke tests; 0 uses the full universe")
    parser.add_argument("--workers", type=int, default=2, help="Parallel stock analyses per snapshot; use 1 for sequential execution")
    parser.add_argument("--use-cache", action="store_true", help="Reuse/save downloaded OHLCV data for this universe and end date")
    parser.add_argument("--cache-dir", default="output/backtest_cache")
    parser.add_argument("--min-selection-trades", type=int, default=30)
    parser.add_argument("--initial-capital", type=float, default=100_000_000)
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--output-dir", default="output/backtest_v1_corrected")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0

    screener = load_screener(Path(args.screener_file))
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    holdout_start = pd.Timestamp(args.holdout_start)
    if not start < holdout_start <= end:
        raise ValueError("Expected start < holdout-start <= end")
    if args.top < 1 or args.max_positions < 1 or args.workers < 1:
        raise ValueError("top, max-positions, and workers must be positive")
    if args.initial_capital <= 0 or args.batch_size < 1 or args.min_selection_trades < 1:
        raise ValueError("initial-capital, batch-size, and min-selection-trades must be positive")

    config = RunConfig(
        start=start,
        end=end,
        holdout_start=holdout_start,
        min_turnover=args.min_turnover,
        min_price=args.min_price,
        top=args.top,
        min_selection_trades=args.min_selection_trades,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pool = normalize_tickers(args.tickers) if args.tickers else screener.ambil_semua_ticker_dari_excel(args.excel)
    pool = sorted(set(pool))
    if args.max_tickers > 0:
        pool = pool[: args.max_tickers]
    if not pool:
        raise RuntimeError("The ticker universe is empty")
    requested_universe_size = len(pool)

    ihsg, stock_frames, cache_path = load_market_data(
        screener,
        pool,
        args.period,
        args.batch_size,
        Path(args.cache_dir),
        end,
        args.use_cache,
    )
    pool = [ticker for ticker in pool if ticker in stock_frames]
    if ihsg.empty or not stock_frames:
        raise RuntimeError("Historical IHSG or stock data is unavailable")

    timeframes = list(TIMEFRAME_HOLDS) if args.timeframe == "all" else [args.timeframe]
    all_signals: list[dict] = []
    screen_errors: list[dict] = []
    snapshot_counts: dict[str, dict[str, int]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        active_executor = executor if args.workers > 1 else None
        for timeframe in timeframes:
            dates = evaluation_dates(screener, ihsg, timeframe, start, end)
            successful_snapshots = 0
            print(f"{timeframe}: evaluating {len(dates)} completed-candle snapshots")
            timeframe_signal_count = 0
            for number, screen_date in enumerate(dates, start=1):
                try:
                    candidates, regime = screen_snapshot(
                        screener,
                        pool,
                        stock_frames,
                        ihsg,
                        screen_date,
                        timeframe,
                        config,
                        active_executor,
                    )
                except Exception as exc:
                    screen_errors.append({
                        "timeframe": timeframe,
                        "screen_date": screen_date,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    })
                    print(f"Warning: skipped {timeframe} {screen_date.date()}: {exc}")
                    continue
                successful_snapshots += 1
                records = [
                    signal_record(candidate, regime, timeframe, screen_date, args.top)
                    for candidate in candidates
                ]
                all_signals.extend(records)
                timeframe_signal_count += len(records)
                if number % 25 == 0 or number == len(dates):
                    print(
                        f"  processed {number}/{len(dates)} snapshots; "
                        f"retained {timeframe_signal_count:,} eligible signals"
                    )
            snapshot_counts[timeframe] = {
                "scheduled": len(dates),
                "successful": successful_snapshots,
                "failed": len(dates) - successful_snapshots,
            }

    if not all_signals:
        raise RuntimeError("No Ready-to-Enter or Wait-for-Trigger signals were produced")

    signals = pd.DataFrame(all_signals)
    signals["screen_date"] = pd.to_datetime(signals["screen_date"])
    signals["sample_split"] = np.where(signals["screen_date"] < holdout_start, "SELECTION", "HOLDOUT")
    signals.to_csv(output_dir / "backtest_signals.csv", index=False)
    pd.DataFrame(
        screen_errors,
        columns=["timeframe", "screen_date", "error_type", "error_message"],
    ).to_csv(output_dir / "screen_errors.csv", index=False)

    outcome_rows: list[dict] = []
    selection_end = holdout_start - pd.Timedelta(nanoseconds=1)
    for signal in signals.to_dict("records"):
        sample_end = selection_end if signal["sample_split"] == "SELECTION" else end
        stock = stock_frames[signal["ticker"]].loc[:sample_end]
        benchmark_window = ihsg.loc[:sample_end]
        # Same signal-date cohort for all hold alternatives, determined only by
        # the calendar cutoff; do not retain near-boundary early winners/losers
        # while removing unresolved trades from that same cohort.
        longest_hold = max(days for _, days in TIMEFRAME_HOLDS[signal["timeframe"]])
        remaining_sessions = int((benchmark_window.index > signal["screen_date"]).sum())
        signal["eligible_for_comparison"] = remaining_sessions >= longest_hold
        for max_bars, max_days in TIMEFRAME_HOLDS[signal["timeframe"]]:
            outcome_rows.append(
                simulate_signal(signal, stock, benchmark_window, max_bars, max_days)
            )
    trades = pd.DataFrame(outcome_rows)
    trades.to_csv(output_dir / "backtest_trades.csv", index=False)

    summary_groups = ["timeframe", "max_hold_bars", "max_hold_days", "sample_split", "status"]
    summary = grouped_stats(trades, summary_groups)
    summary.to_csv(output_dir / "backtest_summary.csv", index=False)

    breakdown = create_breakdown(trades)
    breakdown.to_csv(output_dir / "backtest_breakdown.csv", index=False)

    ready = ready_status(screener)
    best_holds = select_best_holds(summary, config, ready)
    best_hold_rows = [
        {"timeframe": timeframe, "max_hold_bars": bars, "max_hold_days": days,
         "decision": "BEST_CANDIDATE_NOT_VALIDATED"}
        for timeframe, (bars, days) in best_holds.items()
    ]
    pd.DataFrame(
        best_hold_rows,
        columns=["timeframe", "max_hold_bars", "max_hold_days", "decision"],
    ).to_csv(output_dir / "best_holds.csv", index=False)
    portfolio_summaries = []
    portfolio_trades_all = []
    portfolio_curves_all = []
    for timeframe, (max_bars, max_days) in best_holds.items():
        for sample_split, period_start, period_end in (
            ("SELECTION", start, selection_end),
            ("HOLDOUT", holdout_start, end),
        ):
            portfolio_signals = prepare_portfolio_signals(
                trades,
                timeframe,
                max_bars,
                ready,
                args.top,
                sample_split,
            )
            market_dates = ihsg.index[
                (ihsg.index >= period_start) & (ihsg.index <= period_end)
            ]
            closed, curve = simulate_portfolio(
                portfolio_signals,
                stock_frames,
                market_dates,
                max_days,
                args.initial_capital,
                args.max_positions,
            )
            if not closed.empty:
                closed["max_hold_bars"] = max_bars
                closed["max_hold_days"] = max_days
                closed["sample_split"] = sample_split
                portfolio_trades_all.append(closed)
            if not curve.empty:
                curve["timeframe"] = timeframe
                curve["max_hold_bars"] = max_bars
                curve["max_hold_days"] = max_days
                curve["sample_split"] = sample_split
                portfolio_curves_all.append(curve)
                stats = portfolio_statistics(
                    timeframe,
                    sample_split,
                    max_bars,
                    max_days,
                    args.initial_capital,
                    closed,
                    curve,
                    ihsg,
                )
                if stats:
                    portfolio_summaries.append(stats)

    portfolio_trades = pd.concat(portfolio_trades_all, ignore_index=True) if portfolio_trades_all else pd.DataFrame()
    portfolio_curve = pd.concat(portfolio_curves_all, ignore_index=True) if portfolio_curves_all else pd.DataFrame()
    portfolio_summary = pd.DataFrame(portfolio_summaries)
    portfolio_trades.to_csv(output_dir / "portfolio_trades.csv", index=False)
    portfolio_curve.to_csv(output_dir / "portfolio_equity_curve.csv", index=False)
    portfolio_summary.to_csv(output_dir / "portfolio_summary.csv", index=False)

    metadata = {
        "backtest_engine_version": BACKTEST_ENGINE_VERSION,
        "screener_sha256": hashlib.sha256(Path(args.screener_file).read_bytes()).hexdigest(),
        "entry_rule": "next_market_open_research_baseline_not_planned_trigger_execution",
        "portfolio_uses_future_outcome_filter": False,
        "signal_comparison_cohort": "common_longest_hold_calendar_cutoff_per_split",
        "screener_file": str(Path(args.screener_file)),
        "timeframes": timeframes,
        "start": str(start.date()),
        "holdout_start": str(holdout_start.date()),
        "end": str(end.date()),
        "period": args.period,
        "universe_size_requested": requested_universe_size,
        "universe_size_with_data": len(pool),
        "top": args.top,
        "max_positions": args.max_positions,
        "workers": args.workers,
        "raw_price_only": True,
        "market_data_cache": str(cache_path) if args.use_cache else None,
        "holding_grids": TIMEFRAME_HOLDS,
        "snapshot_counts": snapshot_counts,
        "screen_error_count": len(screen_errors),
    }
    (output_dir / "run_metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    write_analysis(
        output_dir / "backtest_analysis.md",
        trades,
        summary,
        best_holds,
        portfolio_summary,
        ready,
        config,
        len(screen_errors),
    )

    print(f"Backtest completed. Results: {output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Backtest interrupted by user", file=sys.stderr)
        raise SystemExit(130)
