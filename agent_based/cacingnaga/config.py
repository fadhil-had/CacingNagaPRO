"""Immutable configuration schema for the AI_TEAM_DAILY_V1 pipeline.

Phase 0: run-level configuration. Phase 2 adds the deterministic decision
policy configuration — score weights, hard gates, and risk mandates — so
weights/status/rank change through configuration only, never agent code
(plan §7 Phase 2 work items 6–8). The whole config is part of every run's
``config_hash`` so results stay auditable and reproducible.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ContractViolation
from .versioning import PIPELINE_VERSION


@dataclass(frozen=True)
class ScreenerConfig:
    """Deterministic pre-agent screening thresholds (WP-02/WP-03 inputs)."""

    min_turnover_idr: float = 1_000_000_000.0
    min_price_idr: float = 100.0
    max_atr_pct: float = 0.04
    min_history_bars: int = 260


@dataclass(frozen=True)
class RiskPolicyConfig:
    """Long-only risk mandates (plan §7 Phase 2 work items 1–3).

    Sources: PRD §12 (2–5% stop, 3–10% target, ≤10 sessions) and the legacy
    TIMEFRAME_CONFIG daily values (stop_atr 1.5, structural stop buffer).
    """

    min_stop_pct: float = 0.02
    max_stop_pct: float = 0.05
    min_target_pct: float = 0.03
    max_target_pct: float = 0.10
    rr_tp1: float = 1.5          # TP1 at 1.5R (PRD example ≈ 1.4–2R range)
    rr_tp2: float = 2.5          # TP2 at 2.5R
    stop_buffer_pct: float = 0.005
    stop_atr_multiplier: float = 1.5
    entry_max_above_price_pct: float = 0.01
    max_holding_days: int = 10

    def validate(self) -> None:
        for name in ("min_stop_pct", "max_stop_pct", "min_target_pct", "max_target_pct"):
            value = getattr(self, name)
            if not 0 < value < 1:
                raise ContractViolation(f"risk.{name} must be in (0, 1), got {value}")
        if not self.min_stop_pct <= self.max_stop_pct:
            raise ContractViolation("risk.min_stop_pct must not exceed risk.max_stop_pct")
        if not self.min_target_pct <= self.max_target_pct:
            raise ContractViolation("risk.min_target_pct must not exceed risk.max_target_pct")
        # PRD §12 mandates: stop 2–5%, target 3–10%.
        if self.min_stop_pct < 0.02 or self.max_stop_pct > 0.05:
            raise ContractViolation("stop mandate is 2–5% (PRD §12); widen only via a PRD amendment")
        if self.min_target_pct < 0.03 or self.max_target_pct > 0.10:
            raise ContractViolation("target mandate is 3–10% (PRD §12); widen only via a PRD amendment")
        if not 1.0 < self.rr_tp1 < self.rr_tp2:
            raise ContractViolation("risk.rr_tp1/rr_tp2 must satisfy 1 < rr_tp1 < rr_tp2")
        if self.stop_buffer_pct < 0 or self.stop_buffer_pct >= 0.05:
            raise ContractViolation("risk.stop_buffer_pct must be in [0, 0.05)")
        if self.stop_atr_multiplier <= 0 or self.entry_max_above_price_pct < 0:
            raise ContractViolation("risk multipliers must be positive")
        if self.max_holding_days <= 0 or self.max_holding_days > 10:
            raise ContractViolation("max holding period is 10 trading days (PRD §12)")


@dataclass(frozen=True)
class ScoreWeights:
    """PRD §14 ranking weights. Must sum to 1.0 within a float tolerance."""

    technical: float = 0.30
    flow: float = 0.20
    market_fit: float = 0.15
    risk_reward: float = 0.20
    catalyst: float = 0.10
    liquidity: float = 0.05

    def validate(self) -> None:
        names = ("technical", "flow", "market_fit", "risk_reward", "catalyst", "liquidity")
        for name in names:
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ContractViolation(f"weights.{name} must be within [0, 1], got {value}")
        total = sum(getattr(self, n) for n in names)
        if abs(total - 1.0) > 1e-6:
            raise ContractViolation(f"score weights must sum to 1.0 (got {total:.6f})")


@dataclass(frozen=True)
class UnknownComponentPolicy:
    """How unavailable components score (Section 4 decision, item 13).

    ``conservative`` (default) assigns the component's floor value (0.0) and
    records it; ``zero`` is an alias kept explicit; ``reject`` makes any
    missing mandatory component disqualify the candidate from READY. Weights
    are never silently renormalized.
    """

    mode: str = "conservative"       # conservative | reject
    treat_missing_flow_as_zero: bool = True
    treat_missing_catalyst_as_zero: bool = True   # v1 has no news source

    def validate(self) -> None:
        if self.mode not in ("conservative", "reject"):
            raise ContractViolation("unknown_component.mode must be 'conservative' or 'reject'")


@dataclass(frozen=True)
class GatesConfig:
    """Deterministic hard gates for READY eligibility (work item 9)."""

    min_technical_score: float = 0.5      # normalized 0..1
    min_composite_score: float = 0.55     # normalized 0..1
    min_risk_reward_tp1: float = 1.2
    min_relative_volume: float = 0.8
    min_adx: float = 15.0
    require_uptrend_structure: bool = True
    require_valid_risk_plan: bool = True
    # Plan §3 boundary: READY requires a trigger, not just an attractive setup.
    require_trigger: bool = True
    trigger_volume_confirm: float = 1.1
    min_rsi: float = 40.0
    max_rsi: float = 78.0

    def validate(self) -> None:
        for name in ("min_technical_score", "min_composite_score"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ContractViolation(f"gates.{name} must be within [0, 1], got {value}")
        if self.min_risk_reward_tp1 < 1.0:
            raise ContractViolation("gates.min_risk_reward_tp1 must be at least 1.0")
        if self.min_relative_volume < 0 or self.min_adx < 0:
            raise ContractViolation("gate thresholds must be non-negative")
        if self.trigger_volume_confirm < 0:
            raise ContractViolation("gates.trigger_volume_confirm must be non-negative")
        if not 0 < self.min_rsi < self.max_rsi <= 100:
            raise ContractViolation("gates.min_rsi/max_rsi must satisfy 0 < min < max <= 100")


@dataclass(frozen=True)
class PolicyConfig:
    """Decision-policy configuration (Phase 2). Owned by Python only."""

    weights: ScoreWeights = field(default_factory=ScoreWeights)
    risk: RiskPolicyConfig = field(default_factory=RiskPolicyConfig)
    gates: GatesConfig = field(default_factory=GatesConfig)
    unknown_component: UnknownComponentPolicy = field(default_factory=UnknownComponentPolicy)
    max_recommendations: int = 3

    def validate(self) -> None:
        self.weights.validate()
        self.risk.validate()
        self.gates.validate()
        self.unknown_component.validate()
        if self.max_recommendations != 3:
            raise ContractViolation("PRD G3: the daily recommendation cap is three")


@dataclass(frozen=True)
class AIAnalystConfig:
    """Immutable configuration for one analysis run (Phase 0 + Phase 2)."""

    # Policy boundary decision (Section 4): the frozen legacy screener stays
    # FINAL_DAILY_D10_WEEKLY_MONTHLY; this pipeline is versioned separately.
    pipeline_version: str = PIPELINE_VERSION
    # Decision-state semantics decision (Section 4): NO_TRADE is run-level only.
    candidate_states: tuple[str, ...] = ("READY", "WAIT", "REJECT")
    run_no_trade_token: str = "NO_TRADE"
    # Market benchmark decision (Section 4): ^JKSE, Asia/Jakarta.
    market_symbol: str = "^JKSE"
    market_timezone: str = "Asia/Jakarta"
    # Candidate pool decision (Section 4): all eligible candidates, then a cap.
    max_agent_pool_size: int = 30
    # Trade direction decision (Section 4): long-only in v1.
    long_only: bool = True
    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)

    def validate(self) -> None:
        if self.pipeline_version != PIPELINE_VERSION:
            raise ContractViolation(
                f"unsupported pipeline_version: {self.pipeline_version}"
            )
        if self.max_agent_pool_size < 4:
            raise ContractViolation(
                "pre-agent candidate pool must exceed the final recommendation "
                "cap of three (Phase 1 exit criterion)"
            )
        if not self.long_only:
            raise ContractViolation("v1 is long-only; shorts require a PRD amendment")
        if self.min_turnover_idr <= 0 or self.min_price_idr <= 0:
            raise ContractViolation("screener thresholds must be positive")
        self.policy.validate()

    @property
    def min_turnover_idr(self) -> float:
        return self.screener.min_turnover_idr

    @property
    def min_price_idr(self) -> float:
        return self.screener.min_price_idr


def load_config(path: str | Path) -> AIAnalystConfig:
    """Load and validate configuration from a JSON file."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    def build(cls: type, data: dict[str, Any]):
        nested = {
            name: build(type(getattr(cls, name)), data.pop(name))
            for name in list(data)
            if hasattr(cls, name) and isinstance(getattr(cls, name), (ScreenerConfig, RiskPolicyConfig, ScoreWeights, GatesConfig, UnknownComponentPolicy, PolicyConfig))
        }
        return cls(**{**nested, **data})

    config = build(AIAnalystConfig, raw)
    config.validate()
    return config


