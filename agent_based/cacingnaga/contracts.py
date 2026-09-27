"""Typed contracts for the three data layers (Phase 0, work item 3):

1. Immutable facts — calculated/sourced by Python only.
2. Constrained interpretations — returned by agents; numbers/tickers locked.
3. Final decisions — produced by Python policy after validation.

Validation is dependency-free and executable (exit criterion: "invalid states,
missing evidence, and forbidden agent fields are rejected"). All enums follow
the strict reconciliation decisions in Section 4 of the implementation plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .canonical import canonical_hash
from .errors import ContractViolation

# --- Strict enums (Section 4 decisions) -----------------------------------

MARKET_REGIMES = ("BULLISH", "NEUTRAL", "BEARISH")
SWING_ENVIRONMENTS = ("FAVORABLE", "NEUTRAL", "UNFAVORABLE")
CONFIDENCE_BANDS = ("LOW", "MEDIUM", "HIGH")
TRENDS = ("BULLISH", "NEUTRAL", "BEARISH")
MOMENTUMS = ("POSITIVE", "NEUTRAL", "NEGATIVE")
# Python setup classifier owns the canonical label; agents may not invent one.
SETUPS = (
    "BREAKOUT",
    "PULLBACK",
    "REVERSAL",
    "TREND_CONTINUATION",
    "RANGE",
    "FAILED_BREAKOUT",
    "BREAKDOWN",
    "UNKNOWN",
)
FLOW_STATES = ("ACCUMULATION", "NEUTRAL", "DISTRIBUTION", "UNKNOWN")
FLOW_STRENGTHS = ("STRONG", "MEDIUM", "WEAK", "UNKNOWN")
CATALYST_DIRECTIONS = ("POSITIVE", "NEUTRAL", "NEGATIVE", "UNKNOWN")
CATALYST_IMPACTS = ("HIGH", "MEDIUM", "LOW", "UNKNOWN")
CATALYST_FRESHNESS = ("RECENT", "OLD", "UNKNOWN")
CANDIDATE_STATUSES = ("READY", "WAIT", "REJECT")
RUN_STATUSES = ("RUNNING", "COMPLETE", "PARTIAL", "FAILED")
DECISION_OUTCOMES = ("PENDING", "TOP_3", "NO_TRADE", "NOT_EVALUATED")
# Run-level only; never a per-candidate state (Section 4 decision).
NO_TRADE_TOKEN = "NO_TRADE"

# Fields an agent response may never carry (plan §3/§6.4: model-supplied price,
# entry, stop, target, score, rank or ticker mutation are forbidden).
FORBIDDEN_AGENT_FIELDS = frozenset(
    {
        "price",
        "entry",
        "entry_low",
        "entry_high",
        "stop",
        "stop_loss",
        "target",
        "tp1",
        "tp2",
        "score",
        "technical_score",
        "rank",
        "final_status",
    }
)

_MAX_REASONS = 50
_KNOWN_VALUE = "KNOWN"
_MISSING = "UNKNOWN"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractViolation(message)


def _validate_enum(value: str, allowed: tuple[str, ...], label: str) -> None:
    _require(
        isinstance(value, str) and value in allowed,
        f"{label} must be one of {allowed}, got {value!r}",
    )


def _validate_reason_list(items: Any, label: str) -> None:
    _require(
        isinstance(items, list) and all(isinstance(i, str) and i.strip() for i in items),
        f"{label} must be a list of non-empty strings",
    )
    _require(len(items) <= _MAX_REASONS, f"{label} exceeds {_MAX_REASONS} items")


def _validate_band(value: str, label: str) -> None:
    _validate_enum(value, CONFIDENCE_BANDS, label)


def _finite(value: Any, label: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{label} must be numeric")
    number = float(value)
    _require(number == number and number not in (float("inf"), float("-inf")), f"{label} must be finite")
    return number


def format_rupiah_like(value: Any) -> str:
    """Renderer helper: finite numbers as Rp; missing data as a dash."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number != number or number in (float("inf"), float("-inf")):
        return "-"
    return f"Rp {number:,.0f}".replace(",", ".")


