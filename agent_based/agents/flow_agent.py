"""Flow Agent — interprets per-candidate flow facts (plan §7 Phase 3).

Interprets OBV, MFI, CMF, relative volume, and other available flow facts.
Returns UNKNOWN for unavailable directional evidence and never treats a
header-only file as a data source. OHLCV-derived flow is an *indication*
only: the agent is prohibited from claiming certainty about actual
transactions by bandar, foreign investors, or brokers.
"""
from __future__ import annotations

from typing import Any, Mapping

from cacingnaga.contracts import FlowInterpretation
from cacingnaga.errors import ContractViolation
from cacingnaga.transport import TransportConfig

from .base import AgentOutcome, build_agent_prompt, run_with_retries

AGENT_NAME = "FlowAgent"

# Assertive claims about real-world transaction actors are forbidden:
# OHLCV flow is an indication, never proof (Section 4 decision). MENTIONING
# the words is fine (agents may say "we cannot know"); asserting that any
# actor IS transacting is not. Matched case-insensitively.
FORBIDDEN_CLAIMS = (
    "bandar confirmed", "confirmed bandar", "bandar is buying", "bandar is selling",
    "bandar was buying", "bandar was selling", "bandar accumulation", "akumulasi bandar",
    "distribusi bandar", "bandar distribution", "proof of bandar", "bandar proof",
    "proof that bandar", "certain that bandar", "definitely bandar",
    "foreign investor confirmed", "confirmed foreign investor", "foreign flow confirmed",
    "proof of foreign", "foreign accumulation confirmed", "foreign distribution confirmed",
    "broker confirmed", "confirmed broker", "proof of broker", "proof that broker",
    "certain that broker", "certain that foreign",
)

SCHEMA: tuple[tuple[str, str], ...] = (
    ("ticker", "the exact ticker you were given"),
    ("flow", '"ACCUMULATION" | "NEUTRAL" | "DISTRIBUTION" | "UNKNOWN"'),
    ("strength", '"STRONG" | "MEDIUM" | "WEAK" | "UNKNOWN"'),
    ("confidence_band", '"LOW" | "MEDIUM" | "HIGH"'),
    ("evidence", "array of strings citing evidence ids"),
    ("risks", "array of strings citing evidence ids"),
    ("evidence_refs", "array of the evidence ids you cited"),
    ("available_indicators", "echo of indicators with values"),
    ("missing_indicators", "echo of indicators that were null/absent"),
)


def build_request(candidate: Mapping[str, Any], *, run_id: str = "") -> AgentRequest:
    """Flow Agent sees exactly one candidate slice (flow, not technicals)."""
    facts = candidate.get("facts", {})
    slice_ = {"candidates": [{
        "flow": candidate.get("flow", {}),
        "evidence_refs": candidate.get("evidence_refs", {}),
        "evidence_ids": candidate.get("evidence_ids", {}),
    }]}
    return build_agent_prompt(AGENT_NAME, SCHEMA, slice_, ticker=facts.get("ticker"), run_id=run_id)


def build_interpretation(payload: Mapping[str, Any]) -> FlowInterpretation:
    return FlowInterpretation.from_payload(payload)


def run(
    transport,
    candidate: Mapping[str, Any],
    *,
    config: TransportConfig | None = None,
    run_id: str = "",
) -> AgentOutcome:
    """One bounded Flow Agent turn for one candidate."""
    request = build_request(candidate, run_id=run_id)

    def guarded_builder(payload: Mapping[str, Any]) -> FlowInterpretation:
        assert_ticker_matches(candidate, payload)   # ticker mutation rejected
        interp = build_interpretation(payload)
        assert_no_bandar_certainty(tuple(interp.evidence) + tuple(interp.risks))
        return interp

    return run_with_retries(
        transport,
        request,
        guarded_builder,
        envelope={"candidates": [candidate]},
        config=config,
    )


def assert_no_bandar_certainty(texts) -> None:
    """Reject prose claiming certainty about bandar/foreign/broker activity."""
    for text in texts:
        lowered = (text or "").lower()
        for claim in FORBIDDEN_CLAIMS:
            if claim in lowered:
                raise ContractViolation(
                    f"prohibited certainty claim about real transactions: {claim!r}"
                )


def assert_ticker_matches(candidate: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    """A malicious response that changes the ticker cannot pass validation."""
    expected = candidate.get("facts", {}).get("ticker", "")
    if payload.get("ticker") != expected:
        raise ContractViolation(
            f"ticker mutation rejected: expected {expected!r}, got {payload.get('ticker')!r}"
        )
