"""Phase 2 — deterministic decision policy (WP-04, plan §7 Phase 2).

Everything here is pure Python over the immutable snapshot: score components
with documented ranges, configurable PRD weights, hard gates, deterministic
tie-breaking, and run finalization that enforces the Section 6.10 invariants.

Design rules (plan §7 Phase 2 exit criteria):
- Agents never need to recompute an indicator, level, score, or status rule.
- Invalid risk plans cannot back a READY decision.
- Weight/unknown-component changes require configuration only.
- Same inputs + configuration produce the same status and ranking.
- The policy can return zero eligible candidates without failure (NO_TRADE).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_hash
from .config import AIAnalystConfig, config_payload
from .contracts import (
    CandidateDecision,
    MarketFacts,
    MarketInterpretation,
    NO_TRADE_TOKEN,
    RiskLevels,
    RunResult,
    TechnicalFacts,
)
from .errors import ContractViolation
from .risk import RiskPlanCalculator, RiskPlanResult


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _safe(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ---------------------------------------------------------------------------
# Score components — each documented, normalized to 0..1
# ---------------------------------------------------------------------------


def technical_component(facts: TechnicalFacts, market: MarketFacts) -> float:
    """0..1: trend structure, momentum (RSI band), ADX strength, location."""
    score = 0.0
    price = _safe(facts.price)
    ema20, ema50, ema200 = _safe(facts.ema20), _safe(facts.ema50), _safe(facts.ema200)
    if price is not None and ema20 is not None and price > ema20:
        score += 0.2
    if ema20 is not None and ema50 is not None and ema20 > ema50:
        score += 0.2
    if ema50 is not None and ema200 is not None and ema50 > ema200:
        score += 0.2
    rsi = _safe(facts.rsi)
    if rsi is not None and 50.0 <= rsi <= 70.0:
        score += 0.2   # healthy momentum band
    elif rsi is not None and 40.0 <= rsi < 50.0:
        score += 0.1   # cooling pullback, not broken
    adx = _safe(facts.adx)
    if adx is not None and adx >= 25.0:
        score += 0.2
    elif adx is not None and adx >= 20.0:
        score += 0.1
    return _clamp01(score)


def flow_component(flow: Any) -> tuple[float, bool]:
    """0..1 directional flow indication + explicit availability flag.

    Unavailable/UNKNOWN evidence scores 0.0 (conservative) and is *recorded*,
    never silently treated as evidence-backed neutral.
    """
    cmf = _safe(getattr(flow, "cmf", None))
    mfi = _safe(getattr(flow, "mfi", None))
    ratio = _safe(getattr(flow, "up_down_volume_ratio", None))
    obv = _safe(getattr(flow, "obv_slope", None))
    if cmf is None and mfi is None and ratio is None and obv is None:
        return 0.0, False
    parts: list[float] = []
    if cmf is not None:
        parts.append(_clamp01((cmf + 0.2) / 0.4))          # ±0.2 CMF band
    if mfi is not None:
        parts.append(_clamp01((mfi - 40.0) / 30.0))        # 40..70 band
    if ratio is not None:
        parts.append(_clamp01((ratio - 0.8) / 0.7))        # 0.8..1.5 band
    if obv is not None:
        parts.append(1.0 if obv > 0 else 0.0)
    return _clamp01(sum(parts) / len(parts)), True


def market_fit_component(facts: MarketFacts) -> tuple[float, bool]:
    """0..1 swing-environment fit from market facts + availability flag."""
    close, ema20 = _safe(facts.close), _safe(facts.ema20)
    ema50, ema200 = _safe(facts.ema50), _safe(facts.ema200)
    if close is None or ema20 is None or ema50 is None or ema200 is None:
        return 0.0, False
    score = 0.0
    if close > ema20:
        score += 0.34
    if ema20 > ema50:
        score += 0.33
    if ema50 > ema200:
        score += 0.33
    return _clamp01(score), True


def risk_reward_component(levels: RiskLevels | None) -> float:
    """0..1 from TP1 reward/risk (1.0R → 0.0, 2.5R+ → 1.0). None → 0."""
    rr = _safe(levels.risk_reward_tp1) if levels is not None else None
    if rr is None:
        return 0.0
    return _clamp01((rr - 1.0) / 1.5)


def liquidity_component(facts: TechnicalFacts) -> float:
    """0..1 from 20-day median turnover (log-scaled, 1bn→~0.35, 50bn→1)."""
    turnover = _safe(facts.turnover20_idr)
    if turnover is None or turnover <= 0:
        return 0.0
    log_t = math.log10(turnover)
    return _clamp01((log_t - 9.0) / (10.7 - 9.0))


# ---------------------------------------------------------------------------
# Candidate policy evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyOutcome:
    """Per-candidate deterministic policy result (audit-grade)."""

    ticker: str
    final_status: str
    agent_proposed_status: str
    score: float
    components: dict[str, float]
    hard_gate_failures: tuple[str, ...]
    reasons: tuple[str, ...]
    risks: tuple[str, ...]
    risk_plan: RiskPlanResult
    confidence_band: str = "LOW"
    challenge_ref: str | None = None      # Phase 6: id of the debate record

    def payload(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "final_status": self.final_status,
            "agent_proposed_status": self.agent_proposed_status,
            "score": self.score,
            "components": dict(self.components),
            "hard_gate_failures": list(self.hard_gate_failures),
            "reasons": list(self.reasons),
            "risks": list(self.risks),
            "risk_plan": self.risk_plan.payload(),
        }


class DecisionPolicy:
    """Deterministic status + ranking policy for ``AI_TEAM_DAILY_V1``."""

    version = "AI_TEAM_DAILY_V1_POLICY_1"

    def __init__(self, config: AIAnalystConfig) -> None:
        config.validate()
        self._config = config
        self._risk = RiskPlanCalculator(config.policy)

    # -- scoring ------------------------------------------------------------

    def _score(
        self,
        facts: TechnicalFacts,
        flow: Any,
        market: MarketFacts,
        risk_plan: RiskPlanResult,
    ) -> tuple[float, dict[str, float], list[str]]:
        w = self._config.policy.weights
        unknown = self._config.policy.unknown_component
        notes: list[str] = []

        tech = technical_component(facts, market)
        flow_score, flow_available = flow_component(flow)
        market_score, market_available = market_fit_component(market)
        rr_score = risk_reward_component(risk_plan.levels)
        liquidity = liquidity_component(facts)
        catalyst_score = 0.0    # v1: no validated news source (policy decision)

        if not flow_available:
            if unknown.mode == "reject":
                notes.append("flow evidence unavailable (unknown-component: reject)")
            else:
                notes.append("flow evidence unavailable; scored conservatively as 0")
        if not market_available:
            notes.append("market facts incomplete; market_fit scored conservatively as 0")
        notes.append("catalyst component UNKNOWN (no validated news source in v1); scored 0")

        components = {
            "technical": tech,
            "flow": flow_score,
            "market_fit": market_score,
            "risk_reward": rr_score,
            "catalyst": catalyst_score,
            "liquidity": liquidity,
        }
        score = (
            w.technical * components["technical"]
            + w.flow * components["flow"]
            + w.market_fit * components["market_fit"]
            + w.risk_reward * components["risk_reward"]
            + w.catalyst * components["catalyst"]
            + w.liquidity * components["liquidity"]
        )
        return _clamp01(score), components, notes

    # -- hard gates ----------------------------------------------------------

    def _hard_gates(
        self,
        facts: TechnicalFacts,
        flow: Any,
        risk_plan: RiskPlanResult,
        score: float,
        components: dict[str, float],
    ) -> tuple[list[str], list[str]]:
        """Return (hard failures, soft risks)."""
        gates = self._config.policy.gates
        failures: list[str] = []
        risks: list[str] = []

        if gates.require_valid_risk_plan and not risk_plan.valid:
            failures.append("risk plan invalid: " + "; ".join(risk_plan.reject_reasons))
        if components["technical"] < gates.min_technical_score:
            failures.append(
                f"technical score {components['technical']:.2f} below gate {gates.min_technical_score}"
            )
        if score < gates.min_composite_score:
            failures.append(
                f"composite score {score:.2f} below gate {gates.min_composite_score}"
            )
        rel_vol = _safe(facts.relative_volume)
        if rel_vol is None or rel_vol < gates.min_relative_volume:
            failures.append(
                f"relative volume {'unavailable' if rel_vol is None else f'{rel_vol:.2f}'} "
                f"below gate {gates.min_relative_volume}"
            )
        adx = _safe(facts.adx)
        if adx is None or adx < gates.min_adx:
            failures.append(f"ADX {'unavailable' if adx is None else f'{adx:.1f}'} below gate {gates.min_adx}")
        rsi = _safe(facts.rsi)
        if rsi is None or not gates.min_rsi <= rsi <= gates.max_rsi:
            failures.append(
                f"RSI {'unavailable' if rsi is None else f'{rsi:.1f}'} outside [{gates.min_rsi}, {gates.max_rsi}]"
            )
        if gates.require_uptrend_structure:
            price, ema20, ema50 = _safe(facts.price), _safe(facts.ema20), _safe(facts.ema50)
            if price is None or ema20 is None or ema50 is None or not price > ema20 > ema50:
                failures.append("uptrend structure gate not met (need close > EMA20 > EMA50)")
        if gates.require_trigger:
            # Deterministic trigger: previous-bar breakout or EMA20 reclaim,
            # both confirmed by volume (plan §3: no trigger ⇒ never READY).
            price = _safe(facts.price)
            prev_high = _safe(facts.prev_high)
            rel_vol = _safe(facts.relative_volume)
            ema20 = _safe(facts.ema20)
            breakout = (
                price is not None and prev_high is not None and price > prev_high
            )
            reclaim = (
                price is not None and ema20 is not None and price > ema20
            )
            confirmed = rel_vol is not None and rel_vol >= gates.trigger_volume_confirm
            if not (breakout and confirmed) and not (reclaim and confirmed):
                failures.append(
                    "trigger gate not met (need breakout or EMA20 reclaim with "
                    f"relative volume >= {gates.trigger_volume_confirm})"
                )

        levels = risk_plan.levels
        if levels is not None:
            rr1 = _safe(levels.risk_reward_tp1)
            if rr1 is not None and rr1 < gates.min_risk_reward_tp1:
                failures.append(f"reward/risk TP1 {rr1:.2f} below gate {gates.min_risk_reward_tp1}")
            if facts.resistance is not None and levels.tp1 is not None and facts.resistance <= levels.tp1:
                risks.append("resistance sits at/below TP1; target may be unreachable before TP1")
        else:
            risks.append("no actionable risk plan; levels unavailable")
        return failures, risks

    # -- per-candidate evaluation --------------------------------------------

    def evaluate(
        self,
        facts: TechnicalFacts,
        flow: Any,
        market: MarketFacts,
        agent_proposed_status: str = "WAIT",
    ) -> PolicyOutcome:
        """Deterministically score, gate, and status one candidate.

        ``agent_proposed_status`` is accepted for contract compatibility but
        never overrides Python's derivation (plan §7 Phase 4 work item 6).
        """
        risk_plan = self._risk.calculate(facts)
        score, components, notes = self._score(facts, flow, market, risk_plan)
        failures, risks = self._hard_gates(facts, flow, risk_plan, score, components)

        if not failures:
            status = "READY"
        elif not risk_plan.valid and facts.setup in ("PULLBACK", "RANGE", "TREND_CONTINUATION"):
            # Broken/unbuildable plan on a recoverable structure: the setup
            # itself is attractive but not actionable now (PRD §9 WAIT).
            status = "WAIT"
        elif risk_plan.valid and score >= 0.85 * self._config.policy.gates.min_composite_score:
            # Near-miss on quality gates: keep for confirmation, do not reject.
            status = "WAIT"
        else:
            status = "REJECT"

        reasons = list(notes)
        if status == "READY":
            reasons.append("all hard gates passed; valid long-only risk plan")
        elif failures:
            reasons.append("hard gate failures: " + "; ".join(failures))

        band = "LOW"
        if status == "READY" and score >= 0.7:
            band = "HIGH"
        elif status == "READY" or score >= 0.5:
            band = "MEDIUM"

        return PolicyOutcome(
            ticker=facts.ticker,
            final_status=status,
            agent_proposed_status=agent_proposed_status,
            score=round(score, 6),
            components={k: round(v, 6) for k, v in components.items()},
            hard_gate_failures=tuple(failures),
            reasons=tuple(reasons),
            risks=tuple(risks),
            risk_plan=risk_plan,
            confidence_band=band,
        )

    # -- run finalization -----------------------------------------------------

    def finalize_run(
        self,
        snapshot: Any,
        outcomes: list[PolicyOutcome],
        *,
        run_id: str,
        market_interpretation: Any | None = None,
    ) -> RunResult:
        """Assemble a validated RunResult from deterministic outcomes.

        Enforces: only READY in recommendations, top-3 by score with
        deterministic tie-breaks, WAIT/REJECT rank=None, and the complete
        Section 6.10 invariant set via ``RunResult.validate``.
        """
        if len({o.ticker for o in outcomes}) != len(outcomes):
            raise ContractViolation("duplicate ticker in policy outcomes")

        ready = [o for o in outcomes if o.final_status == "READY"]
        # Deterministic ordering: score desc, components desc, ticker asc.
        ready.sort(
            key=lambda o: (
                -o.score,
                -o.components["technical"],
                -o.components["risk_reward"],
                o.ticker,
            )
        )
        cap = self._config.policy.max_recommendations
        promoted = ready[:cap]

        def _decision(o: PolicyOutcome, rank: int | None) -> CandidateDecision:
            return CandidateDecision(
                ticker=o.ticker,
                agent_proposed_status=o.agent_proposed_status,
                final_status=o.final_status,
                confidence_band=o.confidence_band,
                score=o.score,
                rank=rank,
                reasons=o.reasons,
                risks=o.risks,
                conflicts=o.hard_gate_failures,
                risk_plan_ref=(
                    f"{RiskPlanCalculator.version}:{o.risk_plan.levels.fact_hash}"
                    if o.risk_plan.levels is not None else ""
                ),
                challenge_ref=o.challenge_ref,
            )

        recommendations = tuple(
            _decision(o, i + 1) for i, o in enumerate(promoted)
        )
        wait = tuple(
            _decision(o, None) for o in outcomes if o.final_status == "WAIT"
        )
        wait = tuple(sorted(wait, key=lambda d: (-d.score, d.ticker)))
        rejected = tuple(
            _decision(o, None) for o in outcomes if o.final_status == "REJECT"
        )[:50]
        rejected = tuple(sorted(rejected, key=lambda d: (-d.score, d.ticker)))

        market_interp = market_interpretation
        if market_interp is None:
            market_interp = MarketInterpretation()

        outcome_token = "TOP_3" if recommendations else NO_TRADE_TOKEN
        result = RunResult(
            run_id=run_id,
            analysis_date=snapshot.analysis_date,
            as_of=snapshot.as_of,
            market=market_interp,
            market_facts=snapshot.market,
            recommendations=recommendations,
            wait=wait,
            rejected=rejected,
            run_status="COMPLETE",
            decision_outcome=outcome_token,
            warnings=snapshot.warnings,
            created_at=snapshot.analysis_date,
            completed_at=snapshot.analysis_date,
            data_snapshot_hash=snapshot.data_snapshot_hash,
            config_hash=canonical_hash(config_payload(self._config)),
        )
        result.validate()
        return result


def evaluate_snapshot(snapshot: Any, config: AIAnalystConfig) -> tuple[RunResult, list[PolicyOutcome]]:
    """Convenience: run the full deterministic policy over a Phase 1 snapshot."""
    policy = DecisionPolicy(config)
    outcomes = [
        policy.evaluate(facts, flow, snapshot.market)
        for facts, flow in zip(snapshot.candidates, snapshot.flows)
    ]
    run = policy.finalize_run(
        snapshot, outcomes, run_id=f"offline-{snapshot.data_snapshot_hash[:12]}"
    )
    return run, outcomes
