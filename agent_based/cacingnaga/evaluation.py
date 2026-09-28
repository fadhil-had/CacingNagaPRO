"""Phase 7 — backtest integration and forward evaluation (plan §7 Phase 7).

This module makes every recommendation **measurable** and compares the
analyst team against deterministic baselines, under rules frozen *before*
any variant is selected (item 12):

- ``EvalCriteria`` pins minimum sample, horizon, costs, and the promotion
  rule; ``assert_frozen`` marks a criterion set frozen and immutable.
- ``SignalRecord`` (item 2) carries policy/agent/challenge/prompt/config
  references next to the trade outcome so ablations can be reproduced
  from storage (exit criterion 3).
- Status semantics stay distinct (item 5/6): READY creates a trade, WAIT
  records ``NOT_EXECUTED`` plus a separately labeled ``WAIT_TRIGGER_CF``
  counterfactual, REJECT is diagnostic only, unfilled entry-zone
  recommendations are distinct from rejected or executed trades.
- Ablations (item 9) run the same point-in-time pool through reduced
  policies; baselines (item 11) are deterministic; drawdown (item 10) is
  measured on a documented equal-weight evaluation basket — an evaluation
  construct, never production position sizing (item 10/exit 4).

No AI improvement is claimed anywhere in this module unless the frozen
promotion criterion is met; every report closes with the disclaimer
(item 14).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace as _dc_replace
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .backtest import (
    DEFAULT_FEE_PCT,
    DEFAULT_SLIPPAGE_PCT,
    _Fills,
    _session_slice,
    BacktestConfig,
    TradeRecord,
    _simulate_ready,
)
from .canonical import canonical_hash
from .config import AIAnalystConfig, config_payload
from .contracts import NO_TRADE_TOKEN
from .errors import ContractViolation
from .policy import DecisionPolicy, PolicyOutcome

# ---------------------------------------------------------------------------
# Frozen evaluation criteria (item 12)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalCriteria:
    """Frozen before selecting a winning variant (item 12).

    ``frozen=True`` is an explicit marker: an unfrozen criteria object must
    not be used to promote a variant, and a frozen one cannot be thawed.
    """

    min_sample: int = 30                     # minimum executed trades per arm
    horizon_sessions: int = 10               # evaluation horizon per trade
    fee_pct: float = DEFAULT_FEE_PCT
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT
    promotion_metric: str = "average_net_return_pct"
    promotion_min_edge_pct: float = 0.5      # must beat screener by ≥ this
    promotion_min_samples: int = 30
    frozen: bool = False

    def validate(self) -> None:
        if self.min_sample < 1 or self.promotion_min_samples < 1:
            raise ContractViolation("evaluation sample sizes must be positive")
        if self.horizon_sessions < 1:
            raise ContractViolation("evaluation horizon must be positive")
        if self.fee_pct < 0 or self.slippage_pct < 0:
            raise ContractViolation("evaluation costs must be non-negative")
        if self.promotion_metric not in _PROMOTION_METRICS:
            raise ContractViolation(f"unknown promotion metric: {self.promotion_metric!r}")
        if self.promotion_min_edge_pct < 0:
            raise ContractViolation("promotion edge must be non-negative")

    def assert_frozen(self) -> None:
        self.validate()
        if not self.frozen:
            raise ContractViolation(
                "evaluation criteria must be frozen before promoting a variant"
            )


_PROMOTION_METRICS = ("average_net_return_pct", "profit_factor", "expectancy_per_trade")


# ---------------------------------------------------------------------------
# Signal records with full provenance (item 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalRecord:
    """One recommendation with its full provenance chain (item 2).

    ``refs`` carries policy/agent/challenge/prompt/config/model references
    so the signal can be reconstructed and audited from storage alone.
    """

    run_id: str
    screen_date: str
    ticker: str
    status: str                     # READY / WAIT / REJECT
    score: float
    components: dict[str, float]
    outcome: str                    # TradeRecord outcome label
    policy_ref: str = ""
    agent_refs: tuple[str, ...] = ()
    challenge_ref: str | None = None
    prompt_hash: str = ""
    model_ref: str = ""
    config_hash: str = ""
    trade: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "screen_date": self.screen_date,
            "ticker": self.ticker,
            "status": self.status,
            "score": self.score,
            "components": dict(self.components),
            "outcome": self.outcome,
            "policy_ref": self.policy_ref,
            "agent_refs": list(self.agent_refs),
            "challenge_ref": self.challenge_ref,
            "prompt_hash": self.prompt_hash,
            "model_ref": self.model_ref,
            "config_hash": self.config_hash,
            "trade": dict(self.trade),
            "schema": "SIGNAL_RECORD_1",
        }


def signal_records_from_run(
    run_result: Any,
    outcomes: list[PolicyOutcome],
    *,
    run_id: str,
    config: AIAnalystConfig,
    config_hash: str,
    policy_version: str = "AI_TEAM_DAILY_V1_POLICY_1",
    agents_consulted: tuple[str, ...] = (),
    challenge_refs: Mapping[str, str] | None = None,
    prompt_hash: str = "",
    model_ref: str = "",
    trades_by_ticker: Mapping[str, TradeRecord] | None = None,
) -> list[SignalRecord]:
    """Build provenance-complete signal records from one evaluated run.

    Deterministic and offline — the same inputs produce the same records.
    """
    records: list[SignalRecord] = []
    challenge_refs = challenge_refs or {}
    outcome_by_ticker = {o.ticker: o for o in outcomes}
    for bucket in (run_result.recommendations, run_result.wait, run_result.rejected):
        for decision in bucket:
            outcome = outcome_by_ticker.get(decision.ticker)
            trade = trades_by_ticker.get(decision.ticker) if trades_by_ticker else None
            records.append(
                SignalRecord(
                    run_id=run_id,
                    screen_date=run_result.as_of,
                    ticker=decision.ticker,
                    status=decision.final_status,
                    score=decision.score,
                    components=dict(outcome.components) if outcome else {},
                    outcome=trade.outcome if trade is not None else "",
                    policy_ref=policy_version,
                    agent_refs=tuple(agents_consulted),
                    challenge_ref=challenge_refs.get(decision.ticker),
                    prompt_hash=prompt_hash,
                    model_ref=model_ref,
                    config_hash=config_hash,
                    trade=trade.to_row() if trade is not None else {},
                )
            )
    return records


# ---------------------------------------------------------------------------
# Outcome rules for status buckets (items 5–7 semantics, explicit + testable)
# ---------------------------------------------------------------------------


def ready_trades(outcomes: list[PolicyOutcome], *, accepted_status: str = "READY") -> list[PolicyOutcome]:
    """Candidates eligible to create a trade under the configured policy."""
    return [o for o in outcomes if o.final_status == accepted_status]


def wait_counterfactuals(outcomes: list[PolicyOutcome]) -> list[PolicyOutcome]:
    """WAIT candidates: NOT_EXECUTED execution outcome + labeled trigger CF."""
    return [o for o in outcomes if o.final_status == "WAIT"]


def reject_diagnostics(outcomes: list[PolicyOutcome]) -> list[PolicyOutcome]:
    """REJECT candidates: diagnostic returns only, never performance."""
    return [o for o in outcomes if o.final_status == "REJECT"]


def counterfactual_trigger_return(
    stock: pd.DataFrame,
    screen_date: pd.Timestamp,
    entry_ref: float,
    *,
    evaluation_days: int,
    fills: _Fills,
) -> float:
    """Separately labeled WAIT confirmation: trigger-fill → horizon close.

    ``NaN`` when forward data is insufficient (unknown, not zero).
    """
    sessions = _session_slice(stock, screen_date, evaluation_days + 1)
    if len(sessions) < 2 or entry_ref <= 0:
        return float("nan")
    exit_close = float(sessions.iloc[-1]["Close"])
    if exit_close <= 0:
        return float("nan")
    entry = fills.buy(entry_ref)
    return (fills.sell(exit_close) - entry) / entry * 100


# ---------------------------------------------------------------------------
# Ablations (item 9) — same point-in-time pool, reduced policies
# ---------------------------------------------------------------------------

ABLATION_VARIANTS: tuple[str, ...] = (
    "no_ai",
    "technical_only",
    "technical_flow",
    "technical_flow_market",
    "full_decision",
)


def ablation_weights(variant: str) -> dict[str, float]:
    """Score weights per ablation arm (renormalized over active components)."""
    if variant not in ABLATION_VARIANTS:
        raise ContractViolation(f"unknown ablation variant: {variant!r}")
    base = {
        "technical": 0.30,
        "flow": 0.20,
        "market_fit": 0.15,
        "risk_reward": 0.20,
        "catalyst": 0.10,
        "liquidity": 0.05,
    }
    if variant == "no_ai":
        # Deterministic baseline: gates + risk plan remain; score collapses to
        # the risk-reward/liquidity core with no interpretation components.
        base.update({"technical": 0.0, "flow": 0.0, "market_fit": 0.0,
                     "catalyst": 0.0, "risk_reward": 0.85, "liquidity": 0.15})
        return base
    if variant == "technical_only":
        return {"technical": 1.0, "flow": 0.0, "market_fit": 0.0,
                "risk_reward": 0.0, "catalyst": 0.0, "liquidity": 0.0}
    if variant == "technical_flow":
        return {"technical": 0.6, "flow": 0.4, "market_fit": 0.0,
                "risk_reward": 0.0, "catalyst": 0.0, "liquidity": 0.0}
    if variant == "technical_flow_market":
        return {"technical": 0.5, "flow": 0.3, "market_fit": 0.2,
                "risk_reward": 0.0, "catalyst": 0.0, "liquidity": 0.0}
    return base          # full_decision: PRD weights unchanged


def ablation_config(config: AIAnalystConfig, variant: str) -> AIAnalystConfig:
    """Frozen base config with the ablation arm's score weights."""
    import dataclasses

    weights = ablation_weights(variant)
    policy = _dc_replace(
        config.policy,
        weights=dataclasses.replace(config.policy.weights, **weights),
    )
    return _dc_replace(config, policy=policy)


