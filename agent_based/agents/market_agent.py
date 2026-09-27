"""Market Agent — interprets IHSG regime facts (plan §7 Phase 3).

Interprets regime, momentum, volatility, and breadth facts from the compact
envelope. Unavailable breadth stays UNKNOWN-adjacent: the agent must not treat
absent breadth as evidence-backed neutral, and must cap confidence when
evidence is sparse or stale.
"""
from __future__ import annotations

from typing import Any, Mapping

from cacingnaga.contracts import MarketInterpretation
from cacingnaga.errors import ContractViolation
from cacingnaga.transport import AgentRequest

from .base import (
    AgentOutcome,
    build_agent_prompt,
    run_with_retries,
)
from cacingnaga.transport import TransportConfig

AGENT_NAME = "MarketAgent"

# Expected JSON shape (enforced again by MarketInterpretation.validate()).
SCHEMA: tuple[tuple[str, str], ...] = (
    ("regime", '"BULLISH" | "NEUTRAL" | "BEARISH"'),
    ("confidence_band", '"LOW" | "MEDIUM" | "HIGH"'),
    ("swing_environment", '"FAVORABLE" | "NEUTRAL" | "UNFAVORABLE"'),
    ("secondary_direction", "optional nuance string or null"),
    ("reasons", "array of strings, each citing at least one evidence id"),
    ("risk_flags", "array of strings"),
    ("evidence_refs", "array of the evidence ids you cited"),
)


def build_request(envelope: Mapping[str, Any], *, run_id: str = "") -> AgentRequest:
    """Market Agent sees only the market slice of the envelope."""
    market_slice = {"market": envelope["market"]}
    return build_agent_prompt(AGENT_NAME, SCHEMA, market_slice, run_id=run_id)


def build_interpretation(payload: Mapping[str, Any]) -> MarketInterpretation:
    """Contract-guarded builder: rejects unknown/forbidden keys, then validates."""
    return MarketInterpretation.from_payload(payload)


def run(
    transport,
    envelope: Mapping[str, Any],
    *,
    config: TransportConfig | None = None,
    run_id: str = "",
) -> AgentOutcome:
    """One bounded Market Agent turn over the immutable market facts."""
    request = build_request(envelope, run_id=run_id)
    return run_with_retries(
        transport,
        request,
        build_interpretation,
        envelope=request.envelope,
        config=config,
    )


def assert_market_scope(envelope: Mapping[str, Any]) -> None:
    """Prompt-boundary check: the market slice must not carry candidates."""
    if "candidates" in envelope:
        raise ContractViolation("Market Agent envelope must not contain per-candidate data")