def config_payload(config: AIAnalystConfig) -> dict[str, Any]:
    """JSON-safe representation used for hashing and audit storage."""
    policy = config.policy
    payload: dict[str, Any] = {
        "pipeline_version": config.pipeline_version,
        "candidate_states": list(config.candidate_states),
        "run_no_trade_token": config.run_no_trade_token,
        "market_symbol": config.market_symbol,
        "market_timezone": config.market_timezone,
        "max_agent_pool_size": config.max_agent_pool_size,
        "long_only": config.long_only,
        "screener": {
            "min_turnover_idr": config.screener.min_turnover_idr,
            "min_price_idr": config.screener.min_price_idr,
            "max_atr_pct": config.screener.max_atr_pct,
            "min_history_bars": config.screener.min_history_bars,
        },
        "policy": {
            "weights": {
                "technical": policy.weights.technical,
                "flow": policy.weights.flow,
                "market_fit": policy.weights.market_fit,
                "risk_reward": policy.weights.risk_reward,
                "catalyst": policy.weights.catalyst,
                "liquidity": policy.weights.liquidity,
            },
            "risk": {
                "min_stop_pct": policy.risk.min_stop_pct,
                "max_stop_pct": policy.risk.max_stop_pct,
                "min_target_pct": policy.risk.min_target_pct,
                "max_target_pct": policy.risk.max_target_pct,
                "rr_tp1": policy.risk.rr_tp1,
                "rr_tp2": policy.risk.rr_tp2,
                "stop_buffer_pct": policy.risk.stop_buffer_pct,
                "stop_atr_multiplier": policy.risk.stop_atr_multiplier,
                "entry_max_above_price_pct": policy.risk.entry_max_above_price_pct,
                "max_holding_days": policy.risk.max_holding_days,
            },
            "gates": {
                "min_technical_score": policy.gates.min_technical_score,
                "min_composite_score": policy.gates.min_composite_score,
                "min_risk_reward_tp1": policy.gates.min_risk_reward_tp1,
                "min_relative_volume": policy.gates.min_relative_volume,
                "min_adx": policy.gates.min_adx,
                "require_uptrend_structure": policy.gates.require_uptrend_structure,
                "require_valid_risk_plan": policy.gates.require_valid_risk_plan,
                "require_trigger": policy.gates.require_trigger,
                "trigger_volume_confirm": policy.gates.trigger_volume_confirm,
                "min_rsi": policy.gates.min_rsi,
                "max_rsi": policy.gates.max_rsi,
            },
            "unknown_component": {
                "mode": policy.unknown_component.mode,
                "treat_missing_flow_as_zero": policy.unknown_component.treat_missing_flow_as_zero,
                "treat_missing_catalyst_as_zero": policy.unknown_component.treat_missing_catalyst_as_zero,
            },
            "max_recommendations": policy.max_recommendations,
        },
    }
    return payload