def run_ablation(
    ihsg: pd.DataFrame,
    stock_frames: Mapping[str, pd.DataFrame],
    config: AIAnalystConfig,
    backtest: BacktestConfig,
    screen_dates: list[str],
    *,
    variant: str,
) -> dict[str, Any]:
    """One ablation arm over the same dates/pool (date-matched, item 9)."""
    from .backtest import run_offline_backtest

    arm_config = ablation_config(config, variant)
    result = run_offline_backtest(
        ihsg, stock_frames, arm_config, backtest, screen_dates=list(screen_dates)
    )
    report = result["report"]
    return {
        "variant": variant,
        "screen_dates": list(screen_dates),
        "report": report,
        "weights": ablation_weights(variant),
    }


def run_ablation_suite(
    ihsg: pd.DataFrame,
    stock_frames: Mapping[str, pd.DataFrame],
    config: AIAnalystConfig,
    backtest: BacktestConfig,
    screen_dates: list[str],
) -> dict[str, Any]:
    """All five date-matched arms over one eligible pool (item 9)."""
    arms = [
        run_ablation(ihsg, stock_frames, config, backtest, screen_dates, variant=v)
        for v in ABLATION_VARIANTS
    ]
    return {
        "variants": ABLATION_VARIANTS,
        "arms": arms,
        "eligible_pool": "same point-in-time pool for every arm (item 9)",
    }


