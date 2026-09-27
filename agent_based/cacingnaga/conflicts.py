"""Phase 4 — deterministic conflict detection (plan §7 Phase 4, work item 2).

Conflict rules are **versioned pure functions** over evidence the run already
holds (agent interpretations + facts + policy outcomes). They never call an
agent, never see the raw provider text, and produce one of three severities:

- ``HARD_INVALIDATION``  — Python evidence directly contradicts the agent's
  claim; the candidate cannot become READY this run.
- ``MATERIAL_CONFLICT``  — two evidence-backed readings disagree in a way that
  changes what a trader would do; triggers the Phase 6 challenge flow and
  caps the candidate at WAIT this run (until a challenge resolves it).
- ``MINOR_CAUTION``      — worth surfacing in reasons; no status effect.

Every detection is deterministic: same inputs ⇒ same
``(conflict_type, severity)`` pairs, so Phase 6 triggers are reproducible and
the Phase 2B replay can trust them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ContractViolation

CONFLICT_RULES_VERSION = "CONFLICT_RULES_1"

SEVERITY_HARD = "HARD_INVALIDATION"
SEVERITY_MATERIAL = "MATERIAL_CONFLICT"
SEVERITY_MINOR = "MINOR_CAUTION"

# Status effects implied by severity (preconfigured, not agent-chosen).
SEVERITY_STATUS_EFFECT = {
    SEVERITY_HARD: "BLOCK_READY",
    SEVERITY_MATERIAL: "CAP_AT_WAIT",
    SEVERITY_MINOR: "NONE",
}


@dataclass(frozen=True)
class Conflict:
    """One detected conflict, audit-ready and versioned."""

    rule_id: str
    severity: str
    conflict_type: str          # e.g. "TECHNICAL_VS_FLOW", "MARKET_VS_CANDIDATE"
    ticker: str
    description: str
    evidence_refs: tuple[str, ...] = ()
    status_effect: str = "NONE"

    def payload(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "conflict_type": self.conflict_type,
            "ticker": self.ticker,
            "description": self.description,
            "evidence_refs": list(self.evidence_refs),
            "status_effect": self.status_effect,
            "rules_version": CONFLICT_RULES_VERSION,
        }


def _safe(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def detect_conflicts(
    ticker: str,
    *,
    facts: Any,
    flow_facts: Any,
    technical: Any | None,
    flow: Any | None,
    market: Any | None,
) -> tuple[Conflict, ...]:
    """Run the versioned rule set for one candidate.

    All inputs are validated contract objects or facts; ``None`` interpretations
    (failed agent turn) simply skip the rules that need them — a failed agent
    is handled by the runner's PARTIAL path, not by conflict logic.
    """
    conflicts: list[Conflict] = []
    refs = getattr(flow_facts, "payload", lambda: {})()
    fact_refs = getattr(facts, "payload", lambda: {})()

    # --- R1: technical-bullish vs flow-distribution (the canonical conflict) -
    # PRD §6.5: price structure says up, volume flow says distribution. This
    # is exactly the CUAN fixture shape.
    if (
        technical is not None
        and flow is not None
        and technical.trend == "BULLISH"
        and flow.flow == "DISTRIBUTION"
    ):
        mfi = _safe(getattr(flow_facts, "mfi", None))
        cmf = _safe(getattr(flow_facts, "cmf", None))
        ema20_ref = f"TechnicalFacts:{getattr(facts, 'fact_hash', '')[:12]}:ema20"
        mfi_ref = f"FlowFacts:{getattr(flow_facts, 'fact_hash', '')[:12]}:mfi"
        conflicts.append(
            Conflict(
                rule_id="R1",
                severity=SEVERITY_MATERIAL,
                conflict_type="TECHNICAL_VS_FLOW",
                ticker=ticker,
                description=(
                    "Technical structure is bullish while volume flow reads "
                    f"distribution (MFI={mfi}, CMF={cmf}); the swing entry "
                    "carries distribution risk"
                ),
                evidence_refs=(ema20_ref, mfi_ref),
                status_effect=SEVERITY_STATUS_EFFECT[SEVERITY_MATERIAL],
            )
        )

    # --- R2: bullish-structure claim vs missing/invalid risk plan ------------
    # A bullish trend with no buildable long plan cannot back READY.
    if technical is not None and technical.trend == "BULLISH":
        ema20 = _safe(getattr(facts, "ema20", None))
        ema50 = _safe(getattr(facts, "ema50", None))
        price = _safe(getattr(facts, "price", None))
        if ema20 is None or ema50 is None or price is None:
            conflicts.append(
                Conflict(
                    rule_id="R2",
                    severity=SEVERITY_HARD,
                    conflict_type="CLAIM_VS_MISSING_EVIDENCE",
                    ticker=ticker,
                    description="bullish claim but core structure facts are missing",
                    status_effect=SEVERITY_STATUS_EFFECT[SEVERITY_HARD],
                )
            )

    # --- R3: bearish market vs bullish candidate (HARD) ----------------------
    if (
        market is not None
        and technical is not None
        and market.regime == "BEARISH"
        and technical.trend == "BULLISH"
    ):
        conflicts.append(
            Conflict(
                rule_id="R3",
                severity=SEVERITY_HARD,
                conflict_type="MARKET_VS_CANDIDATE",
                ticker=ticker,
                description="market regime is BEARISH while the candidate reads bullish; "
                "long entries against a bearish regime are invalid this run",
                status_effect=SEVERITY_STATUS_EFFECT[SEVERITY_HARD],
            )
        )

    # --- R4: unsupported-strength claims (MINOR) ------------------------------
    # e.g. ACCUMULATION claimed with no directional flow evidence available.
    if flow is not None and flow.flow == "ACCUMULATION":
        available = set(getattr(flow_facts, "available_indicators", ()) or ())
        directional = available & {"mfi", "cmf", "obv_slope", "up_down_volume_ratio"}
        if not directional:
            conflicts.append(
                Conflict(
                    rule_id="R4",
                    severity=SEVERITY_MINOR,
                    conflict_type="STRENGTH_VS_EVIDENCE",
                    ticker=ticker,
                    description="accumulation claimed without directional flow evidence",
                    status_effect=SEVERITY_STATUS_EFFECT[SEVERITY_MINOR],
                )
            )

    return tuple(conflicts)


def merge_severity(conflicts: tuple[Conflict, ...]) -> str | None:
    """Worst severity among conflicts (HARD > MATERIAL > MINOR); None if clean."""
    if not conflicts:
        return None
    order = {SEVERITY_HARD: 3, SEVERITY_MATERIAL: 2, SEVERITY_MINOR: 1}
    return max(conflicts, key=lambda c: order[c.severity]).severity


def assert_known_severity(severity: str) -> None:
    if severity not in SEVERITY_STATUS_EFFECT:
        raise ContractViolation(f"unknown conflict severity: {severity}")
