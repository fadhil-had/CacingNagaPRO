"""Phase 2B — offline policy backtest for AI_TEAM_DAILY_V1 (plan §7 Phase 2B).

Replays the deterministic pipeline over historical dates using point-in-time
snapshots and the frozen execution assumptions in ``EXECUTION_POLICY.md``:
next-session entry inside the zone, pessimistic intrabar resolution (stop
before target; gaps fill at the open), two-target accounting, and the 10-day
time stop. WAIT/REJECT are tracked as counterfactuals/diagnostics only — they
never enter performance metrics (plan §7 Phase 7 work item 5 semantics).

This module claims nothing about AI value: it measures the *deterministic*
policy (zero-eligible runs are valid NO_TRADE outcomes).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .canonical import canonical_hash
from .config import AIAnalystConfig
from .contracts import NO_TRADE_TOKEN
from .errors import ContractViolation
from .policy import DecisionPolicy, PolicyOutcome, evaluate_snapshot
from .snapshot import build_snapshot

# Legacy-benchmark cost parity (tests/backtest_engine.py::ExecutionConfig).
DEFAULT_FEE_PCT = 0.20
DEFAULT_SLIPPAGE_PCT = 0.10
LOT_SIZE = 100


@dataclass(frozen=True)
class BacktestConfig:
    """Execution assumptions frozen in EXECUTION_POLICY.md (v1).

    ``accepted_status`` (Phase 7 item 1): which policy status feeds the
    entry/execution simulator instead of hardcoding READY. ``REJECT`` is
    never a legal accepted status (item 5: REJECT is diagnostic only).
    """

    fee_pct: float = DEFAULT_FEE_PCT            # buy+sell, % per side
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT  # % adverse per side
    entry_window: int = 3                       # sessions to fill the zone
    max_holding_days: int = 10                  # sessions (time stop)
    evaluation_days: int = 10                   # MFE/MAE window (post-fill)
    max_positions_per_day: int = 3              # execution capacity
    fee_free_buy_and_hold: bool = True          # benchmark convention
    accepted_status: str = "READY"              # READY | WAIT (item 1)

    def validate(self) -> None:
        if self.fee_pct < 0 or self.slippage_pct < 0:
            raise ContractViolation("backtest costs must be non-negative")
        if self.entry_window < 1 or self.max_holding_days < 1 or self.evaluation_days < 1:
            raise ContractViolation("backtest windows must be positive")
        if self.max_positions_per_day < 1:
            raise ContractViolation("execution capacity must be at least one position per day")
        if self.accepted_status not in ("READY", "WAIT"):
            raise ContractViolation(
                "accepted_status must be READY or WAIT (REJECT is diagnostic only)"
            )


@dataclass
class TradeRecord:
    """One replayed outcome (READY trade, WAIT counterfactual, or REJECT diag).

    Phase 7 item 3 extensions: hit dates for each target/stop, the discrete
    ``actual_result``, and the cost-free ``gross_return_pct`` alongside the
    cost-adjusted net. Benchmark return stays per-position (entry→exit).
    """

    screen_date: str
    ticker: str
    status: str                     # READY / WAIT / REJECT (policy status)
    entry_date: str | None = None
    entry_price: float | None = None
    exit_date: str | None = None
    exit_price: float | None = None
    outcome: str = ""               # EXECUTED_* / NOT_EXECUTED / NOT_TAKEN / DIAGNOSTIC_*
    exit_reason: str = ""
    holding_sessions: int = 0
    net_return_pct: float = float("nan")
    ihsg_return_pct: float = float("nan")
    mfe_pct: float = float("nan")
    mae_pct: float = float("nan")
    gross_return_pct: float = float("nan")   # before costs (raw fill prices)
    tp1_hit_date: str | None = None
    tp2_hit_date: str | None = None
    sl_hit_date: str | None = None
    actual_result: str | None = None          # WIN / LOSS / FLAT (executed only)
    confidence_band: str | None = None        # policy band at signal time
    notes: tuple[str, ...] = ()

    def to_row(self) -> dict[str, Any]:
        """JSON-safe row: non-finite metrics serialize as ``None`` (unknown)."""
        def _num(value: float) -> float | None:
            return value if math.isfinite(value) else None
        return {
            "screen_date": self.screen_date,
            "ticker": self.ticker,
            "status": self.status,
            "outcome": self.outcome,
            "entry_date": self.entry_date,
            "entry_price": self.entry_price,
            "exit_date": self.exit_date,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "holding_sessions": self.holding_sessions,
            "net_return_pct": _num(self.net_return_pct),
            "gross_return_pct": _num(self.gross_return_pct),
            "ihsg_return_pct": _num(self.ihsg_return_pct),
            "mfe_pct": _num(self.mfe_pct),
            "mae_pct": _num(self.mae_pct),
            "tp1_hit_date": self.tp1_hit_date,
            "tp2_hit_date": self.tp2_hit_date,
            "sl_hit_date": self.sl_hit_date,
            "actual_result": self.actual_result,
            "confidence_band": self.confidence_band,
            "notes": "; ".join(self.notes),
        }


class _Fills:
    """Fill helpers mirroring tests/backtest_engine.py (cost parity)."""

    def __init__(self, cfg: BacktestConfig) -> None:
        self._cfg = cfg

    def buy(self, price: float) -> float:
        return price * (1 + self._cfg.slippage_pct / 100)

    def sell(self, price: float) -> float:
        return price * (1 - self._cfg.slippage_pct / 100)

    def net_return(self, entry: float, exit_: float, half_position: bool = False) -> float:
        """Net return over one full or half position, costs on both sides."""
        entry_fee = entry * self._cfg.fee_pct / 100
        exit_fee = exit_ * self._cfg.fee_pct / 100
        if half_position:
            return (exit_ - entry - entry_fee - exit_fee) / entry * 100
        return (exit_ - entry - entry_fee - exit_fee) / entry * 100


def _session_slice(frame: pd.DataFrame, start: pd.Timestamp, sessions: int) -> pd.DataFrame:
    """Strictly-future sessions after ``start`` (T+1..T+sessions), volume>0."""
    future = frame.loc[frame.index > start]
    future = future[future["Volume"] > 0]
    return future.iloc[:sessions]


def _ihsg_return(ihsg: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float:
    try:
        start_close = float(ihsg.loc[ihsg.index <= start, "Close"].iloc[-1])
        end_close = float(ihsg.loc[ihsg.index <= end, "Close"].iloc[-1])
    except (IndexError, KeyError):
        return float("nan")
    if start_close <= 0:
        return float("nan")
    return (end_close / start_close - 1) * 100


def _forward_window(stock: pd.DataFrame, start: pd.Timestamp, sessions: int, *, inclusive: bool) -> pd.DataFrame:
    """Future sessions with volume, optionally including the start session."""
    index = pd.to_datetime(stock.index)
    mask = (index >= start) if inclusive else (index > start)
    window = stock.loc[mask]
    return window[window["Volume"] > 0].iloc[:sessions]


def _simulate_ready(
    ticker: str,
    screen_date: pd.Timestamp,
    stock: pd.DataFrame,
    ihsg: pd.DataFrame,
    levels: Any,
    fills: _Fills,
    cfg: BacktestConfig,
    *,
    status: str = "READY",
    confidence_band: str | None = None,
) -> TradeRecord:
    """Replay one accepted decision under the frozen execution policy.

    ``status`` labels the policy status that produced the trade (READY by
    default; WAIT when ``accepted_status="WAIT"``). Execution rules are
    identical regardless of label (item 4).
    """
    entry_low, entry_high = float(levels.entry_low), float(levels.entry_high)
    stop, tp1, tp2 = float(levels.stop_loss), float(levels.tp1), float(levels.tp2)
    record = TradeRecord(
        screen_date=screen_date.date().isoformat(),
        ticker=ticker,
        status=status,
        outcome="NOT_EXECUTED",
        confidence_band=confidence_band,
    )

    # --- entry: T+1 .. T+entry_window, first close-position inside the zone --
    sessions = _session_slice(stock, screen_date, cfg.entry_window)
    entry_row = None
    for date, bar in sessions.iterrows():
        open_, close = float(bar["Open"]), float(bar["Close"])
        if close <= 0:
            continue
        if open_ <= entry_high:              # no chase above the zone ceiling
            entry_row = (date, open_, close)
            break
    if entry_row is None:
        return record                        # NOT_EXECUTED (unfilled entry)
    entry_date, entry_open, _entry_close = entry_row
    entry_price = fills.buy(entry_open)
    record.entry_date = entry_date.date().isoformat()
    record.entry_price = round(entry_price, 6)

    # --- walk forward from the fill bar: pessimistic intrabar resolution -----
    # EXECUTION_POLICY §2: stop before target; gaps fill at the open; a bar
    # touching both TP1 and TP2 resolves at TP1 only (§2.4), so a full TP2
    # exit requires TP1 to have filled on an *earlier* bar.
    window = _forward_window(stock, entry_date, cfg.evaluation_days, inclusive=True)
    if not len(window):
        record.notes = ("no forward data after fill",)
        return record
    half1_price: float | None = None
    half1_raw: float | None = None
    exit_raw: float | None = None
    exit_date: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason = ""
    holding = 0
    for date, bar in window.iterrows():
        holding += 1
        open_, high, low = float(bar["Open"]), float(bar["High"]), float(bar["Low"])
        if low <= stop:
            # Gap through the stop fills at the open, never below it (§2.2).
            raw = min(open_, stop)
            exit_date, exit_price = date, fills.sell(raw)
            exit_raw = raw
            record.sl_hit_date = date.date().isoformat()
            exit_reason = "STOP"
            record.outcome = (
                "EXECUTED_TP1_THEN_STOP" if half1_price is not None else "EXECUTED_STOP"
            )
            break
        if high >= tp2 and half1_price is not None:
            raw = open_ if open_ > tp2 else tp2
            exit_date, exit_price = date, fills.sell(raw)
            exit_raw = raw
            record.tp2_hit_date = date.date().isoformat()
            exit_reason = "TP2"
            record.outcome = "EXECUTED_TP1_THEN_TP2"
            break
        if high >= tp1 and half1_price is None:
            # §2.4: same-bar TP2 is ignored; partial exit at TP1 (gap-aware).
            raw = open_ if open_ > tp1 else tp1
            half1_price = fills.sell(raw)
            half1_raw = raw
            record.tp1_hit_date = date.date().isoformat()
            record.outcome = "EXECUTED_TP1_PENDING"
    if exit_date is None:
        last_date, last_bar = window.index[-1], window.iloc[-1]
        exit_date = last_date
        exit_raw = float(last_bar["Close"])
        exit_price = fills.sell(exit_raw)
        if half1_price is not None:
            exit_reason = "TP1_THEN_TIME_STOP"
            record.outcome = "EXECUTED_TP1_THEN_TIME"
        else:
            exit_reason = "TIME_STOP"
            record.outcome = "EXECUTED_TIME_STOP"

    record.exit_date = exit_date.date().isoformat()          # type: ignore[union-attr]
    record.exit_price = round(exit_price, 6)                 # type: ignore[arg-type]
    record.exit_reason = exit_reason
    record.holding_sessions = holding

    # --- net return: two-target accounting (§3) ------------------------------
    if half1_price is not None:
        net = (
            fills.net_return(entry_price, half1_price, half_position=True)
            + fills.net_return(entry_price, exit_price, half_position=True)
        ) / 2
    else:
        net = fills.net_return(entry_price, exit_price)
    record.net_return_pct = round(net, 6)

    # --- gross return: same accounting on raw fill prices (before costs) -----
    if half1_raw is not None and exit_raw is not None:
        gross = (
            (half1_raw / entry_price - 1) + (exit_raw / entry_price - 1)
        ) / 2 * 100
    elif exit_raw is not None:
        gross = (exit_raw / entry_price - 1) * 100
    else:
        gross = float("nan")
    record.gross_return_pct = round(gross, 6) if math.isfinite(gross) else float("nan")

    # --- discrete result label for executed trades ---------------------------
    if math.isfinite(net):
        record.actual_result = "WIN" if net > 0 else ("LOSS" if net < 0 else "FLAT")
    record.ihsg_return_pct = round(
        _ihsg_return(ihsg, pd.Timestamp(record.entry_date), pd.Timestamp(record.exit_date)), 6
    )

    # --- MFE/MAE from the actual entry fill (§3 / Phase 7 item 7) ------------
    if len(window):
        mfe = (window["High"].max() / entry_price - 1) * 100
        mae = (window["Low"].min() / entry_price - 1) * 100
        record.mfe_pct, record.mae_pct = round(float(mfe), 6), round(float(mae), 6)
    return record


def _simulate_counterfactual(
    ticker: str,
    screen_date: pd.Timestamp,
    stock: pd.DataFrame,
    ihsg: pd.DataFrame,
    status: str,
    fills: _Fills,
    cfg: BacktestConfig,
    entry_ref: float | None,
    *,
    confidence_band: str | None = None,
) -> TradeRecord:
    """WAIT counterfactual / REJECT diagnostic: next-session close to N-day close.

    Never creates a position; return is diagnostic only (plan §7 Phase 7).
    With ``entry_ref`` (WAIT only) the counterfactual fills at the trigger
    reference level on the first session — the separately labeled WAIT
    confirmation signal (item 5).
    """
    label = "NOT_TAKEN" if status == "WAIT" else "DIAGNOSTIC_REJECT"
    record = TradeRecord(
        screen_date=screen_date.date().isoformat(),
        ticker=ticker,
        status=status,
        outcome=label,
        confidence_band=confidence_band,
    )
    sessions = _session_slice(stock, screen_date, cfg.evaluation_days + 1)
    if len(sessions) < 2:
        record.notes = ("insufficient forward data",)
        return record
    entry_close = float(sessions.iloc[0]["Close"])
    exit_close = float(sessions.iloc[-1]["Close"])
    if entry_close <= 0 or exit_close <= 0:
        record.notes = ("invalid prices",)
        return record
    if status == "WAIT" and entry_ref is not None and entry_ref > 0:
        # Counterfactual trigger: entry at the reference level (prev high /
        # zone floor) on the first session, time-stopped after 10 sessions.
        entry = fills.buy(entry_ref)
        exit_ = fills.sell(exit_close)
        record.entry_date = sessions.index[0].date().isoformat()
        record.entry_price = round(entry, 6)
        record.exit_date = sessions.index[-1].date().isoformat()
        record.exit_price = round(exit_, 6)
        record.holding_sessions = len(sessions) - 1
        record.net_return_pct = round(fills.net_return(entry, exit_), 6)
        record.ihsg_return_pct = round(
            _ihsg_return(ihsg, sessions.index[0], sessions.index[-1]), 6
        )
        return record
    # Plain diagnostic close-to-close return.
    record.entry_date = sessions.index[0].date().isoformat()
    record.entry_price = round(entry_close, 6)
    record.exit_date = sessions.index[-1].date().isoformat()
    record.exit_price = round(exit_close, 6)
    record.holding_sessions = len(sessions) - 1
    record.net_return_pct = round((exit_close / entry_close - 1) * 100, 6)
    record.ihsg_return_pct = round(
        _ihsg_return(ihsg, sessions.index[0], sessions.index[-1]), 6
    )
    return record


def run_offline_backtest(
    ihsg: pd.DataFrame,
    stock_frames: Mapping[str, pd.DataFrame],
    config: AIAnalystConfig,
    backtest: BacktestConfig | None = None,
    *,
    screen_dates: list[str] | None = None,
) -> dict[str, Any]:
    """Walk-forward replay: point-in-time snapshot → policy → simulated trades.

    Deterministic and offline; identical inputs produce identical reports.
    """
    backtest = backtest or BacktestConfig()
    backtest.validate()
    config.validate()
    fills = _Fills(backtest)
    policy = DecisionPolicy(config)

    ihsg_index = pd.to_datetime(ihsg.index)
    if getattr(ihsg, "tz", None) is not None and getattr(ihsg.index, "tz", None) is not None:
        ihsg = ihsg.copy()
        ihsg.index = ihsg_index.tz_localize(None)
    min_history = config.screener.min_history_bars
    last_date = pd.Timestamp(pd.to_datetime(ihsg.index).max())

    if screen_dates is None:
        # Default grid: every completed session after enough warmup history,
        # leaving evaluation_days of forward data for outcome resolution.
        candidates_index = pd.to_datetime(ihsg.index)
        warmup_end = candidates_index[min_history - 1]
        screen_dates = [
            d.date().isoformat()
            for d in candidates_index[candidates_index > warmup_end]
            if (last_date - d).days >= backtest.evaluation_days + 2
        ]

    trades: list[TradeRecord] = []
    runs: list[dict[str, Any]] = []
    for date_str in screen_dates:
        snapshot = build_snapshot(ihsg, stock_frames, config, as_of=date_str)
        run, outcomes = evaluate_snapshot(snapshot, config)
        runs.append(
            {
                "screen_date": date_str,
                "run_id": run.run_id,
                "decision_outcome": run.decision_outcome,
                "ready_count": len(run.recommendations),
                "wait_count": len(run.wait),
                "reject_count": len(run.rejected),
            }
        )
        def _stock_for(ticker: str) -> pd.DataFrame | None:
            frame = stock_frames.get(ticker)
            if frame is None:
                return None
            stock = frame.copy()
            if getattr(stock.index, "tz", None) is not None:
                stock.index = pd.to_datetime(stock.index).tz_localize(None)
            return stock.loc[pd.to_datetime(stock.index) <= pd.Timestamp(date_str)]

        def _levels_for(ticker: str) -> Any | None:
            return next(
                (o.risk_plan.levels for o in outcomes if o.ticker == ticker
                 and o.risk_plan.levels is not None),
                None,
            )

        # Item 1: the accepted status feeds the entry simulator (READY by
        # default; WAIT as an explicit policy experiment). REJECT is never
        # accepted (item 5) — validated in BacktestConfig.
        accepted = (
            run.recommendations if backtest.accepted_status == "READY" else run.wait
        )
        accepted_tickers = {d.ticker for d in accepted}
        for decision in accepted[: backtest.max_positions_per_day]:
            levels = _levels_for(decision.ticker)
            stock = _stock_for(decision.ticker)
            if stock is None or levels is None:
                continue
            trades.append(
                _simulate_ready(
                    decision.ticker, pd.Timestamp(date_str), stock, ihsg, levels,
                    fills, backtest,
                    status=backtest.accepted_status,
                    confidence_band=decision.confidence_band,
                )
            )
        for decision in run.wait:
            if backtest.accepted_status == "WAIT" and decision.ticker in accepted_tickers:
                continue                     # already simulated as the entry trade
            stock = _stock_for(decision.ticker)
            if stock is None:
                continue
            # WAIT confirmation counterfactual: fill at the plan's entry-zone
            # floor when a risk plan exists (item 5, separately labeled).
            outcome = next((o for o in outcomes if o.ticker == decision.ticker), None)
            levels = outcome.risk_plan.levels if outcome is not None else None
            entry_ref = float(levels.entry_low) if levels is not None else None
            trades.append(
                _simulate_counterfactual(
                    decision.ticker, pd.Timestamp(date_str), stock, ihsg, "WAIT",
                    fills, backtest, entry_ref=entry_ref,
                    confidence_band=decision.confidence_band,
                )
            )
        for decision in run.rejected:
            stock = _stock_for(decision.ticker)
            if stock is None:
                continue
            trades.append(
                _simulate_counterfactual(
                    decision.ticker, pd.Timestamp(date_str), stock, ihsg, "REJECT",
                    fills, backtest, entry_ref=None,
                    confidence_band=decision.confidence_band,
                )
            )

    report = _build_report(runs, trades, backtest)
    return {
        "screen_dates": screen_dates,
        "runs": runs,
        "trades": [t.to_row() for t in trades],
        "report": report,
    }


def _build_report(
    runs: list[dict[str, Any]], trades: list[TradeRecord], cfg: BacktestConfig
) -> dict[str, Any]:
    executed = [t for t in trades if t.outcome.startswith("EXECUTED")]
    unfilled = [t for t in trades if t.outcome == "NOT_EXECUTED"]
    wins = [t for t in executed if t.net_return_pct > 0]
    losses = [t for t in executed if t.net_return_pct <= 0]
    returns = [t.net_return_pct for t in executed]
    tp1_hits = [t for t in executed if "TP1" in t.exit_reason or t.outcome == "EXECUTED_TP2"]
    tp2_hits = [t for t in executed if "TP2" in t.outcome or t.exit_reason in ("TP2", "TP2_FULL_GAP")]
    stop_hits = [t for t in executed if "STOP" in t.outcome]
    gross_profit = sum(t.net_return_pct for t in wins)
    gross_loss = abs(sum(t.net_return_pct for t in losses))
    return {
        "total_runs": len(runs),
        "no_trade_runs": sum(1 for r in runs if r["decision_outcome"] == NO_TRADE_TOKEN),
        "top3_runs": sum(1 for r in runs if r["decision_outcome"] == "TOP_3"),
        "executed_trades": len(executed),
        "unfilled_entries": len(unfilled),
        "win_rate": len(wins) / len(executed) if executed else None,
        "average_return_pct": float(np.mean(returns)) if returns else None,
        "median_return_pct": float(np.median(returns)) if returns else None,
        "tp1_hit_rate": len(tp1_hits) / len(executed) if executed else None,
        "tp2_hit_rate": len(tp2_hits) / len(executed) if executed else None,
        "sl_hit_rate": len(stop_hits) / len(executed) if executed else None,
        "average_holding_sessions": float(np.mean([t.holding_sessions for t in executed])) if executed else None,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "wait_counterfactuals": sum(1 for t in trades if t.outcome == "NOT_TAKEN"),
        "reject_diagnostics": sum(1 for t in trades if t.outcome == "DIAGNOSTIC_REJECT"),
        # Phase 7 item 3 extensions.
        "accepted_status": cfg.accepted_status,
        "actual_results": {
            "win": sum(1 for t in executed if t.actual_result == "WIN"),
            "loss": sum(1 for t in executed if t.actual_result == "LOSS"),
            "flat": sum(1 for t in executed if t.actual_result == "FLAT"),
        },
        "average_gross_return_pct": (
            float(np.mean([t.gross_return_pct for t in executed
                           if math.isfinite(t.gross_return_pct)]))
            if any(math.isfinite(t.gross_return_pct) for t in executed) else None
        ),
    }


def render_baseline_report(result: dict[str, Any]) -> str:
    """Deterministic Markdown summary of the offline replay."""
    report = result["report"]
    lines = [
        "# AI_TEAM_DAILY_V1 — Offline Policy Baseline",
        "",
        "> Deterministic-only comparison. No AI value is claimed from this",
        "> report (plan §7 Phase 2B exit criterion).",
        "",
        f"- Screen dates: **{report['total_runs']}** "
        f"(TOP_3: {report['top3_runs']}, NO_TRADE: {report['no_trade_runs']})",
        f"- Executed trades: **{report['executed_trades']}** "
        f"(unfilled entries: {report['unfilled_entries']})",
        f"- Win rate: {report['win_rate'] if report['win_rate'] is not None else '—'}",
        f"- Average net return: {report['average_return_pct']}% | "
        f"Median: {report['median_return_pct']}%",
        f"- TP1 hit rate: {report['tp1_hit_rate']} | TP2: {report['tp2_hit_rate']} | "
        f"SL: {report['sl_hit_rate']}",
        f"- Average holding sessions: {report['average_holding_sessions']}",
        f"- Profit factor: {report['profit_factor']}",
        f"- WAIT counterfactuals: {report['wait_counterfactuals']} | "
        f"REJECT diagnostics: {report['reject_diagnostics']}",
        "",
        "## Run outcomes",
        "",
        "| Date | Outcome | READY | WAIT | REJECT |",
        "|---|---|---:|---:|---:|",
    ]
    for r in result["runs"][:50]:
        lines.append(
            f"| {r['screen_date']} | {r['decision_outcome']} | {r['ready_count']} | "
            f"{r['wait_count']} | {r['reject_count']} |"
        )
    return "\n".join(lines) + "\n"