# ---------------------------------------------------------------------------
# Baselines (item 11) — deterministic, reproducible
# ---------------------------------------------------------------------------


def ihsg_baseline(ihsg: pd.DataFrame, screen_dates: list[str], *, horizon: int) -> dict[str, Any]:
    """IHSG close-to-close over each screen-date horizon (deterministic)."""
    returns = []
    index = pd.to_datetime(ihsg.index)
    for date_str in screen_dates:
        start = pd.Timestamp(date_str)
        future = index[index > start]
        if not len(future):
            continue
        end = future[min(horizon, len(future)) - 1]
        try:
            base = float(ihsg.loc[index <= start, "Close"].iloc[-1])
            last = float(ihsg.loc[index <= end, "Close"].iloc[-1])
        except (IndexError, KeyError):
            continue
        if base > 0:
            returns.append((last / base - 1) * 100)
    return {
        "name": "IHSG",
        "n": len(returns),
        "average_return_pct": float(np.mean(returns)) if returns else None,
        "median_return_pct": float(np.median(returns)) if returns else None,
    }


def buy_and_hold_baseline(ihsg: pd.DataFrame, *, screen_dates: list[str]) -> dict[str, Any]:
    """Fee-free buy-and-hold over the full screen span (benchmark convention)."""
    closes = ihsg["Close"].astype(float)
    if not len(closes):
        return {"name": "buy_and_hold", "average_return_pct": None}
    total = (float(closes.iloc[-1]) / float(closes.iloc[0]) - 1) * 100
    return {
        "name": "buy_and_hold",
        "n": 1,
        "average_return_pct": total,
        "fee_free": True,
    }


