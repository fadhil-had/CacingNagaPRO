"""Phase 9 — cross-agent consultation (peer review) between analyst turns.

The Phase 3 agents each see exactly one evidence slice, so a Technical reading
and a Flow reading are produced in isolation: neither can notice that the other
one contradicts it. The Phase 4 Decision Agent then sees both, and the Phase 6
debate only fires once Python has already detected a conflict.

This module inserts one bounded **consultation round** between the first analyst
pass and the synthesis step. After the independent pass, the Technical and Flow
agents for the same candidate each take a second turn in which they can see:

- the other agent's validated reading for the *same* candidate,
- the Market Agent's validated reading,

and may confirm, refine, or contradict it. The design keeps every boundary the
pipeline already enforces:

1. **Still interpretation only.** The peer turn answers the *same* contract as
   the first pass, so no LLM computes status, score, price, or rank. Python
   still derives ``final_status`` in ``agents/merge.py``.
2. **Facts stay isolated.** The *prompt* keeps the first pass's slice boundary —
   a Technical agent is shown technical numbers only, a Flow agent flow numbers
   only. What crosses the boundary is the other agent's *claim*, which the peer
   is free to dispute.
3. **One candidate only.** The envelope carries a single candidate, so a peer
   turn can never leak another ticker into the prompt.
4. **Citations stay honest.** The citation check runs against the same evidence
   surface the first pass used (the full candidate slice), so peers may cite the
   ids their own slice already contains while fabricated ids still fail closed.
5. **Bounded and non-blocking.** One extra attempt per agent, under the same
   ``TransportConfig`` budget. A failed consultation keeps the first-pass
   reading rather than degrading the run — a second opinion is an enrichment,
   not a dependency.
6. **Fully auditable.** Both turns are persisted alongside the first pass, so
   the reconciled reading and the change behind it are reconstructable.

The revised readings become the ones synthesis sees, so conflicts, the decision
packet, and the debate all operate on the reconciled view.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping

from cacingnaga.contracts import FlowInterpretation, TechnicalInterpretation
from cacingnaga.errors import ContractViolation
from cacingnaga.transport import TransportConfig

from .base import AgentOutcome, build_agent_prompt, run_with_retries
from .flow_agent import SCHEMA as FLOW_SCHEMA
from .flow_agent import assert_no_bandar_certainty
from .technical_agent import SCHEMA as TECHNICAL_SCHEMA
from .technical_agent import assert_ticker_matches

TECHNICAL_REVIEW_AGENT = "TechnicalAgentPeerReview"
FLOW_REVIEW_AGENT = "FlowAgentPeerReview"

PEER_ROUND_VERSION = "PEER_ROUND_1"

CONSULTATION_INSTRUCTIONS = """You are {agent_name} inside CacingNagaPRO, an \
Indonesian IDX swing-trading analyst team. This is your CONSULTATION turn: you \
already produced a first reading in isolation, and you are now shown what the \
other specialists concluded about the same candidate.

What you may do:
1. Answer with a single JSON object matching the given schema exactly.
2. Confirm your first reading, or revise the parts the peers contradict — but
   only on the strength of the evidence in your own slice.
3. State disagreement plainly in "reasons"/"risks" when you do not share the
   peers' view. A disagreement is a legitimate, useful outcome; a false
   agreement is not.

What you may never do:
1. Never include price, entry, stop, target, score, rank, or final-status
   fields. Those are owned by Python.
2. Never cite an evidence id absent from the envelope; unsupported claims are
   rejected. Peer readings cite ids from this candidate, so you may cite those
   ids, but you may not invent new ones.
3. Never treat a peer's reading as a fact. Peers are other fallible analysts,
   not an additional data source.
4. Never change the ticker, and never introduce a second candidate.
5. Never claim certainty about activity by bandar, foreign investors, or
   brokers; OHLCV-derived flow is an indication only.
6. Keep missing data UNKNOWN; never fabricate evidence to fill a gap.

How your revised reading is used: the reconciled reading feeds Python's
deterministic policy, so a revision can change interpretation only, never the
ownership of any number or final status."""


def technical_slice(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The Technical Agent's own slice — technicals only, mirroring pass one."""
    facts = candidate.get("facts", {})
    return {
        "candidates": [{
            "facts": facts,
            "evidence_refs": candidate.get("evidence_refs", {}),
            "evidence_ids": candidate.get("evidence_ids", {}),
        }]
    }


