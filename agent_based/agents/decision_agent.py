"""Phase 4 — Decision Agent (plan §7 Phase 4, work items 4–5).

Receives a **fixed evidence packet**: the candidate's immutable facts, the
validated Market/Technical/Flow readings, Python's hard-gate results and
deterministic assessment, and the detected conflicts. It may propose one of
the allowed statuses (READY / WAIT / REJECT) — it never computes scores,
prices, or ranks, and Python still derives ``final_status`` (item 6).

A proposal that *changes* Python's derived status must explain itself
(``status_change_reason``) and cite evidence; the merge step (``merge.py``)
veto-reviews every upgrade and applies conflict status effects.
"""
from __future__ import annotations

from typing import Any, Mapping

from cacingnaga.contracts import DecisionInterpretation
from cacingnaga.transport import TransportConfig

from .base import AgentOutcome, build_agent_prompt, run_with_retries

AGENT_NAME = "DecisionAgent"

ALLOWED_PROPOSED_STATUSES = ("READY", "WAIT", "REJECT")

SCHEMA: tuple[tuple[str, str], ...] = (
    ("ticker", "the exact ticker you were given"),
    ("proposed_status", '"READY" | "WAIT" | "REJECT"'),
    ("confidence_band", '"LOW" | "MEDIUM" | "HIGH"'),
    ("status_change_reason", "required when proposing a different status than python_status"),
    ("reasons", "array of strings citing evidence ids"),
    ("concerns", "array of strings citing evidence ids where applicable"),
    ("evidence_refs", "array of the evidence ids you cited"),
)


def build_evidence_packet(
    candidate: Mapping[str, Any],
    *,
    market_reading: Mapping[str, Any] | None,
    technical_reading: Mapping[str, Any] | None,
    flow_reading: Mapping[str, Any] | None,
    policy_assessment: Mapping[str, Any],
    conflicts: tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any]:
    """Fixed evidence packet (work item 4).

    Contains only what the Decision Agent is allowed to see: immutable facts,
    validated agent readings (payloads, not objects), Python's gate results,
    and the versioned conflict records. No raw provider responses, no scores
    it could overwrite (score is included read-only as context).
    """
    facts = candidate.get("facts", {})
    packet: dict[str, Any] = {
        "candidates": [
            {
                "facts": facts,
                "evidence_refs": candidate.get("evidence_refs", {}),
                "evidence_ids": candidate.get("evidence_ids", {}),
            }
        ],
        "market_reading": market_reading,
        "technical_reading": technical_reading,
        "flow_reading": flow_reading,
        "policy": {
            "python_status": policy_assessment.get("final_status", "WAIT"),
            "hard_gate_failures": list(policy_assessment.get("hard_gate_failures", ())),
            "python_reasons": list(policy_assessment.get("reasons", ())),
            "python_risks": list(policy_assessment.get("risks", ())),
        },
        "conflicts": [dict(c) for c in conflicts],
    }
    return packet


def build_request(
    packet: Mapping[str, Any],
    *,
    ticker: str,
    run_id: str = "",
):
    return build_agent_prompt(AGENT_NAME, SCHEMA, packet, ticker=ticker, run_id=run_id)


def build_interpretation(payload: Mapping[str, Any]) -> DecisionInterpretation:
    return DecisionInterpretation.from_payload(payload)


def run(
    transport,
    packet: Mapping[str, Any],
    *,
    ticker: str,
    config: TransportConfig | None = None,
    run_id: str = "",
) -> AgentOutcome:
    """One bounded Decision Agent turn over the fixed evidence packet."""
    request = build_request(packet, ticker=ticker, run_id=run_id)

    def guarded_builder(payload: Mapping[str, Any]) -> DecisionInterpretation:
        expected = packet["candidates"][0]["facts"].get("ticker", "")
        if payload.get("ticker") != expected:
            from cacingnaga.errors import ContractViolation

            raise ContractViolation(
                f"ticker mutation rejected: expected {expected!r}, got {payload.get('ticker')!r}"
            )
        return build_interpretation(payload)

    return run_with_retries(
        transport,
        request,
        guarded_builder,
        envelope=packet,
        config=config,
    )