def seeded_random_baseline(
    eligible_pool_by_date: Mapping[str, list[str]],
    *,
    seed: int = 42,
    picks_per_day: int = 3,
) -> dict[str, Any]:
    """Seeded random eligible-candidate picks (deterministic via the seed)."""
    import random

    rng = random.Random(seed)
    picks: dict[str, list[str]] = {}
    for date_str in sorted(eligible_pool_by_date):
        pool = sorted(eligible_pool_by_date[date_str])
        if pool:
            picks[date_str] = rng.sample(pool, k=min(picks_per_day, len(pool)))
    return {
        "name": f"seeded_random(seed={seed})",
        "seed": seed,
        "picks": picks,
    }


def current_screener_baseline(
    ihsg: pd.DataFrame,
    stock_frames: Mapping[str, pd.DataFrame],
    screen_dates: list[str],
    *,
    min_turnover: float = 1_000_000_000,
    min_price: float = 100.0,
    mode: str = "daily_swing",
    top: int = 3,
) -> dict[str, Any]:
    """The frozen legacy screener run point-in-time over the same dates.

    Reads ``scripts/`` through the frozen adapter; the legacy package is
    never modified. Per-date top picks + per-date horizon returns.
    """
    from .legacy_adapter import get_legacy_ranking

    legacy = get_legacy_ranking()
    base = legacy._BASE
    core = base

    breadth_ok = hasattr(base, "hitung_market_breadth_from_frames")
    prepared_cache: dict[tuple[str, str], Any] = {}
    picks: dict[str, list[str]] = {}
    returns: list[float] = []

    for date_str in screen_dates:
        cutoff = pd.Timestamp(date_str)
        # Point-in-time truncation for every frame (no lookahead).
        ihsg_cut = ihsg.loc[pd.to_datetime(ihsg.index) <= cutoff]
        frames_cut = {
            t: f.loc[pd.to_datetime(f.index) <= cutoff] for t, f in stock_frames.items()
        }
        if ihsg_cut.empty:
            continue
        pool = list(stock_frames)
        breadth = (
            base.hitung_market_breadth_from_frames(frames_cut, pool)
            if breadth_ok else (0, 0, 0)
        )
        ihsg_tf = base.siapkan_data_untuk_timeframe(ihsg_cut, mode)
        regime = legacy.analisa_market_regime(ihsg_cut, mode, breadth)
        candidates: list[dict[str, Any]] = []
        for ticker, frame in frames_cut.items():
            if frame.empty:
                continue
            prepared = prepared_cache.get((ticker, date_str))
            if prepared is None:
                prepared = base.siapkan_data_untuk_timeframe(frame, mode)
                prepared_cache[(ticker, date_str)] = prepared
            liquidity = core.hitung_daily_liquidity(frame)
            candidate = legacy.analisa_saham_confluence(
                ticker, None, ihsg_tf, mode, min_turnover, min_price,
                prepared_tf=prepared, liquidity=liquidity,
            )
            if not candidate.get("error"):
                candidates.append(candidate)
        legacy.finalisasi_score_dan_status(candidates, mode, regime)
        strong = [
            c for c in candidates
            if c.get("status") in (core.STATUS_READY, core.STATUS_WAIT)
        ]
        key = lambda c: (c.get("quality_score", 0), c.get("turnover20", 0) or 0)  # noqa: E731
        top_candidates = sorted(strong, key=key, reverse=True)[:top]
        picks[date_str] = [c["ticker"] for c in top_candidates]

        # Per-pick horizon return from the legacy plan (entry → horizon close).
        index_all = pd.to_datetime(ihsg.index)
        for candidate in top_candidates:
            frame = stock_frames.get(candidate["ticker"])
            if frame is None:
                continue
            index = pd.to_datetime(frame.index)
            future = index[index > cutoff]
            if not len(future):
                continue
            end = future[min(10, len(future)) - 1]
            window = frame.loc[(index > cutoff) & (index <= end)]
            if window.empty:
                continue
            entry_level = float(candidate.get("entry_level") or 0)
            if entry_level <= 0:
                continue
            first = window.iloc[0]
            if float(first["Open"]) > entry_level:
                continue                      # zone ceiling exceeded: no fill
            exit_close = float(window.iloc[-1]["Close"])
            if exit_close <= 0:
                continue
            returns.append((exit_close / entry_level - 1) * 100)

    return {
        "name": "current_screener",
        "picks": picks,
        "n": len(returns),
        "average_return_pct": float(np.mean(returns)) if returns else None,
        "median_return_pct": float(np.median(returns)) if returns else None,
        "status_labels": {"ready": core.STATUS_READY, "wait": core.STATUS_WAIT},
    }


