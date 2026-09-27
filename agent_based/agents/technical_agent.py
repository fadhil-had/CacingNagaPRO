"""Technical Agent — interprets per-candidate technical facts (plan §7 Phase 3).

Interprets the Python setup label, structure, momentum, and invalidation
facts. It explains cited levels without returning or changing their values,
and flags missing evidence (``missing_facts``) instead of inventing a setup.
"""
from __future__ import annotations

from typing import Any, Mapping

from cacingnaga.contracts import TechnicalInterpretation
from cacingnaga.errors import ContractViolation
from cacingnaga.transport import TransportConfig

from .base import AgentOutcome, build_agent_prompt, run_with_retries

AGENT_NAME = "TechnicalAgent"

SCHEMA: tuple[tuple[str, str], ...] = (
    ("ticker", "the exact ticker you were given"),
    ("trend", '"BULLISH" | "NEUTRAL" | "BEARISH"'),
    ("setup", "echo the Python setup label; use UNKNOWN when unsupported"),
    ("momentum", '"POSITIVE" | "NEUTRAL" | "NEGATIVE"'),
    ("confidence_band", '"LOW" | "MEDIUM" | "HIGH"'),
    ("reasons", "array of strings citing evidence ids"),
    ("risks", "array of strings citing evidence ids"),
    ("evidence_refs", "array of the evidence ids you cited"),
    ("missing_facts", "array of fact names that were absent or null"),
)


def build_request(candidate: Mapping[str, Any], *, run_id: str = "") -> AgentRequest:
    """Technical Agent sees exactly one candidate slice (facts, not flows)."""
    facts = candidate.get("facts", {})
    slice_ = {"candidates": [{
        "facts": facts,
        "evidence_refs": candidate.get("evidence_refs", {}),
        "evidence_ids": candidate.get("evidence_ids", {}),
    }]}
    return build_agent_prompt(AGENT_NAME, SCHEMA, slice_, ticker=facts.get("ticker"), run_id=run_id)


def build_interpretation(payload: Mapping[str, Any]) -> TechnicalInterpretation:
    return TechnicalInterpretation.from_payload(payload)


def run(
    transport,
    candidate: Mapping[str, Any],
    *,
    config: TransportConfig | None = None,
    run_id: str = "",
) -> AgentOutcome:
    """One bounded Technical Agent turn for one candidate."""
    request = build_request(candidate, run_id=run_id)

    def guarded_builder(payload: Mapping[str, Any]) -> TechnicalInterpretation:
        assert_ticker_matches(candidate, payload)   # ticker mutation rejected
        return build_interpretation(payload)

    return run_with_retries(
        transport,
        request,
        guarded_builder,
        envelope={"candidates": [candidate]},
        config=config,
    )


def assert_ticker_matches(candidate: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    """A malicious response that changes the ticker cannot pass validation."""
    expected = candidate.get("facts", {}).get("ticker", "")
    if payload.get("ticker") != expected:
        raise ContractViolation(
            f"ticker mutation rejected: expected {expected!r}, got {payload.get('ticker')!r}"
        )
