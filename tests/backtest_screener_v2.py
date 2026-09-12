#!/usr/bin/env python3
"""Execution-valid V2.0 backtest for the frozen IDX screener V1 signals.

V2.0 deliberately does not change indicator rules.  It corrects execution:
planned setups trade only after their trigger, all fills include costs, and
cancelled orders remain visible in the audit output.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd


ENGINE_VERSION = "2.7.0"
V1_PATH = Path(__file__).with_name("backtest_screener_v1.py")
HOLD_SELECTION_MIN_TRADES = {
    "daily_swing": 100,
    "weekly_position": 60,
    "monthly_long_term": 30,
}
HOLD_SELECTION_MIN_PF = 1.20
HOLD_SELECTION_MAX_DRAWDOWN_PCT = 30.0
PORTFOLIO_SUMMARY_COLUMNS = [
    "timeframe", "sample_split", "max_hold_bars", "max_hold_days", "start_equity",
    "final_equity", "total_return_pct", "cagr_pct", "ihsg_return_pct",
    "ihsg_cagr_pct", "cagr_excess_vs_ihsg_pct", "max_drawdown_pct",
    "closed_trades", "win_rate_pct", "profit_factor", "forced_end_exits",
    "open_positions_at_end",
]
WALK_FORWARD_COLUMNS = [
    "fold", "fit_start", "fit_end", "validation_start", "validation_end",
    *PORTFOLIO_SUMMARY_COLUMNS, "eligible_candidates", "skipped_candidates",
]
READY_SETUP_FAMILIES = {
    "all": frozenset(),
    "breakout": frozenset({"Breakout Confirmed"}),
    "pullback": frozenset({"Bullish Pullback/Reclaim"}),
    "candlestick": frozenset({"Bullish Engulfing", "Hammer", "Marubozu Bullish"}),
}
UNIVERSE_AUDIT_COLUMNS = [
    "ticker", "in_current_excel", "in_membership_manifest", "membership_intervals",
    "member_at_backtest_start", "member_at_holdout_start", "member_at_backtest_end",
    "has_market_data", "first_observed_date", "last_observed_date", "observed_sessions",
    "benchmark_sessions_while_observed", "coverage_pct_while_observed",
    "max_consecutive_missing_sessions", "observed_after_backtest_start",
    "observed_before_backtest_end", "audit_status",
]
RECOMMENDATION_SUMMARY_COLUMNS = [
    "timeframe", "max_hold_bars", "max_hold_days", "sample_split",
    "recommendation_policy", "recommendation_runs", "order_count",
    "average_picks_per_run", "triggered_orders", "trigger_rate_pct",
    "executed_runs", "positive_basket_run_rate_pct", "mean_basket_return_pct",
    "median_basket_return_pct", "mean_basket_excess_vs_ihsg_pct",
    "individual_win_rate_pct", "individual_net_expectancy_pct",
    "individual_median_return_pct", "individual_profit_factor",
]
CADENCE_SUMMARY_COLUMNS = [
    "timeframe", "max_hold_bars", "max_hold_days", "sample_split", "cadence",
    "scheduled_runs", "recommendation_runs", "runs_with_triggered_orders",
    "order_count", "triggered_orders", "trigger_rate_pct",
    "positive_basket_run_rate_pct", "mean_basket_return_pct",
    "median_basket_return_pct", "mean_basket_excess_vs_ihsg_pct",
]


def load_v1_runner() -> ModuleType:
    """Reuse V1's point-in-time screen formation, never its execution logic."""
    spec = importlib.util.spec_from_file_location("backtest_screener_v1_base", V1_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load V1 runner: {V1_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


V1 = load_v1_runner()
HOLD_GRID = V1.TIMEFRAME_HOLDS


@dataclass(frozen=True)
class ExecutionConfig:
    fee_pct: float
    slippage_pct: float
    same_bar_policy: str
    lot_size: int = 100
    risk_per_trade_pct: float = 0.0
    max_position_pct: float = 100.0


def buy_fill(price: float, cfg: ExecutionConfig) -> float:
    return price * (1 + cfg.slippage_pct / 100)


def sell_fill(price: float, cfg: ExecutionConfig) -> float:
    return price * (1 - cfg.slippage_pct / 100)


def side_cost(price: float, shares: int, cfg: ExecutionConfig) -> float:
    return price * shares * cfg.fee_pct / 100


def empty_outcome(signal: dict, bars: int, days: int, status: str, **values: object) -> dict:
    return {
        **signal,
        "max_hold_bars": bars,
        "max_hold_days": days,
        "order_status": status,
        "trade_status": status,
        "entry_date": pd.NaT,
        "entry_price": np.nan,
        "exit_date": pd.NaT,
        "exit_price": np.nan,
        "exit_reason": status,
        "holding_sessions": np.nan,
        "gross_return_pct": np.nan,
        "net_return_pct": np.nan,
        "entry_fee": np.nan,
        "exit_fee": np.nan,
        "total_cost": np.nan,
        "ihsg_return_pct": np.nan,
        "net_excess_vs_ihsg_pct": np.nan,
        **values,
    }


def eligible_sessions(ihsg: pd.DataFrame, screen_date: pd.Timestamp, window: int) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(ihsg.index[ihsg.index > screen_date][:window])


def find_entry(signal: dict, stock: pd.DataFrame, ihsg: pd.DataFrame, cfg: ExecutionConfig) -> dict:
    screen_date = pd.Timestamp(signal["screen_date"])
    stop, target = float(signal["stop_price"]), float(signal["target_price"])
    entry_type = str(signal.get("entry_type", ""))
    window = int(signal.get("entry_window", 1) or 1)
    sessions = eligible_sessions(ihsg, screen_date, 1 if entry_type == "active" else window)
    if not len(sessions):
        return {"status": "NO_ENTRY_SESSION"}

    for date in sessions:
        if date not in stock.index or float(stock.loc[date].get("Volume", 0)) <= 0:
            return {"status": "MISSING_ENTRY_SESSION"}
        candle = stock.loc[date]
        opening, high, low = (float(candle[key]) for key in ("Open", "High", "Low"))
        if entry_type == "active":
            base = opening
            fill = buy_fill(base, cfg)
            if fill <= stop:
                return {"status": "ENTRY_BELOW_STOP"}
            if fill >= target:
                return {"status": "ENTRY_ABOVE_TARGET"}
            return {"status": "TRIGGERED", "entry_date": date, "entry_price": fill, "entry_base_price": base}

        trigger = float(signal["trigger_price"])
        if opening <= stop:
            return {"status": "PRE_TRIGGER_STOP_GAP"}
        if opening >= target:
            return {"status": "PRE_TRIGGER_TARGET_GAP"}
        if low <= stop and high < trigger:
            return {"status": "PRE_TRIGGER_STOP"}
        if high >= trigger:
            base = max(opening, trigger)
            fill = buy_fill(base, cfg)
            if fill <= stop:
                return {"status": "ENTRY_BELOW_STOP"}
            if fill >= target:
                return {"status": "ENTRY_ABOVE_TARGET"}
            return {"status": "TRIGGERED", "entry_date": date, "entry_price": fill, "entry_base_price": base}
    return {"status": "NO_TRIGGER"}


def simulate_signal(signal: dict, stock: pd.DataFrame, ihsg: pd.DataFrame, bars: int, days: int, cfg: ExecutionConfig) -> dict:
    entry = find_entry(signal, stock, ihsg, cfg)
    if entry["status"] != "TRIGGERED":
        return empty_outcome(signal, bars, days, entry["status"])

    entry_date, entry_price = pd.Timestamp(entry["entry_date"]), float(entry["entry_price"])
    stop, target = float(signal["stop_price"]), float(signal["target_price"])
    early_exit = float(signal.get("early_exit_price", np.nan))
    path = stock.loc[entry_date:].head(days)
    if len(path) < days:
        return empty_outcome(signal, bars, days, "INCOMPLETE_HOLD_DATA", entry_date=entry_date, entry_price=entry_price)

    exit_date = path.index[-1]
    exit_base = float(path.iloc[-1]["Close"])
    reason = "MAX_HOLD"
    holding_sessions = days
    for holding_sessions, (date, candle) in enumerate(path.iterrows(), start=1):
        opening, high, low = (float(candle[key]) for key in ("Open", "High", "Low"))
        if opening <= stop:
            exit_date, exit_base, reason = date, opening, "STOP_GAP"
            break
        if opening >= target:
            exit_date, exit_base, reason = date, opening, "TARGET_GAP"
            break
        stop_hit, target_hit = low <= stop, high >= target
        if stop_hit and target_hit:
            exit_date, exit_base, reason = date, (stop if cfg.same_bar_policy == "stop" else target), f"{cfg.same_bar_policy.upper()}_SAME_BAR"
            break
        if stop_hit:
            exit_date, exit_base, reason = date, stop, "STOP"
            break
        if target_hit:
            exit_date, exit_base, reason = date, target, "TARGET"
            break
        if np.isfinite(early_exit) and float(candle["Close"]) < early_exit:
            exit_date, exit_base, reason = date, float(candle["Close"]), "EARLY_INVALIDATION"
            break

    shares = cfg.lot_size
    exit_price = sell_fill(exit_base, cfg)
    entry_fee, exit_fee = side_cost(entry_price, shares, cfg), side_cost(exit_price, shares, cfg)
    gross_return = (exit_price / entry_price - 1) * 100
    net_cost = entry_price * shares + entry_fee
    net_proceeds = exit_price * shares - exit_fee
    net_return = (net_proceeds / net_cost - 1) * 100
    benchmark = V1.ihsg_return(ihsg, entry_date, pd.Timestamp(exit_date))
    return {
        **signal, "max_hold_bars": bars, "max_hold_days": days,
        "order_status": "TRIGGERED", "trade_status": "COMPLETED",
        "entry_date": entry_date, "entry_price": entry_price,
        "exit_date": pd.Timestamp(exit_date), "exit_price": exit_price,
        "exit_reason": reason, "holding_sessions": holding_sessions,
        "gross_return_pct": gross_return, "net_return_pct": net_return,
        "entry_fee": entry_fee, "exit_fee": exit_fee, "total_cost": entry_fee + exit_fee,
        "ihsg_return_pct": benchmark,
        "net_excess_vs_ihsg_pct": net_return - benchmark if np.isfinite(benchmark) else np.nan,
    }


def stats(frame: pd.DataFrame) -> dict:
    completed = frame[frame.trade_status.eq("COMPLETED")].copy()
    values = completed.net_return_pct.dropna()
    wins, losses = values[values > 0], values[values < 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) else (np.inf if len(wins) else np.nan)
    return {
        "order_count": len(frame), "triggered_orders": int(frame.order_status.eq("TRIGGERED").sum()),
        "trigger_rate_pct": float(frame.order_status.eq("TRIGGERED").mean() * 100) if len(frame) else np.nan,
        "completed_trades": len(values), "cancelled_or_invalid": int(len(frame) - len(values)),
        "win_rate_pct": float((values > 0).mean() * 100) if len(values) else np.nan,
        "net_expectancy_pct": float(values.mean()) if len(values) else np.nan,
        "net_median_return_pct": float(values.median()) if len(values) else np.nan,
        "profit_factor": float(pf),
        "average_ihsg_return_pct": float(completed.ihsg_return_pct.mean()) if len(completed) else np.nan,
        "net_excess_vs_ihsg_pct": float(completed.net_excess_vs_ihsg_pct.mean()) if len(completed) else np.nan,
        "stop_exit_pct": float(completed.exit_reason.str.startswith("STOP").mean() * 100) if len(completed) else np.nan,
        "target_exit_pct": float(completed.exit_reason.str.startswith("TARGET").mean() * 100) if len(completed) else np.nan,
    }


def grouped_stats(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame([{**dict(zip(columns, keys if isinstance(keys, tuple) else (keys,))), **stats(group)} for keys, group in frame.groupby(columns, dropna=False)])


def recommendation_stats(frame: pd.DataFrame) -> dict:
    """Measure one top-N screen as a run, while retaining trade diagnostics."""
    completed = frame[frame.trade_status.eq("COMPLETED")].copy()
    baskets = completed.groupby("screen_date", dropna=False).agg(
        basket_return_pct=("net_return_pct", "mean"),
        basket_excess_vs_ihsg_pct=("net_excess_vs_ihsg_pct", "mean"),
    )
    individual = stats(frame)
    runs = int(frame.screen_date.nunique())
    return {
        "recommendation_runs": runs,
        "order_count": len(frame),
        "average_picks_per_run": len(frame) / runs if runs else np.nan,
        "triggered_orders": individual["triggered_orders"],
        "trigger_rate_pct": individual["trigger_rate_pct"],
        "executed_runs": len(baskets),
        "positive_basket_run_rate_pct": (
            float((baskets.basket_return_pct > 0).mean() * 100)
            if len(baskets) else np.nan
        ),
        "mean_basket_return_pct": (
            float(baskets.basket_return_pct.mean()) if len(baskets) else np.nan
        ),
        "median_basket_return_pct": (
            float(baskets.basket_return_pct.median()) if len(baskets) else np.nan
        ),
        "mean_basket_excess_vs_ihsg_pct": (
            float(baskets.basket_excess_vs_ihsg_pct.mean())
            if len(baskets) else np.nan
        ),
        "individual_win_rate_pct": individual["win_rate_pct"],
        "individual_net_expectancy_pct": individual["net_expectancy_pct"],
        "individual_median_return_pct": individual["net_median_return_pct"],
        "individual_profit_factor": individual["profit_factor"],
    }


def create_recommendation_summary(trades: pd.DataFrame) -> pd.DataFrame:
    """Summarise displayed top picks by status and as one combined top-N list."""
    work = trades[trades.is_top_pick.astype(bool)].copy()
    keys = ["timeframe", "max_hold_bars", "max_hold_days", "sample_split"]
    rows = []
    for values, group in work.groupby(keys, dropna=False):
        base = dict(zip(keys, values))
        rows.append({
            **base,
            "recommendation_policy": "ALL_TOP_PICKS",
            **recommendation_stats(group),
        })
        for status, status_group in group.groupby("status", dropna=False):
            rows.append({
                **base,
                "recommendation_policy": str(status),
                **recommendation_stats(status_group),
            })
    return pd.DataFrame(rows, columns=RECOMMENDATION_SUMMARY_COLUMNS)


def create_recommendation_rank_summary(trades: pd.DataFrame) -> pd.DataFrame:
    work = trades[trades.is_top_pick.astype(bool)].copy()
    keys = [
        "timeframe", "max_hold_bars", "max_hold_days", "sample_split",
        "status", "status_rank",
    ]
    rows = []
    for values, group in work.groupby(keys, dropna=False):
        rows.append({**dict(zip(keys, values)), **recommendation_stats(group)})
    return pd.DataFrame(rows)


def create_recommendation_yearly_summary(trades: pd.DataFrame) -> pd.DataFrame:
    work = trades[trades.is_top_pick.astype(bool)].copy()
    work["calendar_year"] = pd.to_datetime(work.screen_date).dt.year
    keys = [
        "timeframe", "max_hold_bars", "max_hold_days", "sample_split",
        "calendar_year",
    ]
    rows = []
    for values, group in work.groupby(keys, dropna=False):
        base = dict(zip(keys, values))
        rows.append({
            **base,
            "recommendation_policy": "ALL_TOP_PICKS",
            **recommendation_stats(group),
        })
        for status, status_group in group.groupby("status", dropna=False):
            rows.append({
                **base,
                "recommendation_policy": str(status),
                **recommendation_stats(status_group),
            })
    return pd.DataFrame(rows)


def _cadence_dates(dates: pd.DatetimeIndex, cadence: str) -> pd.DatetimeIndex:
    series = pd.Series(pd.DatetimeIndex(dates), index=pd.DatetimeIndex(dates))
    weekly = pd.DatetimeIndex(series.groupby(dates.to_period("W-FRI")).max())
    if cadence == "weekly":
        return weekly
    if cadence == "biweekly":
        return weekly[::2]
    if cadence == "monthly":
        return pd.DatetimeIndex(series.groupby(dates.to_period("M")).max())
    raise ValueError(f"Unsupported cadence: {cadence}")


def create_cadence_summary(
    trades: pd.DataFrame,
    ihsg: pd.DataFrame,
    start: pd.Timestamp,
    holdout: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Test occasional use of the daily screener on fixed calendar schedules."""
    work = trades[
        trades.is_top_pick.astype(bool) & trades.timeframe.eq("daily_swing")
    ].copy()
    work["screen_date"] = pd.to_datetime(work.screen_date)
    rows = []
    periods = (
        ("SELECTION", start, holdout - pd.Timedelta(nanoseconds=1)),
        ("CONFIRMATION", holdout, end),
    )
    for split, period_start, period_end in periods:
        market_dates = pd.DatetimeIndex(
            ihsg.index[(ihsg.index >= period_start) & (ihsg.index <= period_end)]
        )
        split_work = work[work.sample_split.eq(split)]
        for cadence in ("weekly", "biweekly", "monthly"):
            scheduled = _cadence_dates(market_dates, cadence)
            for (bars, days), hold_group in split_work.groupby(
                ["max_hold_bars", "max_hold_days"], dropna=False,
            ):
                sample = hold_group[hold_group.screen_date.isin(scheduled)]
                completed = sample[sample.trade_status.eq("COMPLETED")]
                baskets = completed.groupby("screen_date").agg(
                    basket_return_pct=("net_return_pct", "mean"),
                    basket_excess_vs_ihsg_pct=("net_excess_vs_ihsg_pct", "mean"),
                )
                rows.append({
                    "timeframe": "daily_swing",
                    "max_hold_bars": bars,
                    "max_hold_days": days,
                    "sample_split": split,
                    "cadence": cadence,
                    "scheduled_runs": len(scheduled),
                    "recommendation_runs": int(sample.screen_date.nunique()),
                    "runs_with_triggered_orders": len(baskets),
                    "order_count": len(sample),
                    "triggered_orders": len(completed),
                    "trigger_rate_pct": (
                        len(completed) / len(sample) * 100 if len(sample) else np.nan
                    ),
                    "positive_basket_run_rate_pct": (
                        float((baskets.basket_return_pct > 0).mean() * 100)
                        if len(baskets) else np.nan
                    ),
                    "mean_basket_return_pct": (
                        float(baskets.basket_return_pct.mean())
                        if len(baskets) else np.nan
                    ),
                    "median_basket_return_pct": (
                        float(baskets.basket_return_pct.median())
                        if len(baskets) else np.nan
                    ),
                    "mean_basket_excess_vs_ihsg_pct": (
                        float(baskets.basket_excess_vs_ihsg_pct.mean())
                        if len(baskets) else np.nan
                    ),
                })
    return pd.DataFrame(rows, columns=CADENCE_SUMMARY_COLUMNS)


def create_breakdown(trades: pd.DataFrame) -> pd.DataFrame:
    work = trades.copy()
    work["rank_bucket"] = np.where(work.status_rank <= 3, work.status_rank.astype(str), "4+")
    work["score_bucket"] = pd.cut(work.quality_score, [-np.inf, 59.9, 69.9, 79.9, 89.9, np.inf], labels=["<60", "60-69", "70-79", "80-89", "90+"])
    base = ["timeframe", "max_hold_bars", "max_hold_days", "sample_split", "status"]
    dimensions = ["rank_bucket", "score_bucket", "setup", "market_regime", *[f"factor_{f}" for f in V1.FACTOR_NAMES]]
    rows = []
    for dimension in dimensions:
        part = grouped_stats(work, [*base, dimension]).rename(columns={dimension: "dimension_value"})
        part.insert(len(base), "dimension", dimension)
        rows.append(part)
    return pd.concat(rows, ignore_index=True)


def portfolio_candidates(trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Return only orders the portfolio policy could actually attempt to buy.

    This intentionally applies the same top-N and completed-trigger filters to
    parameter selection, walk-forward tests, and the final portfolio run.
    Trades whose planned entry occurs after the period are excluded; positions
    that remain open at the boundary are handled by ``simulate_portfolio``.
    """
    work = trades[
        trades.status.eq("Ready to Enter")
        & trades.is_top_pick.astype(bool)
        & trades.trade_status.eq("COMPLETED")
        & trades.screen_date.ge(start)
        & trades.screen_date.le(end)
        & trades.entry_date.le(end)
    ].copy()
    return work


def select_holds_from_portfolio(
    trades: pd.DataFrame,
    stocks: dict[str, pd.DataFrame],
    ihsg: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_capital: float,
    max_positions: int,
    cfg: ExecutionConfig,
    scope: str,
    minimum_trades: dict[str, int] | None = None,
    minimum_pf: float = HOLD_SELECTION_MIN_PF,
    maximum_drawdown_pct: float = HOLD_SELECTION_MAX_DRAWDOWN_PCT,
) -> tuple[dict[str, tuple[int, int]], pd.DataFrame]:
    """Choose a hold only from a constrained portfolio fit-window simulation.

    This deliberately does not use individual-trade expectancy.  Every hold is
    subjected to the same cash, slot, duplicate-ticker, and boundary rules that
    apply to a live portfolio, then audited whether it met the pre-declared
    minimum evidence, PF, drawdown, and excess-return gates.
    """
    minimum_trades = minimum_trades or HOLD_SELECTION_MIN_TRADES
    candidates = portfolio_candidates(trades, start, end)
    market_dates = pd.DatetimeIndex(ihsg.index[(ihsg.index >= start) & (ihsg.index <= end)])
    rows = []
    for timeframe, holds in HOLD_GRID.items():
        for bars, days in holds:
            subset = candidates[
                candidates.timeframe.eq(timeframe) & candidates.max_hold_bars.eq(bars)
            ]
            closed, curve, skipped = simulate_portfolio(
                subset, stocks, market_dates, initial_capital, max_positions, cfg,
            )
            result = portfolio_stats(
                timeframe, "FIT", bars, days, initial_capital, closed, curve, ihsg,
            ) if not curve.empty else {
                "timeframe": timeframe, "sample_split": "FIT", "max_hold_bars": bars,
                "max_hold_days": days, "closed_trades": 0, "profit_factor": np.nan,
                "cagr_excess_vs_ihsg_pct": np.nan, "max_drawdown_pct": np.nan,
            }
            min_trade_count = int(minimum_trades[timeframe])
            passes_trades = int(result["closed_trades"]) >= min_trade_count
            passes_pf = bool(
                not np.isnan(result["profit_factor"])
                and result["profit_factor"] >= minimum_pf
            )
            passes_drawdown = bool(
                np.isfinite(result["max_drawdown_pct"])
                and result["max_drawdown_pct"] >= -maximum_drawdown_pct
            )
            passes_excess = bool(
                np.isfinite(result["cagr_excess_vs_ihsg_pct"])
                and result["cagr_excess_vs_ihsg_pct"] > 0
            )
            eligible = passes_trades and passes_pf and passes_drawdown and passes_excess
            rows.append({
                "selection_scope": scope,
                "fit_start": str(pd.Timestamp(start).date()),
                "fit_end": str(pd.Timestamp(end).date()),
                **result,
                "eligible_candidates": len(subset),
                "skipped_candidates": len(skipped),
                "minimum_closed_trades": min_trade_count,
                "minimum_profit_factor": minimum_pf,
                "maximum_drawdown_limit_pct": -maximum_drawdown_pct,
                "passes_minimum_trades": passes_trades,
                "passes_profit_factor": passes_pf,
                "passes_drawdown": passes_drawdown,
                "passes_cagr_excess": passes_excess,
                "eligible_for_selection": eligible,
                "selection_status": "ELIGIBLE" if eligible else "FAIL_GATES",
                "selection_rank": np.nan,
                "selected": False,
            })
    audit = pd.DataFrame(rows)
    selected: dict[str, tuple[int, int]] = {}
    for timeframe, group in audit[audit.eligible_for_selection].groupby("timeframe"):
        ranked = group.sort_values(
            ["cagr_excess_vs_ihsg_pct", "profit_factor", "closed_trades"],
            ascending=False,
        )
        audit.loc[ranked.index, "selection_rank"] = range(1, len(ranked) + 1)
        winner = ranked.iloc[0]
        audit.loc[winner.name, "selected"] = True
        audit.loc[winner.name, "selection_status"] = "SELECTED"
        selected[timeframe] = (int(winner.max_hold_bars), int(winner.max_hold_days))
    return selected, audit


def walk_forward_folds(start: pd.Timestamp, development_end: pd.Timestamp) -> list[dict[str, pd.Timestamp | str]]:
    """Create the pre-registered expanding-window folds from BACKTEST_PLAN_V2."""
    fit_ends = [
        start + pd.DateOffset(years=3) - pd.Timedelta(days=1),
        start + pd.DateOffset(years=5) - pd.Timedelta(days=1),
        start + pd.DateOffset(years=7) - pd.Timedelta(days=1),
    ]
    validation_lengths = [2, 2, 1]
    folds = []
    for number, (fit_end, years) in enumerate(zip(fit_ends, validation_lengths), start=1):
        validation_start = fit_end + pd.Timedelta(days=1)
        validation_end = validation_start + pd.DateOffset(years=years) - pd.Timedelta(days=1)
        if validation_end <= development_end:
            folds.append({"fold": f"WF-{number}", "fit_start": start, "fit_end": fit_end, "validation_start": validation_start, "validation_end": validation_end})
    return folds


def simulate_portfolio(
    candidates: pd.DataFrame,
    stocks: dict[str, pd.DataFrame],
    market_dates: pd.DatetimeIndex,
    initial_capital: float,
    max_positions: int,
    cfg: ExecutionConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Enter executable candidates, force-close at the boundary, and audit skips."""
    scheduled = {
        pd.Timestamp(date): group.sort_values("status_rank").to_dict("records")
        for date, group in candidates.groupby("entry_date")
    }
    cash, active, closed, curve, skipped = float(initial_capital), {}, [], [], []
    last_close: dict[str, float] = {}
    for date in market_dates:
        # Existing positions release cash before same-day new entries.
        for ticker, position in list(active.items()):
            if pd.Timestamp(position["exit_date"]) != date:
                continue
            proceeds = position["shares"] * position["exit_price"] - side_cost(position["exit_price"], position["shares"], cfg)
            cash += proceeds
            closed.append({**position, "proceeds": proceeds, "pnl": proceeds - position["entry_cost"]})
            del active[ticker]
        entry_equity = cash
        for ticker, position in active.items():
            stock = stocks[ticker]
            price = float(stock.loc[date, "Close"]) if date in stock.index else last_close.get(ticker, position["entry_price"])
            last_close[ticker] = price
            liquidation_price = sell_fill(price, cfg)
            entry_equity += position["shares"] * liquidation_price - side_cost(
                liquidation_price, position["shares"], cfg
            )
        for signal in scheduled.get(date, []):
            ticker = signal["ticker"]
            if ticker in active:
                skipped.append({**signal, "portfolio_date": date, "skip_reason": "DUPLICATE_TICKER_ACTIVE"})
                continue
            if len(active) >= max_positions:
                skipped.append({**signal, "portfolio_date": date, "skip_reason": "CAPACITY_FULL"})
                continue
            unit_cost = float(signal["entry_price"]) * (1 + cfg.fee_pct / 100)
            slots = max_positions - len(active)
            allocation_budget = min(
                cash / slots,
                entry_equity * cfg.max_position_pct / 100,
            )
            shares = math.floor(allocation_budget / unit_cost / cfg.lot_size) * cfg.lot_size
            risk_shares = None
            if cfg.risk_per_trade_pct > 0:
                stop_price = float(signal.get("stop_price", np.nan))
                stop_proceeds = sell_fill(stop_price, cfg) * (1 - cfg.fee_pct / 100)
                risk_per_share = unit_cost - stop_proceeds
                if not np.isfinite(risk_per_share) or risk_per_share <= 0:
                    skipped.append({**signal, "portfolio_date": date, "skip_reason": "INVALID_STOP_FOR_RISK_SIZING"})
                    continue
                risk_budget = entry_equity * cfg.risk_per_trade_pct / 100
                risk_shares = math.floor(risk_budget / risk_per_share / cfg.lot_size) * cfg.lot_size
                shares = min(shares, risk_shares)
            if shares < cfg.lot_size:
                reason = "RISK_BUDGET_BELOW_ONE_LOT" if risk_shares is not None and risk_shares < cfg.lot_size else "INSUFFICIENT_CASH_FOR_LOT"
                skipped.append({**signal, "portfolio_date": date, "skip_reason": reason})
                continue
            entry_cost = shares * unit_cost
            cash -= entry_cost
            active[ticker] = {**signal, "shares": shares, "entry_cost": entry_cost}
        # A signal can trigger and reach its stop/target during the entry
        # session.  Keep it active while admitting the day's candidates so it
        # still consumes a portfolio slot, then realise it before marking the
        # end-of-day equity.  Without this pass its exit date has already gone
        # by and the position remains incorrectly open until the test boundary.
        for ticker, position in list(active.items()):
            if (
                pd.Timestamp(position["entry_date"]) != date
                or pd.Timestamp(position["exit_date"]) != date
            ):
                continue
            proceeds = position["shares"] * position["exit_price"] - side_cost(
                position["exit_price"], position["shares"], cfg
            )
            cash += proceeds
            closed.append({
                **position,
                "proceeds": proceeds,
                "pnl": proceeds - position["entry_cost"],
            })
            del active[ticker]
        equity = cash
        for ticker, position in active.items():
            stock = stocks[ticker]
            price = float(stock.loc[date, "Close"]) if date in stock.index else last_close.get(ticker, position["entry_price"])
            last_close[ticker] = price
            equity += position["shares"] * sell_fill(price, cfg) - side_cost(sell_fill(price, cfg), position["shares"], cfg)
        curve.append({"date": date, "equity": equity, "cash": cash, "active_positions": len(active)})
    # A fixed test boundary must realise any residual position.  Otherwise the
    # portfolio return mixes realised performance with an unlabelled open P&L.
    if len(market_dates):
        end_date = pd.Timestamp(market_dates[-1])
        for ticker, position in list(active.items()):
            stock = stocks[ticker]
            base = float(stock.loc[end_date, "Close"]) if end_date in stock.index else last_close.get(ticker, position["entry_price"])
            exit_price = sell_fill(base, cfg)
            proceeds = position["shares"] * exit_price - side_cost(exit_price, position["shares"], cfg)
            cash += proceeds
            closed.append({**position, "exit_date": end_date, "exit_price": exit_price, "exit_reason": "FORCED_END", "proceeds": proceeds, "pnl": proceeds - position["entry_cost"], "forced_end": True})
            del active[ticker]
        if curve:
            curve[-1] = {"date": end_date, "equity": cash, "cash": cash, "active_positions": 0}
    curve_frame = pd.DataFrame(curve)
    if not curve_frame.empty:
        curve_frame["equity_peak"] = curve_frame.equity.cummax().clip(lower=initial_capital)
        curve_frame["drawdown_pct"] = (curve_frame.equity / curve_frame.equity_peak - 1) * 100
    return pd.DataFrame(closed), curve_frame, pd.DataFrame(skipped)


def portfolio_stats(timeframe: str, split: str, bars: int, days: int, initial: float, closed: pd.DataFrame, curve: pd.DataFrame, ihsg: pd.DataFrame) -> dict:
    final = float(curve.iloc[-1].equity)
    start_date, end_date = pd.Timestamp(curve.iloc[0].date), pd.Timestamp(curve.iloc[-1].date)
    years = max((end_date - start_date).days / 365.25, 1 / 365.25)
    total = (final / initial - 1) * 100
    benchmark = V1.period_ihsg_return(ihsg, pd.DatetimeIndex(curve.date))
    cagr = ((final / initial) ** (1 / years) - 1) * 100
    ihsg_cagr = ((1 + benchmark / 100) ** (1 / years) - 1) * 100 if np.isfinite(benchmark) and benchmark > -100 else np.nan
    pnl = closed["pnl"] if len(closed) and "pnl" in closed else pd.Series(dtype=float)
    pf = pnl[pnl > 0].sum() / abs(pnl[pnl < 0].sum()) if (pnl < 0).any() else (np.inf if (pnl > 0).any() else np.nan)
    forced = int(closed.get("forced_end", pd.Series(False, index=closed.index)).fillna(False).sum()) if len(closed) else 0
    return {"timeframe": timeframe, "sample_split": split, "max_hold_bars": bars, "max_hold_days": days, "start_equity": initial, "final_equity": final, "total_return_pct": total, "cagr_pct": cagr, "ihsg_return_pct": benchmark, "ihsg_cagr_pct": ihsg_cagr, "cagr_excess_vs_ihsg_pct": cagr - ihsg_cagr if np.isfinite(ihsg_cagr) else np.nan, "max_drawdown_pct": float(curve.drawdown_pct.min()), "closed_trades": len(closed), "win_rate_pct": float((pnl > 0).mean() * 100) if len(pnl) else np.nan, "profit_factor": float(pf), "forced_end_exits": forced, "open_positions_at_end": int(curve.iloc[-1].active_positions)}


def run_walk_forward(
    trades: pd.DataFrame,
    stocks: dict[str, pd.DataFrame],
    ihsg: pd.DataFrame,
    start: pd.Timestamp,
    development_end: pd.Timestamp,
    initial_capital: float,
    max_positions: int,
    cfg: ExecutionConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Evaluate pre-registered expanding folds without using confirmation data."""
    rows, audits = [], []
    for fold in walk_forward_folds(start, development_end):
        fit_start, fit_end = pd.Timestamp(fold["fit_start"]), pd.Timestamp(fold["fit_end"])
        validation_start, validation_end = pd.Timestamp(fold["validation_start"]), pd.Timestamp(fold["validation_end"])
        selected, audit = select_holds_from_portfolio(
            trades, stocks, ihsg, fit_start, fit_end, initial_capital, max_positions,
            cfg, scope=str(fold["fold"]),
        )
        audits.append(audit.assign(**fold))
        dates = pd.DatetimeIndex(ihsg.index[(ihsg.index >= validation_start) & (ihsg.index <= validation_end)])
        for timeframe, (bars, days) in selected.items():
            candidates = portfolio_candidates(trades, validation_start, validation_end)
            candidates = candidates[candidates.timeframe.eq(timeframe) & candidates.max_hold_bars.eq(bars)]
            closed, curve, skipped = simulate_portfolio(candidates, stocks, dates, initial_capital, max_positions, cfg)
            if curve.empty:
                continue
            result = portfolio_stats(timeframe, "WALK_FORWARD", bars, days, initial_capital, closed, curve, ihsg)
            rows.append({
                **fold,
                **result,
                "eligible_candidates": len(candidates),
                "skipped_candidates": len(skipped),
            })
    return (
        pd.DataFrame(rows, columns=WALK_FORWARD_COLUMNS),
        pd.concat(audits, ignore_index=True) if audits else pd.DataFrame(),
    )


def write_analysis(
    path: Path,
    summary: pd.DataFrame,
    metadata: dict,
    selection_audit: pd.DataFrame | None = None,
) -> None:
    if {"status", "sample_split"}.issubset(summary.columns):
        selected = summary[
            summary.status.eq("Ready to Enter")
            & summary.sample_split.eq("SELECTION")
        ]
    else:
        selected = pd.DataFrame()
    variant = metadata.get("screener_variant_id", "V2_FROZEN_BASELINE")
    setup_family = metadata.get("ready_setup_family", "all")
    universe_note = (
        "- Universe uses an IPO/listing proxy and remains survivor-biased until historical delistings are supplied."
        if metadata.get("universe_membership_applied_per_snapshot")
        else "- Results remain survivor-biased until a point-in-time universe is supplied."
    )
    lines = [
        "# Execution-Valid Screener Backtest", "", "## Scope", "",
        f"- Screener variant: `{variant}`; Ready setup family: `{setup_family}`.",
        f"- Fee per side: {metadata['fee_pct']:.3f}%; adverse slippage per side: {metadata['slippage_pct']:.3f}%.",
        "- Planned entries are no-trade unless the trigger occurs in their configured entry window.",
        universe_note, "", "## Selection summary", "",
        "| Timeframe | Hold bars | Orders | Trigger rate | Net expectancy | PF | Net excess vs IHSG |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in selected.iterrows():
        lines.append(f"| {row.timeframe} | {int(row.max_hold_bars)} | {int(row.order_count)} | {row.trigger_rate_pct:.1f}% | {row.net_expectancy_pct:.3f}% | {row.profit_factor:.3f} | {row.net_excess_vs_ihsg_pct:.3f}% |")
    if selection_audit is not None and len(selection_audit):
        selection = selection_audit[selection_audit.selection_scope.eq("SELECTION")]
        selected_holds = selection[selection.selected.astype(bool)]
        lines.extend(["", "## Constrained hold selection"])
        if selected_holds.empty:
            lines.append(
                "No timeframe passed the constrained portfolio fit gates; no portfolio or "
                "walk-forward variant was selected. See `hold_selection_audit.csv`."
            )
        else:
            lines.extend(["", "| Timeframe | Selected hold | PF | CAGR excess | Max drawdown |", "|---|---:|---:|---:|---:|"])
            for _, row in selected_holds.iterrows():
                lines.append(
                    f"| {row.timeframe} | {int(row.max_hold_bars)} | {row.profit_factor:.3f} | "
                    f"{row.cagr_excess_vs_ihsg_pct:.3f}% | {row.max_drawdown_pct:.3f}% |"
                )
    lines.extend(["", "No V2 screening parameter may be changed based on the confirmation period. See `BACKTEST_PLAN_V2.md`."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_recommendation_analysis(
    path: Path,
    summary: pd.DataFrame,
    cadence: pd.DataFrame,
    metadata: dict,
) -> None:
    lines = [
        "# Recommendation-Centric Backtest", "",
        f"- Screener variant: `{metadata.get('screener_variant_id', '')}`.",
        "- One screen date is one recommendation run; completed top picks are equal-weighted within that run.",
        "- Ready and Wait signals remain separate so a watchlist is not mistaken for an immediate entry.",
        "- Portfolio CAGR and drawdown are secondary diagnostics, not recommendation-quality gates.",
        "", "## Top-pick outcomes", "",
        "| Split | Timeframe | Hold | Policy | Runs | Avg picks | Trigger | Positive runs | Mean basket | Median basket | Basket excess | PF |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in summary.iterrows():
        lines.append(
            f"| {row.sample_split} | {row.timeframe} | {int(row.max_hold_days)}d | "
            f"{row.recommendation_policy} | {int(row.recommendation_runs)} | "
            f"{row.average_picks_per_run:.2f} | {row.trigger_rate_pct:.1f}% | "
            f"{row.positive_basket_run_rate_pct:.1f}% | {row.mean_basket_return_pct:.3f}% | "
            f"{row.median_basket_return_pct:.3f}% | "
            f"{row.mean_basket_excess_vs_ihsg_pct:.3f}% | "
            f"{row.individual_profit_factor:.3f} |"
        )
    if len(cadence):
        lines.extend([
            "", "## Occasional daily-screener schedules", "",
            "The schedule uses the final IDX trading session of each calendar week/month; biweekly uses alternating weekly sessions.",
            "", "| Split | Hold | Cadence | Scheduled | Recommendation runs | Executed runs | Positive runs | Mean basket | Median basket |",
            "|---|---:|---|---:|---:|---:|---:|---:|---:|",
        ])
        for _, row in cadence.iterrows():
            lines.append(
                f"| {row.sample_split} | {int(row.max_hold_days)}d | {row.cadence} | "
                f"{int(row.scheduled_runs)} | {int(row.recommendation_runs)} | "
                f"{int(row.runs_with_triggered_orders)} | "
                f"{row.positive_basket_run_rate_pct:.1f}% | "
                f"{row.mean_basket_return_pct:.3f}% | {row.median_basket_return_pct:.3f}% |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_self_test() -> None:
    dates = pd.date_range("2025-01-02", periods=5, freq="B")
    ihsg = pd.DataFrame({k: [100.] * 5 for k in ("Open", "High", "Low", "Close")}, index=dates)
    base = {"signal_id": "unit", "timeframe": "daily_swing", "screen_date": dates[0] - pd.Timedelta(days=1), "ticker": "TEST.JK", "status": "Pending Trigger", "status_rank": 1, "quality_score": 70., "trigger_price": 105., "stop_price": 95., "target_price": 115., "entry_type": "planned", "entry_window": 2}
    cfg = ExecutionConfig(fee_pct=0.2, slippage_pct=0.1, same_bar_policy="stop")
    triggered = pd.DataFrame({"Open": [100, 104, 106, 110, 110], "High": [102, 106, 110, 116, 111], "Low": [99, 103, 105, 109, 109], "Close": [101, 105, 109, 115, 110], "Volume": [1] * 5}, index=dates)
    result = simulate_signal(base, triggered, ihsg, 3, 3, cfg)
    assert result["trade_status"] == "COMPLETED" and result["entry_date"] == dates[1]
    assert result["exit_reason"] == "TARGET" and result["net_return_pct"] < result["gross_return_pct"]
    no_trigger = triggered.copy(); no_trigger.loc[:, "High"] = 104
    assert simulate_signal(base, no_trigger, ihsg, 3, 3, cfg)["order_status"] == "NO_TRIGGER"
    stopped = triggered.copy(); stopped.loc[dates[1], ["High", "Low"]] = [104, 94]
    assert simulate_signal(base, stopped, ihsg, 3, 3, cfg)["order_status"] == "PRE_TRIGGER_STOP"
    active = {**base, "entry_type": "active", "trigger_price": 100.}
    same_bar = triggered.copy(); same_bar.loc[dates[0], ["High", "Low"]] = [116, 94]
    assert simulate_signal(active, same_bar, ihsg, 3, 3, cfg)["exit_reason"] == "STOP_SAME_BAR"
    first = {**result, "is_top_pick": True}
    second = {**first, "signal_id": "unit-2", "ticker": "TEST2.JK", "status_rank": 2}
    closed, curve, skipped = simulate_portfolio(pd.DataFrame([first, second]), {"TEST.JK": triggered, "TEST2.JK": triggered}, pd.DatetimeIndex(dates[:2]), 1_000_000, 1, cfg)
    assert len(closed) == 1 and closed.iloc[0].exit_reason == "FORCED_END"
    assert curve.iloc[-1].active_positions == 0 and skipped.iloc[0].skip_reason == "CAPACITY_FULL"
    same_session = {
        **first, "entry_date": dates[0], "exit_date": dates[0],
        "entry_price": 100.0, "exit_price": 95.0, "exit_reason": "STOP_SAME_BAR",
    }
    same_closed, same_curve, _ = simulate_portfolio(
        pd.DataFrame([same_session]), {"TEST.JK": triggered},
        pd.DatetimeIndex(dates[:1]), 1_000_000, 1, cfg,
    )
    assert len(same_closed) == 1 and same_closed.iloc[0].exit_reason == "STOP_SAME_BAR"
    assert not bool(same_closed.iloc[0].get("forced_end", False))
    assert same_curve.iloc[-1].active_positions == 0
    print("V2 self-test passed: trigger, cancellation, conservative exit, costs, capacity audit, same-session exit, and force-close.")


def canonical_ticker(value: object) -> str:
    """Normalize a manifest symbol without accepting a non-IDX symbol silently."""
    ticker = str(value).strip().upper()
    return ticker if ticker.endswith(".JK") else f"{ticker}.JK"


def load_membership_manifest(path: Path) -> pd.DataFrame:
    """Read explicit historical IDX membership intervals for the PIT audit.

    The manifest is intentionally a simple CSV: ticker,effective_start,effective_end.
    Each row says that a ticker was eligible for the universe, inclusively, over
    that interval.  Delisted securities therefore remain represented instead of
    disappearing merely because today's Excel list no longer contains them.
    """
    manifest = pd.read_csv(path)
    required = {"ticker", "effective_start", "effective_end"}
    missing = required.difference(manifest.columns)
    if missing:
        raise ValueError(
            f"Universe manifest is missing required columns: {sorted(missing)}"
        )
    manifest = manifest.loc[:, ["ticker", "effective_start", "effective_end"]].copy()
    manifest["ticker"] = manifest["ticker"].map(canonical_ticker)
    manifest["effective_start"] = pd.to_datetime(manifest["effective_start"], errors="coerce")
    manifest["effective_end"] = pd.to_datetime(manifest["effective_end"], errors="coerce")
    if manifest.empty or manifest[["ticker", "effective_start", "effective_end"]].isna().any().any():
        raise ValueError("Universe manifest has blank or invalid ticker/effective dates")
    if (manifest["effective_end"] < manifest["effective_start"]).any():
        raise ValueError("Universe manifest has an effective_end before effective_start")
    return manifest.sort_values(["ticker", "effective_start", "effective_end"]).reset_index(drop=True)


def active_manifest_tickers(
    manifest: pd.DataFrame, available_pool: list[str], date: pd.Timestamp,
) -> list[str]:
    """Return cached tickers eligible on a particular completed-candle date."""
    eligible = manifest.loc[
        manifest.effective_start.le(date) & manifest.effective_end.ge(date), "ticker",
    ]
    available = set(available_pool)
    return sorted(set(eligible).intersection(available))


def apply_ready_setup_family(
    screener: ModuleType, candidates: list[dict], family: str,
) -> list[dict]:
    """Keep only a named setup family in Ready-to-Enter portfolio candidates.

    Wait candidates are retained for audit visibility.  Ready candidates are
    re-ranked after filtering so a rejected setup does not leave artificial
    gaps in status_rank/top-N selection.
    """
    if family == "all":
        return candidates
    allowed = READY_SETUP_FAMILIES[family]
    kept = [
        candidate for candidate in candidates
        if candidate.get("normalized_status") != V1.ready_status(screener)
        or candidate.get("setup_name") in allowed
    ]
    return V1.rank_candidates(screener, kept)


def build_manifest_breadth_history(
    history: V1.SnapshotHistory, manifest: pd.DataFrame, available_pool: list[str],
) -> pd.DataFrame:
    """Precompute breadth for a changing universe once, not once per snapshot."""
    index = pd.DatetimeIndex(history.breadth.index)
    length, width = len(index), len(available_pool)
    valid50 = np.zeros(length, dtype="int32")
    above50 = np.zeros(length, dtype="int32")
    valid200 = np.zeros(length, dtype="int32")
    above200 = np.zeros(length, dtype="int32")
    returns = np.full((length, width), np.nan, dtype="float64")

    for column, ticker in enumerate(available_pool):
        component = history.breadth_components.get(ticker)
        intervals = manifest.loc[manifest.ticker.eq(ticker)]
        if component is None or intervals.empty:
            continue
        active = np.zeros(length, dtype=bool)
        for interval in intervals.itertuples(index=False):
            active |= (index >= interval.effective_start) & (index <= interval.effective_end)
        valid50 += component.valid50.to_numpy(dtype="int32") * active
        above50 += component.above50.to_numpy(dtype="int32") * active
        valid200 += component.valid200.to_numpy(dtype="int32") * active
        above200 += component.above200.to_numpy(dtype="int32") * active
        values = component.return20.to_numpy(dtype="float64")
        returns[:, column] = np.where(active, values, np.nan)

    median_return20 = pd.DataFrame(returns, index=index).median(axis=1, skipna=True).to_numpy()
    return pd.DataFrame({
        "breadth50": above50 / np.where(valid50, valid50, np.nan),
        "breadth200": above200 / np.where(valid200, valid200, np.nan),
        "median_return20": median_return20,
        "jumlah_saham_breadth": valid50,
    }, index=index)


IDX_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "mei": "05", "jun": "06",
    "jul": "07", "agu": "08", "agt": "08", "sep": "09", "okt": "10", "nov": "11", "des": "12",
}


def parse_idx_listing_date(value: object) -> pd.Timestamp:
    """Parse the Indonesian date strings used by the IDX company-list workbook."""
    text = str(value).strip()
    parts = text.split()
    if len(parts) == 3 and parts[1].lower()[:3] in IDX_MONTHS:
        text = f"{parts[0]}-{IDX_MONTHS[parts[1].lower()[:3]]}-{parts[2]}"
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        raise ValueError(f"Invalid IDX listing date: {value!r}")
    return pd.Timestamp(parsed).normalize()


def write_ipo_proxy_manifest(
    out: Path, excel_path: Path, stocks: dict[str, pd.DataFrame], end: pd.Timestamp,
) -> pd.DataFrame:
    """Create an IPO-corrected, but survivor-biased, membership proxy.

    Current IDX Excel membership supplies listing dates but not historical
    delistings.  The output is deliberately named a proxy: it prevents a stock
    from appearing before its IPO/data availability but does not invent absent
    delisted names.
    """
    source = pd.read_excel(excel_path)
    required = {"Kode", "Tanggal Pencatatan"}
    missing = required.difference(source.columns)
    if missing:
        raise ValueError(f"Excel universe is missing required columns: {sorted(missing)}")
    rows, excluded = [], []
    for record in source.loc[:, ["Kode", "Tanggal Pencatatan"]].dropna(subset=["Kode"]).to_dict("records"):
        ticker = canonical_ticker(record["Kode"])
        frame = stocks.get(ticker, pd.DataFrame())
        if frame.empty:
            excluded.append({"ticker": ticker, "reason": "NO_MARKET_DATA"})
            continue
        listing = parse_idx_listing_date(record["Tanggal Pencatatan"])
        first, last = pd.Timestamp(frame.index.min()), pd.Timestamp(frame.index.max())
        effective_start, effective_end = max(listing, first), min(last, end)
        if effective_start > effective_end:
            excluded.append({"ticker": ticker, "reason": "NO_DATA_IN_BACKTEST_WINDOW"})
            continue
        rows.append({
            "ticker": ticker,
            "effective_start": effective_start.date().isoformat(),
            "effective_end": effective_end.date().isoformat(),
            "membership_basis": "IPO_DATE_AND_OHLCV_AVAILABILITY_PROXY",
            "idx_listing_date": listing.date().isoformat(),
            "first_ohlcv_date": first.date().isoformat(),
            "last_ohlcv_date": last.date().isoformat(),
        })
    manifest = pd.DataFrame(rows).drop_duplicates("ticker").sort_values("ticker").reset_index(drop=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(out, index=False)
    pd.DataFrame(excluded).to_csv(out.with_name(f"{out.stem}_excluded.csv"), index=False)
    summary = {
        "manifest_type": "IPO_CORRECTED_SURVIVOR_BIASED_PROXY",
        "source_excel": str(excel_path),
        "source_excel_sha256": hashlib.sha256(excel_path.read_bytes()).hexdigest(),
        "backtest_end": str(end.date()),
        "included_tickers": len(manifest),
        "excluded_tickers": len(excluded),
        "limitations": [
            "The current Excel list omits previously delisted securities.",
            "effective_end reflects OHLCV availability, not verified delisting dates.",
            "This proxy may be used for an IPO-timing sensitivity test, never as proof that survivorship bias is resolved.",
        ],
    }
    out.with_name(f"{out.stem}_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return manifest


def longest_missing_run(observed: pd.Series) -> int:
    """Return the longest consecutive false run in an ordered session mask."""
    longest = current = 0
    for present in observed.astype(bool):
        current = 0 if present else current + 1
        longest = max(longest, current)
    return longest


def write_point_in_time_audit(
    out: Path,
    current_pool: list[str],
    stocks: dict[str, pd.DataFrame],
    ihsg: pd.DataFrame,
    start: pd.Timestamp,
    holdout: pd.Timestamp,
    end: pd.Timestamp,
    excel_path: Path,
    manifest_path: Path | None = None,
) -> dict:
    """Write evidence needed to reject current-universe/survivorship assumptions.

    OHLCV history can reveal coverage problems, but cannot prove past index/listing
    membership.  That proof comes only from a supplied historical interval manifest;
    the report deliberately never promotes an Excel snapshot to point-in-time truth.
    """
    out.mkdir(parents=True, exist_ok=True)
    manifest = load_membership_manifest(manifest_path) if manifest_path else None
    current_set = {canonical_ticker(ticker) for ticker in current_pool}
    manifest_set = set(manifest.ticker) if manifest is not None else set()
    tickers = sorted(current_set | manifest_set)
    market_dates = pd.DatetimeIndex(ihsg.index[(ihsg.index >= start) & (ihsg.index <= end)])

    rows = []
    for ticker in tickers:
        intervals = manifest[manifest.ticker.eq(ticker)] if manifest is not None else pd.DataFrame()
        member_at = lambda date: bool(
            not intervals.empty
            and ((intervals.effective_start <= date) & (intervals.effective_end >= date)).any()
        )
        frame = stocks.get(ticker, pd.DataFrame())
        if frame.empty:
            first = last = pd.NaT
            observed_sessions = expected_sessions = gaps = 0
            coverage = np.nan
            status = "NO_MARKET_DATA"
        else:
            index = pd.DatetimeIndex(frame.index)
            first, last = pd.Timestamp(index.min()), pd.Timestamp(index.max())
            observed_window = index[(index >= start) & (index <= end)]
            active_window = market_dates[(market_dates >= max(start, first)) & (market_dates <= min(end, last))]
            # Restrict to the benchmark calendar.  Vendor calendars can contain
            # dates absent from the benchmark response, so raw stock rows alone
            # must never yield coverage above 100%.
            observed_on_benchmark = observed_window.intersection(market_dates)
            observed_sessions, expected_sessions = len(observed_on_benchmark), len(active_window)
            present = pd.Series(active_window.isin(observed_on_benchmark), index=active_window)
            gaps = longest_missing_run(present)
            coverage = 100 * observed_sessions / expected_sessions if expected_sessions else np.nan
            status = "COVERAGE_REVIEW"
        rows.append({
            "ticker": ticker,
            "in_current_excel": ticker in current_set,
            "in_membership_manifest": ticker in manifest_set,
            "membership_intervals": len(intervals),
            "member_at_backtest_start": member_at(start),
            "member_at_holdout_start": member_at(holdout),
            "member_at_backtest_end": member_at(end),
            "has_market_data": not frame.empty,
            "first_observed_date": first,
            "last_observed_date": last,
            "observed_sessions": observed_sessions,
            "benchmark_sessions_while_observed": expected_sessions,
            "coverage_pct_while_observed": coverage,
            "max_consecutive_missing_sessions": gaps,
            "observed_after_backtest_start": bool(not pd.isna(first) and first > start),
            "observed_before_backtest_end": bool(not pd.isna(last) and last < end),
            "audit_status": status,
        })
    audit = pd.DataFrame(rows, columns=UNIVERSE_AUDIT_COLUMNS)
    audit.to_csv(out / "universe_audit.csv", index=False)
    if manifest is None:
        pd.DataFrame(columns=["ticker", "effective_start", "effective_end"]).to_csv(
            out / "point_in_time_universe_template.csv", index=False,
        )

    summary = {
        "audit_type": "point_in_time_universe_and_ohlcv_coverage",
        "certification": (
            "NOT_CERTIFIED_NO_HISTORICAL_MANIFEST"
            if manifest is None else "MANIFEST_PRESENT_MANUAL_SOURCE_REVIEW_REQUIRED"
        ),
        "current_excel": str(excel_path),
        "current_excel_sha256": hashlib.sha256(excel_path.read_bytes()).hexdigest() if excel_path.is_file() else None,
        "historical_membership_manifest": str(manifest_path) if manifest_path else None,
        "backtest_start": str(start.date()),
        "holdout_start": str(holdout.date()),
        "backtest_end": str(end.date()),
        "benchmark_sessions": len(market_dates),
        "current_excel_tickers": len(current_set),
        "manifest_tickers": len(manifest_set),
        "audited_tickers": len(audit),
        "tickers_without_market_data": int((~audit.has_market_data).sum()),
        "tickers_first_observed_after_start": int(audit.observed_after_backtest_start.sum()),
        "tickers_last_observed_before_end": int(audit.observed_before_backtest_end.sum()),
        "manual_requirements": [
            "Record the primary source, retrieval date, and license for every membership interval.",
            "Include delisted, suspended, renamed, and IPO securities; do not derive membership from the current Excel file.",
            "Resolve corporate-action/ticker-symbol mappings before rerunning the backtest with the manifest.",
            "Review material OHLCV gaps; a gap is not automatically a delisting.",
        ],
    }
    (out / "data_quality_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8",
    )
    lines = [
        "# Step 5 — Point-in-Time Data Audit", "",
        f"Certification: **{summary['certification']}**.", "",
        "The current Excel universe is an as-of-now list. Price history alone cannot prove that a ticker was eligible on a past screen date.", "",
        "## Outputs", "",
        "- `universe_audit.csv`: ticker-level membership and OHLCV coverage evidence.",
        "- `data_quality_summary.json`: aggregate counts and required manual checks.",
    ]
    if manifest is None:
        lines.extend(["- `point_in_time_universe_template.csv`: required format for the historical membership source."])
    (out / "POINT_IN_TIME_AUDIT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trigger-aware V2.0 IDX screener backtest")
    parser.add_argument("--screener-file", default="scripts/financial_screener.py")
    parser.add_argument("--excel", default="resource/daftar-saham.xlsx")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--timeframe", "--trend", dest="timeframe", choices=[*HOLD_GRID, "all"], default="all")
    parser.add_argument("--start", default="2016-01-01"); parser.add_argument("--holdout-start", default="2024-01-01"); parser.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    parser.add_argument("--period", choices=["10y", "max"], default="max"); parser.add_argument("--min-turnover", type=float, default=1_000_000_000); parser.add_argument("--min-price", type=float, default=100)
    parser.add_argument("--top", type=int, default=3); parser.add_argument("--batch-size", type=int, default=100); parser.add_argument("--max-tickers", type=int, default=0); parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--use-cache", action="store_true"); parser.add_argument("--cache-dir", default="output/backtest_cache"); parser.add_argument("--fee-pct", type=float, default=0.20); parser.add_argument("--slippage-pct", type=float, default=0.10); parser.add_argument("--same-bar-policy", choices=["stop", "target"], default="stop")
    parser.add_argument("--market-data-cache-file", default="", help="Use one validated raw market-data pickle explicitly, independent of the requested date range")
    parser.add_argument("--initial-capital", type=float, default=100_000_000); parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--risk-per-trade-pct", type=float, default=0.0, help="Risk budget at the planned stop as a percentage of current equity; 0 keeps equal-slot sizing")
    parser.add_argument("--max-position-pct", type=float, default=100.0, help="Maximum entry value per position as a percentage of current equity")
    parser.add_argument("--output-dir", default="output/backtest_v2/v2_1_validation"); parser.add_argument("--walk-forward", action="store_true", help="Write expanding-window results using only the development period")
    parser.add_argument("--replay-trades", default="", help="Rebuild portfolio and walk-forward outputs from a frozen V2 trades.csv")
    parser.add_argument("--audit-point-in-time", action="store_true", help="Write the Step 5 universe/data coverage audit and stop")
    parser.add_argument("--universe-manifest", default="", help="CSV with ticker,effective_start,effective_end historical membership intervals")
    parser.add_argument("--audit-output-dir", default="output/backtest_v2/v2_5_data_audit")
    parser.add_argument("--build-ipo-proxy-manifest", action="store_true", help="Build an IPO-corrected proxy manifest from the current IDX Excel and stop")
    parser.add_argument("--ipo-proxy-output", default="output/backtest_v2/v2_5_data_audit/ipo_proxy_manifest.csv")
    parser.add_argument("--ready-setup-family", choices=sorted(READY_SETUP_FAMILIES), default="all", help="V3-B: restrict Ready-to-Enter/top-N candidates to one setup family")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_test(); return 0
    start, holdout, end = (pd.Timestamp(value) for value in (args.start, args.holdout_start, args.end))
    if not start < holdout <= end or min(args.top, args.batch_size, args.workers, args.max_positions) < 1 or min(args.fee_pct, args.slippage_pct, args.risk_per_trade_pct) < 0 or args.initial_capital <= 0 or not 0 < args.max_position_pct <= 100:
        raise ValueError("Invalid dates or non-negative execution parameters")
    screener = V1.load_screener(Path(args.screener_file))
    global HOLD_GRID
    HOLD_GRID = getattr(screener, "BACKTEST_HOLD_GRID", V1.TIMEFRAME_HOLDS)
    if getattr(screener, "GLOBAL_RANKING", False) and args.ready_setup_family != "all":
        raise ValueError("--ready-setup-family is a V3 ablation and is not valid for V4")
    pool = V1.normalize_tickers(args.tickers) if args.tickers else screener.ambil_semua_ticker_dari_excel(args.excel)
    pool = sorted(set(pool))[:args.max_tickers or None]
    source_pool = list(pool)
    if args.market_data_cache_file:
        cache_path = Path(args.market_data_cache_file)
        if not cache_path.is_file():
            raise FileNotFoundError(f"Market-data cache not found: {cache_path}")
        with cache_path.open("rb") as handle:
            payload = pickle.load(handle)
        ihsg = V1.normalize_daily(payload.get("ihsg", pd.DataFrame()))
        stocks = {
            ticker: V1.normalize_daily(frame)
            for ticker, frame in payload.get("stocks", {}).items()
        }
        stocks = {ticker: frame for ticker, frame in stocks.items() if not frame.empty}
        if ihsg.empty or not stocks:
            raise ValueError(f"Market-data cache is empty or invalid: {cache_path}")
        print(f"Using explicit market-data cache: {cache_path}", flush=True)
    else:
        ihsg, stocks, cache_path = V1.load_market_data(
            screener, pool, args.period, args.batch_size, Path(args.cache_dir),
            end, args.use_cache,
        )
    pool = [ticker for ticker in pool if ticker in stocks]
    if args.build_ipo_proxy_manifest:
        manifest = write_ipo_proxy_manifest(Path(args.ipo_proxy_output), Path(args.excel), stocks, end)
        print(f"IPO proxy manifest completed: {Path(args.ipo_proxy_output).resolve()} ({len(manifest):,} tickers)")
        return 0
    if args.audit_point_in_time:
        manifest_path = Path(args.universe_manifest) if args.universe_manifest else None
        if manifest_path is not None and not manifest_path.is_file():
            raise FileNotFoundError(f"Universe manifest not found: {manifest_path}")
        summary = write_point_in_time_audit(
            Path(args.audit_output_dir), source_pool, stocks, ihsg, start, holdout, end,
            Path(args.excel), manifest_path,
        )
        print(
            "Step 5 audit completed: "
            f"{Path(args.audit_output_dir).resolve()} "
            f"({summary['certification']})"
        )
        return 0
    config = V1.RunConfig(start, end, holdout, args.min_turnover, args.min_price, args.top, 30)
    execution = ExecutionConfig(
        args.fee_pct, args.slippage_pct, args.same_bar_policy,
        risk_per_trade_pct=args.risk_per_trade_pct,
        max_position_pct=args.max_position_pct,
    )
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.universe_manifest) if args.universe_manifest else None
    if manifest_path is not None and not manifest_path.is_file():
        raise FileNotFoundError(f"Universe manifest not found: {manifest_path}")
    membership_manifest = load_membership_manifest(manifest_path) if manifest_path else None
    if args.replay_trades:
        replay_path = Path(args.replay_trades)
        if not replay_path.is_file():
            raise FileNotFoundError(f"Replay trades file not found: {replay_path}")
        trades = pd.read_csv(replay_path, parse_dates=["screen_date", "entry_date", "exit_date"])
        required = {"signal_id", "timeframe", "is_top_pick", "trade_status", "entry_date", "status"}
        missing = required.difference(trades.columns)
        if missing:
            raise ValueError(f"Replay file is missing required columns: {sorted(missing)}")
        signal_frame = trades.drop_duplicates("signal_id")
        errors, snapshot_counts = [], {"replay": {"source": str(replay_path), "trade_rows": len(trades)}}
        print(f"Replaying {len(trades):,} frozen trade outcomes from {replay_path}", flush=True)
    else:
        signals, errors, snapshot_counts = [], [], {}
        timeframes = list(HOLD_GRID) if args.timeframe == "all" else [args.timeframe]
        print("Precomputing causal indicator, liquidity, and breadth histories...", flush=True)
        history = V1.build_snapshot_history(screener, pool, stocks, ihsg, tuple(timeframes))
        membership_breadth = None
        if membership_manifest is not None:
            print("Precomputing point-in-time universe breadth history...", flush=True)
            membership_breadth = build_manifest_breadth_history(history, membership_manifest, pool)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for timeframe in timeframes:
                dates = V1.evaluation_dates(screener, ihsg, timeframe, start, end)
                successful = 0
                active_sizes: list[int] = []
                print(f"{timeframe}: evaluating {len(dates):,} completed-candle snapshots with {args.workers} workers", flush=True)
                for number, date in enumerate(dates, start=1):
                    try:
                        snapshot_pool = (
                            active_manifest_tickers(membership_manifest, pool, date)
                            if membership_manifest is not None else pool
                        )
                        active_sizes.append(len(snapshot_pool))
                        candidates, regime = V1.screen_snapshot(
                            screener, snapshot_pool, stocks, ihsg, date, timeframe,
                            config, executor, history,
                            breadth_history=membership_breadth,
                        )
                        candidates = apply_ready_setup_family(
                            screener, candidates, args.ready_setup_family,
                        )
                        for candidate in candidates:
                            row = V1.signal_record(candidate, regime, timeframe, date, args.top)
                            row["entry_window"] = screener.get_timeframe_config(timeframe)["entry_window"]
                            row["early_exit_rule"] = candidate.get("early_exit_rule", "")
                            row["early_exit_price"] = candidate.get("early_exit_price", np.nan)
                            row["planned_stop_distance_pct"] = candidate.get(
                                "planned_stop_distance_pct", np.nan,
                            )
                            signals.append(row)
                        successful += 1
                    except Exception as exc:
                        errors.append({"timeframe": timeframe, "screen_date": date, "error": str(exc)})
                    if number % 25 == 0 or number == len(dates):
                        print(f"  processed {number:,}/{len(dates):,} snapshots; retained {len(signals):,} eligible signals", flush=True)
                snapshot_counts[timeframe] = {
                    "scheduled": len(dates), "successful": successful,
                    "failed": len(dates) - successful,
                    "active_universe_min": min(active_sizes, default=0),
                    "active_universe_max": max(active_sizes, default=0),
                    "active_universe_mean": float(np.mean(active_sizes)) if active_sizes else 0.0,
                }
        if not signals:
            raise RuntimeError("No eligible V1 signals were produced")
        signal_frame = pd.DataFrame(signals); signal_frame["screen_date"] = pd.to_datetime(signal_frame.screen_date); signal_frame["sample_split"] = np.where(signal_frame.screen_date < holdout, "SELECTION", "CONFIRMATION")
        outcomes = []
        signal_records = signal_frame.to_dict("records")
        print(f"Simulating trigger-aware outcomes for {len(signal_records):,} signals...", flush=True)
        for number, signal in enumerate(signal_records, start=1):
            cutoff = holdout - pd.Timedelta(nanoseconds=1) if signal["sample_split"] == "SELECTION" else end
            stock, benchmark = stocks[signal["ticker"]].loc[:cutoff], ihsg.loc[:cutoff]
            for bars, days in HOLD_GRID[signal["timeframe"]]: outcomes.append(simulate_signal(signal, stock, benchmark, bars, days, execution))
            if number % 10_000 == 0 or number == len(signal_records):
                print(f"  simulated {number:,}/{len(signal_records):,} signals", flush=True)
        trades = pd.DataFrame(outcomes)
    signal_frame.to_csv(out / "signals.csv", index=False); pd.DataFrame(errors).to_csv(out / "screen_errors.csv", index=False)
    trades.to_csv(out / "trades.csv", index=False)
    summary_keys = ["timeframe", "max_hold_bars", "max_hold_days", "sample_split", "status"]
    summary = grouped_stats(trades, summary_keys); summary.to_csv(out / "summary.csv", index=False); create_breakdown(trades).to_csv(out / "breakdown.csv", index=False)
    recommendation_summary = create_recommendation_summary(trades)
    recommendation_summary.to_csv(out / "recommendation_summary.csv", index=False)
    create_recommendation_rank_summary(trades).to_csv(
        out / "recommendation_rank_summary.csv", index=False,
    )
    create_recommendation_yearly_summary(trades).to_csv(
        out / "recommendation_yearly_summary.csv", index=False,
    )
    cadence_summary = create_cadence_summary(trades, ihsg, start, holdout, end)
    cadence_summary.to_csv(out / "recommendation_cadence_summary.csv", index=False)
    selection_end = holdout - pd.Timedelta(nanoseconds=1)
    policy_selection = portfolio_candidates(trades, start, selection_end)
    policy_summary = grouped_stats(policy_selection, summary_keys)
    policy_summary.to_csv(out / "policy_summary.csv", index=False)
    selected_holds, selection_audit = select_holds_from_portfolio(
        trades, stocks, ihsg, start, selection_end, args.initial_capital,
        args.max_positions, execution, scope="SELECTION",
    )
    portfolio_summaries, portfolio_trades, portfolio_curves, portfolio_skips = [], [], [], []
    for timeframe, (bars, days) in selected_holds.items():
        for split, period_start, period_end in (("SELECTION", start, holdout - pd.Timedelta(nanoseconds=1)), ("CONFIRMATION", holdout, end)):
            candidates = portfolio_candidates(trades, period_start, period_end)
            candidates = candidates[candidates.timeframe.eq(timeframe) & candidates.max_hold_bars.eq(bars)]
            dates = ihsg.index[(ihsg.index >= period_start) & (ihsg.index <= period_end)]
            closed, curve, skipped = simulate_portfolio(candidates, stocks, pd.DatetimeIndex(dates), args.initial_capital, args.max_positions, execution)
            if not curve.empty:
                closed["timeframe"], closed["sample_split"], closed["max_hold_bars"], closed["max_hold_days"] = timeframe, split, bars, days
                curve["timeframe"], curve["sample_split"], curve["max_hold_bars"], curve["max_hold_days"] = timeframe, split, bars, days
                if not skipped.empty:
                    skipped["timeframe"], skipped["sample_split"], skipped["max_hold_bars"], skipped["max_hold_days"] = timeframe, split, bars, days
                    portfolio_skips.append(skipped)
                portfolio_trades.append(closed); portfolio_curves.append(curve)
                portfolio_summaries.append(portfolio_stats(timeframe, split, bars, days, args.initial_capital, closed, curve, ihsg))
    pd.concat(portfolio_trades, ignore_index=True).to_csv(out / "portfolio_trades.csv", index=False) if portfolio_trades else pd.DataFrame().to_csv(out / "portfolio_trades.csv", index=False)
    pd.concat(portfolio_curves, ignore_index=True).to_csv(out / "portfolio_equity_curve.csv", index=False) if portfolio_curves else pd.DataFrame().to_csv(out / "portfolio_equity_curve.csv", index=False)
    pd.concat(portfolio_skips, ignore_index=True).to_csv(out / "portfolio_skipped.csv", index=False) if portfolio_skips else pd.DataFrame().to_csv(out / "portfolio_skipped.csv", index=False)
    pd.DataFrame(portfolio_summaries, columns=PORTFOLIO_SUMMARY_COLUMNS).to_csv(
        out / "portfolio_summary.csv", index=False,
    )
    audits = [selection_audit]
    if args.walk_forward:
        walk_forward, walk_forward_audit = run_walk_forward(trades, stocks, ihsg, start, selection_end, args.initial_capital, args.max_positions, execution)
        walk_forward.to_csv(out / "walk_forward_summary.csv", index=False)
        audits.append(walk_forward_audit)
    pd.concat(audits, ignore_index=True).to_csv(out / "hold_selection_audit.csv", index=False)
    metadata = {"backtest_engine_version": ENGINE_VERSION, "execution": "trigger_aware_cost_aware", "screener_file": args.screener_file, "screener_sha256": hashlib.sha256(Path(args.screener_file).read_bytes()).hexdigest(), "screener_variant_id": getattr(screener, "VARIANT_ID", "V2_FROZEN_BASELINE"), "ready_setup_family": args.ready_setup_family, "start": str(start.date()), "holdout_start": str(holdout.date()), "end": str(end.date()), "fee_pct": args.fee_pct, "slippage_pct": args.slippage_pct, "same_bar_policy": args.same_bar_policy, "initial_capital": args.initial_capital, "max_positions": args.max_positions, "risk_per_trade_pct": args.risk_per_trade_pct, "max_position_pct": args.max_position_pct, "primary_evaluation": "occasional_top_pick_recommendation_runs", "portfolio_candidate_policy": "ready_to_enter_top_pick_triggered", "hold_selection_policy": "secondary_constrained_portfolio_diagnostic", "hold_selection_minimum_trades": HOLD_SELECTION_MIN_TRADES, "hold_selection_minimum_pf": HOLD_SELECTION_MIN_PF, "hold_selection_maximum_drawdown_pct": HOLD_SELECTION_MAX_DRAWDOWN_PCT, "force_close_at_period_end": True, "walk_forward_requested": args.walk_forward, "universe_size_with_data": len(pool), "universe_membership_manifest": str(manifest_path) if manifest_path else None, "universe_membership_applied_per_snapshot": membership_manifest is not None, "market_data_cache": str(cache_path) if (args.use_cache or args.market_data_cache_file) else None, "snapshot_counts": snapshot_counts, "screen_errors": len(errors), "survivorship_bias": True, "universe_status": "IPO_PROXY_OR_HISTORICAL_MANIFEST_APPLIED_REVIEW_REQUIRED" if membership_manifest is not None else "POINT_IN_TIME_UNIVERSE_REQUIRED"}
    (out / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    write_analysis(out / "backtest_analysis.md", policy_summary, metadata, selection_audit)
    write_recommendation_analysis(
        out / "recommendation_analysis.md", recommendation_summary,
        cadence_summary, metadata,
    )
    print(f"V2 backtest completed: {out.resolve()}"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