def deterministic_policy_baseline(
    ihsg: pd.DataFrame,
    stock_frames: Mapping[str, pd.DataFrame],
    config: AIAnalystConfig,
    backtest: BacktestConfig,
    screen_dates: list[str],
) -> dict[str, Any]:
    """The deterministic AI_TEAM_DAILY_V1 replay (Phase 2B) as a baseline."""
    from .backtest import run_offline_backtest

    result = run_offline_backtest(
        ihsg, stock_frames, config, backtest, screen_dates=list(screen_dates)
    )
    return {
        "name": "AI_TEAM_DAILY_V1_deterministic",
        "report": result["report"],
        "trades": result["trades"],
    }


# ---------------------------------------------------------------------------
# Metrics (item 8) + equal-weight basket drawdown (item 10)
# ---------------------------------------------------------------------------


def status_rates(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """READY/WAIT/REJECT rates over the replayed runs."""
    ready = sum(r.get("ready_count", 0) for r in runs)
    wait = sum(r.get("wait_count", 0) for r in runs)
    reject = sum(r.get("reject_count", 0) for r in runs)
    total = ready + wait + reject
    return {
        "ready": ready,
        "wait": wait,
        "reject": reject,
        "total_signals": total,
        "ready_rate": ready / total if total else None,
        "wait_rate": wait / total if total else None,
        "reject_rate": reject / total if total else None,
    }


def wait_confirmation_rate(trades: list[TradeRecord]) -> dict[str, Any]:
    """WAIT confirmation: share of trigger counterfactuals reaching TP1."""
    counterfactuals = [t for t in trades if t.outcome == "NOT_TAKEN" and t.status == "WAIT"]
    if not counterfactuals:
        return {"n": 0, "confirmation_rate": None}
    # TP1 level is not carried on counterfactual rows; approximate
    # confirmation as a positive counterfactual return over the horizon.
    confirmed = sum(1 for t in counterfactuals
                    if math.isfinite(t.net_return_pct) and t.net_return_pct > 0)
    return {
        "n": len(counterfactuals),
        "confirmation_rate": confirmed / len(counterfactuals),
    }


def losses_avoided_by_reject(trades: list[TradeRecord]) -> dict[str, Any]:
    """REJECT diagnostics: how much loss the gates avoided (diagnostic only)."""
    diagnostics = [t for t in trades if t.outcome == "DIAGNOSTIC_REJECT"
                   and math.isfinite(t.net_return_pct)]
    if not diagnostics:
        return {"n": 0, "average_reject_return_pct": None, "losses_avoided_pct": 0.0}
    negative = [t for t in diagnostics if t.net_return_pct < 0]
    return {
        "n": len(diagnostics),
        "average_reject_return_pct": float(np.mean([t.net_return_pct for t in diagnostics])),
        "losses_avoided_pct": float(-sum(t.net_return_pct for t in negative)),
    }


def confidence_band_stability(trades: list[TradeRecord]) -> dict[str, Any]:
    """Win rate per confidence band — HIGH must not underperform MEDIUM."""
    by_band: dict[str, list[float]] = {}
    for t in trades:
        band = getattr(t, "confidence_band", None)
        if band is None or not math.isfinite(t.net_return_pct):
            continue
        by_band.setdefault(band, []).append(t.net_return_pct)
    return {
        band: {
            "n": len(values),
            "win_rate": sum(1 for v in values if v > 0) / len(values),
            "average_return_pct": float(np.mean(values)),
        }
        for band, values in sorted(by_band.items())
    }


def overtrading_vs_screener(runs: list[dict[str, Any]], screener_picks: Mapping[str, list[str]]) -> dict[str, Any]:
    """Signal count comparison: AI team vs current screener per date."""
    ai_signals = sum(r.get("ready_count", 0) for r in runs)
    screener_signals = sum(len(v) for v in screener_picks.values())
    return {
        "ai_signals": ai_signals,
        "screener_signals": screener_signals,
        "ratio": ai_signals / screener_signals if screener_signals else None,
    }


def equal_weight_basket_drawdown(trades: list[TradeRecord], *, per_trade_capital: float = 1.0) -> dict[str, Any]:
    """Maximum drawdown on a documented equal-weight evaluation basket.

    Evaluation construct ONLY (item 10): each executed trade risks the same
    notional; the basket equity curve is the chronological sum of trade
    returns. This is not production position sizing or portfolio management.
    """
    executed = sorted(
        (t for t in trades if t.outcome.startswith("EXECUTED")
         and math.isfinite(t.net_return_pct)),
        key=lambda t: (t.exit_date or t.screen_date, t.ticker),
    )
    if not executed:
        return {"n": 0, "max_drawdown_pct": None, "note": "no executed trades"}
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in executed:
        equity += t.net_return_pct / 100 * per_trade_capital
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "n": len(executed),
        "max_drawdown_pct": round(max_dd * 100, 6),
        "construction": (
            "equal-weight notional per executed trade, chronological equity "
            "curve of net returns; evaluation construct only — not production "
            "position sizing or portfolio management"
        ),
    }