def flow_slice(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The Flow Agent's own slice — flow only, mirroring pass one."""
    facts = candidate.get("facts", {})
    return {
        "candidates": [{
            "flow": candidate.get("flow", {}),
            "evidence_refs": candidate.get("evidence_refs", {}),
            "evidence_ids": candidate.get("evidence_ids", {}),
        }]
    }


def build_peer_envelope(
    slice_: Mapping[str, Any],
    *,
    own_reading: Mapping[str, Any] | None,
    peer_readings: Mapping[str, Mapping[str, Any]],
    market_reading: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Consultation prompt envelope: own slice plus the peers' claims.

    ``peer_readings`` maps a peer agent name to that peer's *validated
    interpretation payload*. A peer with no validated reading is simply absent,
    which the agent must read as "no view available" rather than as agreement.
    """
    return {
        "peer_round_version": PEER_ROUND_VERSION,
        "candidates": [dict(slice_["candidates"][0])],
        "your_first_reading": dict(own_reading) if own_reading else None,
        "peer_readings": {
            name: dict(reading) for name, reading in peer_readings.items() if reading
        },
        "market_reading": dict(market_reading) if market_reading else None,
        "peer_claim_boundary": (
            "Peer readings are other analysts' claims, not facts. Weigh them "
            "against your own evidence; do not inherit their reasoning."
        ),
    }


def assert_peer_boundary(slice_: Mapping[str, Any], *, agent_name: str) -> None:
    """Prompt-boundary check: one candidate, and no foreign raw fact slice."""
    candidates = slice_.get("candidates", [])
    if len(candidates) != 1:
        raise ContractViolation(
            f"peer-review slice for {agent_name} must carry exactly one candidate, "
            f"got {len(candidates)}"
        )
    row = candidates[0]
    if agent_name == TECHNICAL_REVIEW_AGENT and "flow" in row:
        raise ContractViolation(
            "technical peer-review slice must not leak raw flow facts"
        )
    if agent_name == FLOW_REVIEW_AGENT and "facts" in row:
        raise ContractViolation(
            "flow peer-review slice must not leak raw technical facts"
        )


def _first_reading(outcome: AgentOutcome) -> dict[str, Any] | None:
    if not outcome.ok or outcome.result is None:
        return None
    return dict(outcome.result.interpretation.payload())


def _build_request(
    agent_name: str,
    schema: tuple[tuple[str, str], ...],
    envelope: Mapping[str, Any],
    *,
    ticker: str,
    run_id: str,
):
    request = build_agent_prompt(agent_name, schema, envelope, ticker=ticker, run_id=run_id)
    return dataclasses.replace(
        request,
        system_instructions=CONSULTATION_INSTRUCTIONS.format(agent_name=agent_name),
    )


def _run_review(
    transport,
    *,
    agent_name: str,
    schema: tuple[tuple[str, str], ...],
    slice_factory,
    builder,
    candidate: Mapping[str, Any],
    own_reading: Mapping[str, Any] | None,
    peer_readings: Mapping[str, Mapping[str, Any]],
    market_reading: Mapping[str, Any] | None,
    config: TransportConfig | None,
    run_id: str,
) -> AgentOutcome:
    """Shared plumbing for both consultation turns.

    The *prompt* carries the agent's own slice plus peer claims; the
    *validation* envelope carries the full candidate slice, exactly as the first
    pass did, so the citation surface neither widens nor narrows.
    """
    slice_ = slice_factory(candidate)
    assert_peer_boundary(slice_, agent_name=agent_name)
    envelope = build_peer_envelope(
        slice_,
        own_reading=own_reading,
        peer_readings=peer_readings,
        market_reading=market_reading,
    )
    request = _build_request(
        agent_name, schema, envelope,
        ticker=candidate.get("facts", {}).get("ticker", ""),
        run_id=run_id,
    )
    return run_with_retries(
        transport,
        request,
        builder,
        envelope={"candidates": [dict(candidate)]},
        config=config,
    )


def run_technical_review(
    transport,
    candidate: Mapping[str, Any],
    *,
    own_reading: Mapping[str, Any] | None = None,
    peer_readings: Mapping[str, Mapping[str, Any]] | None = None,
    market_reading: Mapping[str, Any] | None = None,
    config: TransportConfig | None = None,
    run_id: str = "",
) -> AgentOutcome:
    """One bounded consultation turn for the Technical Agent."""

    def guarded_builder(payload: Mapping[str, Any]) -> TechnicalInterpretation:
        assert_ticker_matches(candidate, payload)   # ticker mutation rejected
        return TechnicalInterpretation.from_payload(payload)

    return _run_review(
        transport,
        agent_name=TECHNICAL_REVIEW_AGENT,
        schema=TECHNICAL_SCHEMA,
        slice_factory=technical_slice,
        builder=guarded_builder,
        candidate=candidate,
        own_reading=own_reading,
        peer_readings=peer_readings or {},
        market_reading=market_reading,
        config=config,
        run_id=run_id,
    )


def run_flow_review(
    transport,
    candidate: Mapping[str, Any],
    *,
    own_reading: Mapping[str, Any] | None = None,
    peer_readings: Mapping[str, Mapping[str, Any]] | None = None,
    market_reading: Mapping[str, Any] | None = None,
    config: TransportConfig | None = None,
    run_id: str = "",
) -> AgentOutcome:
    """One bounded consultation turn for the Flow Agent."""

    def guarded_builder(payload: Mapping[str, Any]) -> FlowInterpretation:
        assert_ticker_matches(candidate, payload)   # ticker mutation rejected
        interp = FlowInterpretation.from_payload(payload)
        assert_no_bandar_certainty(tuple(interp.evidence) + tuple(interp.risks))
        return interp

    return _run_review(
        transport,
        agent_name=FLOW_REVIEW_AGENT,
        schema=FLOW_SCHEMA,
        slice_factory=flow_slice,
        builder=guarded_builder,
        candidate=candidate,
        own_reading=own_reading,
        peer_readings=peer_readings or {},
        market_reading=market_reading,
        config=config,
        run_id=run_id,
    )


def consult_candidate(
    transport,
    candidate: Mapping[str, Any],
    *,
    technical: AgentOutcome,
    flow: AgentOutcome,
    market_reading: Mapping[str, Any] | None,
    config: TransportConfig | None = None,
    run_id: str = "",
) -> tuple[AgentOutcome, AgentOutcome, tuple[AgentOutcome, ...]]:
    """Run both consultation turns for one candidate.

    Returns ``(technical_reading, flow_reading, peer_turns)``. The first two are
    the *reconciled* readings: the consultation result when it validated, and
    the untouched first-pass outcome otherwise. A failed or unattempted
    consultation therefore leaves the run's state untouched rather than
    degrading it, while ``peer_turns`` still records what happened.
    """
    if not technical.ok or not flow.ok:
        return technical, flow, (
            _absent_turn(TECHNICAL_REVIEW_AGENT, technical),
            _absent_turn(FLOW_REVIEW_AGENT, flow),
        )

    technical_view = _first_reading(technical)
    flow_view = _first_reading(flow)
    market = dict(market_reading) if market_reading else None

    technical_turn = run_technical_review(
        transport,
        candidate,
        own_reading=technical_view,
        peer_readings={"FlowAgent": flow_view, "MarketAgent": market},
        market_reading=market,
        config=config,
        run_id=run_id,
    )
    flow_turn = run_flow_review(
        transport,
        candidate,
        own_reading=flow_view,
        peer_readings={"TechnicalAgent": technical_view, "MarketAgent": market},
        market_reading=market,
        config=config,
        run_id=run_id,
    )

    return (
        technical_turn if technical_turn.ok else technical,
        flow_turn if flow_turn.ok else flow,
        (technical_turn, flow_turn),
    )


def _absent_turn(agent_name: str, source: AgentOutcome) -> AgentOutcome:
    """A never-attempted consultation turn, recorded as UNAVAILABLE.

    Keeps the audit trail gap-free: the store shows the turn existed and why it
    produced no reading, instead of the round silently disappearing.
    """
    return AgentOutcome(
        agent_name=agent_name,
        ticker=source.ticker,
        status="FAILED",
        error="no consultation turn attempted (first-pass reading unavailable)",
        attempts=0,
    )


__all__ = [
    "CONSULTATION_INSTRUCTIONS",
    "FLOW_REVIEW_AGENT",
    "PEER_ROUND_VERSION",
    "TECHNICAL_REVIEW_AGENT",
    "assert_peer_boundary",
    "build_peer_envelope",
    "consult_candidate",
    "flow_slice",
    "run_flow_review",
    "run_technical_review",
    "technical_slice",
]