def _reject_unknown_keys(cls: type, payload: Mapping[str, Any]) -> None:
    """Serialization guard: unknown (e.g. injected agent) fields are rejected."""
    allowed = {f for f in cls.__dataclass_fields__ if f != "extra"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ContractViolation(
            f"{cls.__name__}: forbidden/unknown fields rejected: {unknown}"
        )


def _from_payload(cls: type, payload: Mapping[str, Any]):
    """Shared reconstruct-with-guard; validates the reconstructed instance."""
    _reject_unknown_keys(cls, payload)
    clean = {}
    for key, value in payload.items():
        if isinstance(value, list):
            value = tuple(value)
        clean[key] = value
    instance = cls(**clean)
    instance.validate()
    return instance


# ===========================================================================
# Layer 1 — immutable facts (Python-owned)
# ===========================================================================


@dataclass(frozen=True)
class Fact:
    """Base for Python-owned fact objects with stable evidence ids."""

    def evidence_id(self, field_name: str) -> str:
        """Stable id ``<class>:<hash12>:<field>`` agents must cite in reasons."""
        payload = canonical_hash(self.payload())[:12]
        return f"{type(self).__name__}:{payload}:{field_name}"

    def payload(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            **{k: v for k, v in self.__dict__.items() if not k.startswith("_")},
        }

    @property
    def fact_hash(self) -> str:
        return canonical_hash(self.payload())


@dataclass(frozen=True)
class MarketFacts(Fact):
    """IHSG snapshot facts. Values come only from Python calculation."""

    as_of: str = ""          # ISO date of last completed candle
    close: float | None = None
    # Contract metadata carried with the fact so round-trips stay verifiable.
    fact_type: str = "MarketFacts"
    fact_version: str = "1"
    market_symbol: str = "^JKSE"
    timezone: str = "Asia/Jakarta"
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    rsi: float | None = None
    atr_pct: float | None = None
    macd_hist: float | None = None
    breadth50: float | None = None      # None => unavailable, stays UNKNOWN
    breadth200: float | None = None
    median_return20: float | None = None
    breadth_available: bool = False     # explicit availability (Section 4)

    def validate(self) -> None:
        _require(bool(self.as_of), "MarketFacts.as_of is required")
        _require(self.fact_type == "MarketFacts", "fact_type must be MarketFacts")
        for name in ("close", "ema20", "ema50", "ema200", "rsi", "atr_pct", "macd_hist"):
            value = getattr(self, name)
            _require(value is None or isinstance(value, (int, float)), f"MarketFacts.{name} must be numeric or None")
        _require(isinstance(self.breadth_available, bool), "breadth_available must be bool")
        # Absence of breadth must be explicit, never inferred as neutral.
        _require(
            self.breadth_available or (self.breadth50 is None and self.breadth200 is None),
            "breadth values present but breadth_available=False; mark data UNKNOWN or set availability",
        )


@dataclass(frozen=True)
class TechnicalFacts(Fact):
    """Per-candidate technical facts. Agents explain these, never replace them."""

    ticker: str = ""
    as_of: str = ""
    fact_type: str = "TechnicalFacts"
    fact_version: str = "1"
    price: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    rsi: float | None = None
    adx: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    macd_hist: float | None = None
    support: float | None = None
    resistance: float | None = None
    prev_high: float | None = None
    relative_volume: float | None = None
    turnover20_idr: float | None = None
    # Canonical Python-owned setup classification (Section 6.4).
    setup: str = "UNKNOWN"

    def validate(self) -> None:
        _require(bool(self.ticker), "TechnicalFacts.ticker is required")
        _require(bool(self.as_of), "TechnicalFacts.as_of is required")
        _require(self.fact_type == "TechnicalFacts", "fact_type must be TechnicalFacts")
        _validate_enum(self.setup, SETUPS, "TechnicalFacts.setup")
        for name in (
            "price", "ema20", "ema50", "ema200", "rsi", "adx", "atr", "atr_pct",
            "macd_hist", "support", "resistance", "prev_high", "relative_volume",
            "turnover20_idr",
        ):
            value = getattr(self, name)
            _require(value is None or isinstance(value, (int, float)), f"TechnicalFacts.{name} must be numeric or None")


@dataclass(frozen=True)
class FlowFacts(Fact):
    """Per-candidate flow facts derived from OHLCV only.

    Foreign/broker flow is unavailable in v1 fixtures; OHLCV-derived flow is an
    *indication*, never proof of bandar/foreign/broker transactions.
    """

    ticker: str = ""
    as_of: str = ""
    fact_type: str = "FlowFacts"
    fact_version: str = "1"
    obv_slope: float | None = None
    mfi: float | None = None
    cmf: float | None = None
    relative_volume: float | None = None
    up_down_volume_ratio: float | None = None
    # Explicit availability per indicator (never silently neutral).
    available_indicators: tuple[str, ...] = ()
    missing_indicators: tuple[str, ...] = ()

    def validate(self) -> None:
        _require(bool(self.ticker), "FlowFacts.ticker is required")
        _require(bool(self.as_of), "FlowFacts.as_of is required")
        _require(self.fact_type == "FlowFacts", "fact_type must be FlowFacts")
        _require(
            not (self.available_indicators and self.missing_indicators
                 and set(self.available_indicators) & set(self.missing_indicators)),
            "an indicator cannot be both available and missing",
        )
        for name in ("obv_slope", "mfi", "cmf", "relative_volume", "up_down_volume_ratio"):
            value = getattr(self, name)
            _require(value is None or isinstance(value, (int, float)), f"FlowFacts.{name} must be numeric or None")


@dataclass(frozen=True)
class CatalystFacts(Fact):
    """v1 default: no validated news source => everything UNKNOWN."""

    ticker: str = ""
    source_available: bool = False
    fact_type: str = "CatalystFacts"
    fact_version: str = "1"

    def validate(self) -> None:
        _require(bool(self.ticker), "CatalystFacts.ticker is required")
        _require(self.fact_type == "CatalystFacts", "fact_type must be CatalystFacts")


@dataclass(frozen=True)
class RiskLevels(Fact):
    """Python-calculated risk plan (Section 6.7). Agents cannot alter these."""

    ticker: str = ""
    fact_type: str = "RiskLevels"
    fact_version: str = "2"
    entry_low: float | None = None
    entry_high: float | None = None
    stop_loss: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    maximum_holding_days: int = 10
    invalidation: str = ""
    risk_per_share: float | None = None
    risk_reward_tp1: float | None = None
    risk_reward_tp2: float | None = None

    def validate(self) -> None:
        _require(bool(self.ticker), "RiskLevels.ticker is required")
        _require(self.fact_type == "RiskLevels", "fact_type must be RiskLevels")
        _require(self.maximum_holding_days > 0, "maximum_holding_days must be positive")
        for name in ("entry_low", "entry_high", "stop_loss", "tp1", "tp2"):
            value = getattr(self, name)
            _require(value is None or float(value) > 0, f"RiskLevels.{name} must be positive or None")
        for name in ("risk_per_share", "risk_reward_tp1", "risk_reward_tp2"):
            value = getattr(self, name)
            _require(value is None or float(value) > 0, f"RiskLevels.{name} must be positive or None")
        if self.entry_low is not None and self.entry_high is not None:
            _require(self.entry_low <= self.entry_high, "entry_low must not exceed entry_high")
        # Long-only ordering rules; None (no plan yet) is allowed for WAIT.
        if self.stop_loss is not None and self.entry_low is not None:
            _require(self.stop_loss < self.entry_low, "stop must be below entry for long-only plans")
        if self.tp1 is not None and self.entry_high is not None:
            _require(self.tp1 > self.entry_high, "TP1 must be above entry for long-only plans")
        if self.tp1 is not None and self.tp2 is not None:
            _require(self.tp1 < self.tp2, "TP1 must be below TP2")


@dataclass(frozen=True)
class CandidateProvenance(Fact):
    """Per-candidate screening provenance and data-quality record."""

    ticker: str = ""
    source: str = "yfinance"
    retrieval_time: str = ""      # timezone-aware ISO instant
    price_basis: str = "auto_adjusted"
    rows: int = 0
    last_completed_candle: str = ""
    quality_warnings: tuple[str, ...] = ()
    fact_type: str = "CandidateProvenance"
    fact_version: str = "1"

    def validate(self) -> None:
        _require(bool(self.ticker), "CandidateProvenance.ticker is required")
        _require(bool(self.retrieval_time), "CandidateProvenance.retrieval_time is required")
        _require(self.rows >= 0, "rows must be non-negative")
        _require(self.fact_type == "CandidateProvenance", "fact_type must be CandidateProvenance")


# ===========================================================================
# Layer 2 — constrained agent interpretations
# ===========================================================================


@dataclass(frozen=True)
class EvidenceRef:
    """Reference to an immutable fact/evidence item by stable id."""

    ref: str = ""

    def validate(self) -> None:
        _require(bool(self.ref and self.ref.strip()), "evidence ref must be non-empty")


class _InterpretationPayload:
    """Serialization helper for agent interpretation contracts."""

    def payload(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


@dataclass(frozen=True)
class MarketInterpretation(_InterpretationPayload):
    """Market Agent output (Section 6.3). No numbers beyond enums/bands."""

    regime: str = "NEUTRAL"
    confidence_band: str = "LOW"
    swing_environment: str = "NEUTRAL"
    secondary_direction: str | None = None   # nuance without breaking the enum
    reasons: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def validate(self) -> None:
        _validate_enum(self.regime, MARKET_REGIMES, "MarketInterpretation.regime")
        _validate_enum(self.swing_environment, SWING_ENVIRONMENTS, "MarketInterpretation.swing_environment")
        _validate_band(self.confidence_band, "MarketInterpretation.confidence_band")
        _validate_reason_list(list(self.reasons), "reasons")
        _validate_reason_list(list(self.risk_flags), "risk_flags")
        _require(
            all(isinstance(r, str) and r for r in self.evidence_refs),
            "evidence_refs must be strings",
        )
        for field_name in self.__dict__:
            _require(
                field_name not in FORBIDDEN_AGENT_FIELDS,
                f"forbidden agent field: {field_name}",
            )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "MarketInterpretation":
        return _from_payload(cls, payload)


@dataclass(frozen=True)
class TechnicalInterpretation(_InterpretationPayload):
    """Technical Agent output (Section 6.4). Cannot replace Python facts."""

    ticker: str = ""
    trend: str = "NEUTRAL"
    setup: str = "UNKNOWN"
    momentum: str = "NEUTRAL"
    confidence_band: str = "LOW"
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    missing_facts: tuple[str, ...] = ()

    def validate(self) -> None:
        _require(bool(self.ticker), "TechnicalInterpretation.ticker is required")
        _validate_enum(self.trend, TRENDS, "TechnicalInterpretation.trend")
        _validate_enum(self.setup, SETUPS, "TechnicalInterpretation.setup")
        _validate_enum(self.momentum, MOMENTUMS, "TechnicalInterpretation.momentum")
        _validate_band(self.confidence_band, "TechnicalInterpretation.confidence_band")
        _validate_reason_list(list(self.reasons), "reasons")
        _validate_reason_list(list(self.risks), "risks")
        for field_name in self.__dict__:
            _require(
                field_name not in FORBIDDEN_AGENT_FIELDS,
                f"forbidden agent field: {field_name}",
            )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TechnicalInterpretation":
        return _from_payload(cls, payload)


@dataclass(frozen=True)
class FlowInterpretation(_InterpretationPayload):
    """Flow Agent output (Section 6.5).

    UNKNOWN is required when directional evidence is unavailable; it is distinct
    from evidence-backed NEUTRAL. Never claims certainty about bandar activity.
    """

    ticker: str = ""
    flow: str = "UNKNOWN"
    strength: str = "UNKNOWN"
    confidence_band: str = "LOW"
    evidence: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    available_indicators: tuple[str, ...] = ()
    missing_indicators: tuple[str, ...] = ()

    def validate(self) -> None:
        _require(bool(self.ticker), "FlowInterpretation.ticker is required")
        _validate_enum(self.flow, FLOW_STATES, "FlowInterpretation.flow")
        _validate_enum(self.strength, FLOW_STRENGTHS, "FlowInterpretation.strength")
        _validate_band(self.confidence_band, "FlowInterpretation.confidence_band")
        _validate_reason_list(list(self.evidence), "evidence")
        _validate_reason_list(list(self.risks), "risks")
        # UNKNOWN flow with HIGH confidence is a reliability violation.
        _require(
            not (self.flow == "UNKNOWN" and self.confidence_band == "HIGH"),
            "UNKNOWN flow cannot carry HIGH confidence",
        )
        for field_name in self.__dict__:
            _require(
                field_name not in FORBIDDEN_AGENT_FIELDS,
                f"forbidden agent field: {field_name}",
            )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FlowInterpretation":
        return _from_payload(cls, payload)


@dataclass(frozen=True)
class CatalystInterpretation(_InterpretationPayload):
    """Catalyst assessment (Section 6.6). Defaults to UNKNOWN when unchecked."""

    ticker: str = ""
    catalyst: str = "UNKNOWN"
    impact: str = "UNKNOWN"
    freshness: str = "UNKNOWN"
    reasons: tuple[str, ...] = ()

    def validate(self) -> None:
        _require(bool(self.ticker), "CatalystInterpretation.ticker is required")
        _validate_enum(self.catalyst, CATALYST_DIRECTIONS, "CatalystInterpretation.catalyst")
        _validate_enum(self.impact, CATALYST_IMPACTS, "CatalystInterpretation.impact")
        _validate_enum(self.freshness, CATALYST_FRESHNESS, "CatalystInterpretation.freshness")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CatalystInterpretation":
        return _from_payload(cls, payload)


@dataclass(frozen=True)
class DecisionInterpretation(_InterpretationPayload):
    """Decision Agent output (Phase 4, plan §7 item 5/6).

    The Decision Agent proposes; Python disposes: ``proposed_status`` is one of
    the candidate statuses and is *recorded* as ``agent_proposed_status`` while
    Python derives ``final_status`` from immutable facts, hard gates, and the
    conflict rules. A proposal to change Python's derived status must explain
    itself (``status_change_reason``) and cite evidence.
    """

    ticker: str = ""
    proposed_status: str = "WAIT"
    confidence_band: str = "LOW"
    status_change_reason: str = ""      # required when downgrading Python's status
    reasons: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def validate(self) -> None:
        _require(bool(self.ticker), "DecisionInterpretation.ticker is required")
        _validate_enum(self.proposed_status, CANDIDATE_STATUSES, "DecisionInterpretation.proposed_status")
        _validate_band(self.confidence_band, "DecisionInterpretation.confidence_band")
        _validate_reason_list(list(self.reasons), "reasons")
        _validate_reason_list(list(self.concerns), "concerns")
        _require(
            all(isinstance(r, str) and r for r in self.evidence_refs)
            and len(self.evidence_refs) >= 1,
            "DecisionInterpretation must cite at least one evidence ref",
        )
        for field_name in self.__dict__:
            _require(
                field_name not in FORBIDDEN_AGENT_FIELDS,
                f"forbidden agent field: {field_name}",
            )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DecisionInterpretation":
        return _from_payload(cls, payload)


# ===========================================================================
# Layer 3 — Python-owned final decisions and run result
# ===========================================================================


@dataclass(frozen=True)
class CandidateDecision:
    """Final per-candidate decision (Section 6.8). Python owns status/rank."""

    ticker: str = ""
    agent_proposed_status: str = "WAIT"
    final_status: str = "WAIT"
    confidence_band: str = "LOW"
    score: float = 0.0
    rank: int | None = None
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    risk_plan_ref: str = ""
    challenge_ref: str | None = None

    def validate(self) -> None:
        _require(bool(self.ticker), "CandidateDecision.ticker is required")
        _validate_enum(self.agent_proposed_status, CANDIDATE_STATUSES, "agent_proposed_status")
        _validate_enum(self.final_status, CANDIDATE_STATUSES, "final_status")
        _validate_band(self.confidence_band, "CandidateDecision.confidence_band")
        _finite(self.score, "CandidateDecision.score")
        _validate_reason_list(list(self.reasons), "reasons")
        _validate_reason_list(list(self.risks), "risks")
        _validate_reason_list(list(self.conflicts), "conflicts")
        # READY must carry a Python-validated risk plan reference.
        _require(
            self.final_status != "READY" or bool(self.risk_plan_ref),
            "READY requires risk_plan_ref",
        )

    def payload(self) -> dict[str, Any]:
        """JSON-safe audit payload (tuples become lists via canonicalize)."""
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


@dataclass(frozen=True)
class RunResult:
    """Run envelope + invariants (Sections 6.1 and 6.10)."""

    run_id: str = ""
    analysis_date: str = ""                 # ISO date (Asia/Jakarta)
    as_of: str = ""                         # ISO date of last completed candle
    market: MarketInterpretation = field(default_factory=MarketInterpretation)
    market_facts: MarketFacts = field(default_factory=MarketFacts)
    recommendations: tuple[CandidateDecision, ...] = ()   # final READY only
    wait: tuple[CandidateDecision, ...] = ()
    rejected: tuple[CandidateDecision, ...] = ()          # bounded list
    run_status: str = "COMPLETE"
    decision_outcome: str = "NO_TRADE"
    warnings: tuple[str, ...] = ()
    created_at: str = ""
    completed_at: str = ""
    data_snapshot_hash: str = ""
    config_hash: str = ""
    max_rejected: int = 50

    def validate(self) -> None:
        _require(bool(self.run_id), "RunResult.run_id is required")
        _require(bool(self.analysis_date) and bool(self.as_of), "RunResult dates are required")
        _validate_enum(self.run_status, RUN_STATUSES, "RunResult.run_status")
        _validate_enum(self.decision_outcome, DECISION_OUTCOMES, "RunResult.decision_outcome")
        self.market.validate()
        self.market_facts.validate()
        for decision in (*self.recommendations, *self.wait, *self.rejected):
            decision.validate()
        self._validate_invariants()

    def _validate_invariants(self) -> None:
        # Every evaluated candidate appears exactly once across the three lists.
        all_tickers = [d.ticker for d in (*self.recommendations, *self.wait, *self.rejected)]
        _require(len(all_tickers) == len(set(all_tickers)), "duplicate candidate across outcome lists")

        # recommendations contain only READY, bounded to three, contiguous ranks.
        _require(len(self.recommendations) <= 3, "recommendations must have at most three entries")
        for decision in self.recommendations:
            _require(decision.final_status == "READY", "recommendations may only contain READY candidates")
        ranks = [d.rank for d in self.recommendations]
        _require(
            ranks == list(range(1, len(ranks) + 1)),
            "READY ranks must be unique and contiguous starting at 1",
        )
        for decision in (*self.wait, *self.rejected):
            _require(decision.rank is None, "WAIT/REJECT ranks must be null")
            _require(decision.final_status != "READY", "READY cannot appear in wait/rejected lists")

        # RUNNING maps to PENDING and holds no final recommendations.
        if self.run_status == "RUNNING":
            _require(self.decision_outcome == "PENDING", "RUNNING must map to decision_outcome=PENDING")
            _require(not self.recommendations, "RUNNING runs cannot hold final recommendations")

        # COMPLETE maps only to TOP_3 or NO_TRADE (zero READY => NO_TRADE).
        if self.run_status == "COMPLETE":
            _require(
                self.decision_outcome in ("TOP_3", "NO_TRADE"),
                "COMPLETE must map to TOP_3 or NO_TRADE",
            )
            _require(
                (len(self.recommendations) > 0) == (self.decision_outcome == "TOP_3"),
                "COMPLETE with zero READY candidates must map to NO_TRADE",
            )

        # PARTIAL/FAILED are system outcomes, never NO_TRADE (Section 4).
        if self.run_status in ("PARTIAL", "FAILED"):
            _require(
                self.decision_outcome == "NOT_EVALUATED",
                "PARTIAL/FAILED must map to decision_outcome=NOT_EVALUATED",
            )
            _require(not self.recommendations, "PARTIAL/FAILED runs cannot hold recommendations")

        _require(len(self.rejected) <= self.max_rejected, "rejected list exceeds bound")