# ---------------------------------------------------------------------------
# Promotion gate (item 12) + reproducible evaluation entry point (item 14)
# ---------------------------------------------------------------------------


def promotion_decision(
    criteria: EvalCriteria,
    arm_reports: Mapping[str, Mapping[str, Any]],
    *,
    baseline_metric: float | None,
) -> dict[str, Any]:
    """Apply the frozen promotion rule; ``None`` metrics never promote."""
    criteria.assert_frozen()
    metric = criteria.promotion_metric
    full = arm_reports.get("full_decision")
    if full is None:
        raise ContractViolation("promotion requires the full_decision arm")
    value = full.get(metric)
    result = {
        "metric": metric,
        "full_decision_value": value,
        "baseline_value": baseline_metric,
        "min_edge_pct": criteria.promotion_min_edge_pct,
        "min_samples": criteria.promotion_min_samples,
        "promoted": False,
        "reason": "",
    }
    if value is None or baseline_metric is None:
        result["reason"] = "insufficient data: arm or baseline metric unknown"
        return result
    n = full.get("executed_trades", 0)
    if n < criteria.promotion_min_samples:
        result["reason"] = f"sample {n} below frozen minimum {criteria.promotion_min_samples}"
        return result
    if value - baseline_metric >= criteria.promotion_min_edge_pct:
        result["promoted"] = True
        result["reason"] = (
            f"{metric} edge {round(value - baseline_metric, 4)} meets frozen "
            f"minimum {criteria.promotion_min_edge_pct}"
        )
    else:
        result["reason"] = (
            f"edge {round(value - baseline_metric, 4)} below frozen minimum "
            f"{criteria.promotion_min_edge_pct}"
        )
    return result


def run_evaluation(
    ihsg: pd.DataFrame,
    stock_frames: Mapping[str, pd.DataFrame],
    config: AIAnalystConfig,
    backtest: BacktestConfig | None = None,
    *,
    screen_dates: list[str] | None = None,
    criteria: EvalCriteria | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """Reproducible end-to-end evaluation (item 14).

    One call builds: the deterministic replay, status/outcome metrics, the
    equal-weight basket drawdown, all five ablation arms, the baselines, and
    the promotion decision under the frozen criteria. Identical inputs
    produce byte-identical reports; the report carries the disclaimer.
    """
    backtest = backtest or BacktestConfig()
    backtest.validate()
    config.validate()
    criteria = criteria or EvalCriteria()
    criteria.validate()

    from .backtest import run_offline_backtest

    # --- deterministic replay (the AI_TEAM_DAILY_V1 baseline arm) ------------
    replay = run_offline_backtest(
        ihsg, stock_frames, config, backtest, screen_dates=screen_dates
    )
    dates = list(replay["screen_dates"])

    # Rebuild TradeRecords from stored rows for metric functions.
    from .backtest import TradeRecord as _TR

    def _records(rows: list[dict[str, Any]]) -> list[TradeRecord]:
        out = []
        for row in rows:
            record = _TR(
                screen_date=row["screen_date"], ticker=row["ticker"], status=row["status"]
            )
            for key, value in row.items():
                if key in ("notes",):
                    continue
                if hasattr(record, key) and value is not None:
                    setattr(record, key, value)
            out.append(record)
        return out

    trades = _records(replay["trades"])

    # --- metrics (item 8) -----------------------------------------------------
    rates = status_rates(replay["runs"])
    confirmation = wait_confirmation_rate(trades)
    avoided = losses_avoided_by_reject(trades)
    basket = equal_weight_basket_drawdown(trades)

    # --- ablations (item 9): date-matched arms --------------------------------
    ablations = run_ablation_suite(ihsg, stock_frames, config, backtest, dates)

    # --- baselines (item 11) ----------------------------------------------------
    ihsg_base = ihsg_baseline(ihsg, dates, horizon=backtest.evaluation_days)
    bah = buy_and_hold_baseline(ihsg, screen_dates=dates)
    screener = current_screener_baseline(ihsg, stock_frames, dates)
    random_base = seeded_random_baseline(
        {d: sorted(stock_frames) for d in dates}, seed=seed, picks_per_day=backtest.max_positions_per_day
    )
    policy_base = deterministic_policy_baseline(ihsg, stock_frames, config, backtest, dates)

    # --- promotion (item 12): frozen criteria only ------------------------------
    arm_reports = {
        arm["variant"]: arm["report"] for arm in ablations["arms"]
    }
    screener_metric = screener.get("average_return_pct")
    promotion = promotion_decision(
        criteria, arm_reports, baseline_metric=screener_metric
    )

    report = {
        "schema": "EVALUATION_REPORT_1",
        "pipeline_version": "AI_TEAM_DAILY_V1",
        "criteria": {
            "min_sample": criteria.min_sample,
            "horizon_sessions": criteria.horizon_sessions,
            "fee_pct": criteria.fee_pct,
            "slippage_pct": criteria.slippage_pct,
            "promotion_metric": criteria.promotion_metric,
            "promotion_min_edge_pct": criteria.promotion_min_edge_pct,
            "promotion_min_samples": criteria.promotion_min_samples,
            "frozen": criteria.frozen,
        },
        "backtest": {
            "fee_pct": backtest.fee_pct,
            "slippage_pct": backtest.slippage_pct,
            "entry_window": backtest.entry_window,
            "evaluation_days": backtest.evaluation_days,
            "accepted_status": backtest.accepted_status,
        },
        "status_rates": rates,
        "wait_confirmation": confirmation,
        "losses_avoided_by_reject": avoided,
        "basket_drawdown": basket,
        "ablations": ablations,
        "baselines": {
            "ihsg": ihsg_base,
            "buy_and_hold": bah,
            "seeded_random": random_base,
            "current_screener": screener,
            "deterministic_policy": policy_base,
        },
        "promotion": promotion,
        "config_hash": canonical_hash(config_payload(config)),
        "disclaimer": (
            "Historical results do not guarantee future performance. "
            "No AI improvement is claimed unless the frozen promotion "
            "criterion is met. Evaluation basket drawdown is an evaluation "
            "construct, not production portfolio management."
        ),
    }
    return report


def render_evaluation_report(report: dict[str, Any]) -> str:
    """Deterministic Markdown rendering of the machine-readable report."""
    lines = [
        "# AI_TEAM_DAILY_V1 — Evaluation Report",
        "",
        f"- Status rates: READY {report['status_rates']['ready_rate']} | "
        f"WAIT {report['status_rates']['wait_rate']} | "
        f"REJECT {report['status_rates']['reject_rate']}",
        f"- WAIT confirmation: {report['wait_confirmation']['confirmation_rate']} "
        f"(n={report['wait_confirmation']['n']})",
        f"- Losses avoided by REJECT: "
        f"{report['losses_avoided_by_reject']['losses_avoided_pct']}% "
        f"(n={report['losses_avoided_by_reject']['n']})",
        f"- Basket max drawdown: {report['basket_drawdown']['max_drawdown_pct']}%",
        f"- Promotion: **{'PROMOTED' if report['promotion']['promoted'] else 'NOT PROMOTED'}** "
        f"— {report['promotion']['reason']}",
        "",
        "## Ablation arms (date-matched)",
        "",
        "| Variant | Executed | Win rate | Avg net % |",
        "|---|---:|---|---|",
    ]
    for arm in report["ablations"]["arms"]:
        r = arm["report"]
        lines.append(
            f"| {arm['variant']} | {r['executed_trades']} | "
            f"{r['win_rate']} | {r['average_return_pct']} |"
        )
    lines += [
        "",
        "## Baselines",
        "",
        f"- IHSG ({report['baselines']['ihsg']['n']} windows): "
        f"{report['baselines']['ihsg']['average_return_pct']}%",
        f"- Buy & hold (fee-free): {report['baselines']['buy_and_hold']['average_return_pct']}%",
        f"- Current screener: {report['baselines']['current_screener']['average_return_pct']}% "
        f"(n={report['baselines']['current_screener']['n']})",
        "",
        f"> {report['disclaimer']}",
        "",
    ]
    return "\n".join(lines)
