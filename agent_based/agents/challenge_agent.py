"""Phase 6 — agent turns for the challenge/debate (plan §7 Phase 6, items 2–5).

``run_challenge_turns`` questions each challenged agent once (bounded);
``run_resolution_turn`` asks the Decision Agent to classify the debate. Both
run through the Phase 3 validation pipeline, so hostile responses (injected
prices/scores/tickers, fabricated evidence ids, missing citations) fail
bounded and land as UNRESOLVED — storytelling cannot win a debate.

Python owns the status effect: the Decision Agent only *classifies* the
debate; ``resolution_effect`` maps that classification to the preconfigured
effect, and the orchestrator stores both for audit.
"""
from __future__ import annotations

from typing import Any, Mapping

from cacingnaga.challenges import (
    CHALLENGE_RULES_VERSION,
    build_challenge_packet,
    resolution_effect,
)
from cacingnaga.conflicts import Conflict
from cacingnaga.contracts import ChallengeResponse, ChallengeResolution
from cacingnaga.errors import ContractViolation
from cacingnaga.transport import TransportConfig

from .base import AgentOutcome, build_agent_prompt, run_with_retries

CHALLENGE_AGENT_NAME = "ChallengeAgent"
RESOLUTION_AGENT_NAME = "DecisionAgent"          # persistence identity
# Transport identity of the resolution turn: the same Decision Agent in a
# different turn, but a distinct request name so transports (and tests) can
# route debate resolutions independently of the Phase 4 proposal turn.
RESOLUTION_REQUEST_AGENT = "DecisionAgentResolution"

RESPONSE_SCHEMA: tuple[tuple[str, str], ...] = (
    ("agent_name", "the agent name you were given"),
    ("ticker", "the exact ticker you were given"),
    ("conflict_rule_id", "the exact rule id you were given"),
    ("stance", '"SUPPORT" | "REVISE" | "WITHDRAW"'),
    ("missing_data", "array of data you do not have (required honesty)"),
    ("reasons", "array of strings citing evidence ids"),
    ("evidence_refs", "array of the evidence ids you cited"),
)

RESOLUTION_SCHEMA: tuple[tuple[str, str], ...] = (
    ("ticker", "the exact ticker you were given"),
    ("conflict_rule_id", "the exact rule id you were given"),
    ("resolution", '"CONFIRMED" | "REVISED" | "UNRESOLVED"'),
    ("status_effect", '"CAP_AT_WAIT" | "REJECT" (proposed; Python recomputes)'),
    ("summary", "one-line summary of the debate outcome"),
    ("reasons", "array of strings citing evidence ids"),
    ("evidence_refs", "array of the evidence ids you cited"),
)


def run_challenge_turns(
    transport,
    conflict: Conflict,
    *,
    readings: Mapping[str, Mapping[str, Any]],
    ticker: str,
    run_id: str = "",
    config: TransportConfig | None = None,
) -> tuple[tuple[AgentOutcome, ...], dict[str, Any]]:
    """One bounded debate round for one conflict (items 2–4).

    Each challenged agent gets its own focused packet and must answer in the
    strict ``ChallengeResponse`` schema citing evidence from its own packet.
    Returns ``(agent_outcomes, packet)``; a failed turn is an outcome with
    ``ok=False`` — the debate still happens for the remaining agents.
    """
    packet = build_challenge_packet(conflict, readings=readings)
    outcomes: list[AgentOutcome] = []
    for turn in packet["turns"]:
        request = build_agent_prompt(
            CHALLENGE_AGENT_NAME,
            RESPONSE_SCHEMA,
            turn,
            ticker=ticker,
            run_id=run_id,
        )

        def guarded_builder(
            payload: Mapping[str, Any],
            turn: Mapping[str, Any] = turn,
            ticker: str = ticker,
        ) -> ChallengeResponse:
            expected = turn["agent_name"]
            if payload.get("agent_name") != expected:
                raise ContractViolation(
                    f"agent identity mutation rejected: expected {expected!r}, "
                    f"got {payload.get('agent_name')!r}"
                )
            if payload.get("ticker") != ticker:
                raise ContractViolation(
                    f"ticker mutation rejected: expected {ticker!r}, "
                    f"got {payload.get('ticker')!r}"
                )
            expected_rule = turn["conflict_rule_id"]
            if payload.get("conflict_rule_id") != expected_rule:
                raise ContractViolation(
                    f"rule id mutation rejected: expected {expected_rule!r}, "
                    f"got {payload.get('conflict_rule_id')!r}"
                )
            return ChallengeResponse.from_payload(payload)

        outcomes.append(
            run_with_retries(
                transport,
                request,
                guarded_builder,
                envelope=turn,
                config=config,
            )
        )
    return tuple(outcomes), packet


def turns_view(agent_outcomes: tuple[AgentOutcome, ...]) -> list[dict[str, Any]]:
    """Validated-view of the debate turns for the resolution packet."""
    view: list[dict[str, Any]] = []
    for outcome in agent_outcomes:
        if outcome.ok and outcome.result is not None:
            view.append(
                {
                    "agent_name": outcome.result.interpretation.agent_name,
                    "stance": outcome.result.interpretation.stance,
                    "missing_data": list(outcome.result.interpretation.missing_data),
                    "reasons": list(outcome.result.interpretation.reasons),
                    "evidence_refs": list(outcome.result.interpretation.evidence_refs),
                }
            )
        else:
            view.append(
                {
                    "agent_name": outcome.agent_name,
                    "stance": "UNAVAILABLE",
                    "missing_data": [outcome.error or "agent turn failed"],
                    "reasons": [],
                    "evidence_refs": [],
                }
            )
    return view


def run_resolution_turn(
    transport,
    conflict: Conflict,
    *,
    agent_outcomes: tuple[AgentOutcome, ...],
    ticker: str,
    run_id: str = "",
    config: TransportConfig | None = None,
) -> AgentOutcome:
    """Decision Agent classifies the debate (item 5).

    The agent proposes only the classification and a *proposed* effect;
    Python recomputes the authoritative effect via ``resolution_effect``
    before anything touches status. A failed turn returns ``ok=False`` and
    the caller applies the unresolved policy.
    """
    envelope: dict[str, Any] = {
        "challenge_rules_version": CHALLENGE_RULES_VERSION,
        "conflict": conflict.payload(),
        "agent_turns": turns_view(agent_outcomes),
        "claim_boundary": (
            "Classify the debate only: CONFIRMED (the conflict stands), "
            "REVISED (a challenged reading was withdrawn or revised in the "
            "record), or UNRESOLVED (agents disagree or data is missing). "
            "You do not choose status effects; Python maps your "
            "classification to preconfigured effects."
        ),
    }
    request = build_agent_prompt(
        RESOLUTION_REQUEST_AGENT,
        RESOLUTION_SCHEMA,
        envelope,
        ticker=ticker,
        run_id=run_id,
    )

    def guarded_builder(
        payload: Mapping[str, Any],
        ticker: str = ticker,
        conflict: Conflict = conflict,
    ) -> ChallengeResolution:
        if payload.get("ticker") != ticker:
            raise ContractViolation(
                f"ticker mutation rejected: expected {ticker!r}, "
                f"got {payload.get('ticker')!r}"
            )
        if payload.get("conflict_rule_id") != conflict.rule_id:
            raise ContractViolation(
                f"rule id mutation rejected: expected {conflict.rule_id!r}, "
                f"got {payload.get('conflict_rule_id')!r}"
            )
        return ChallengeResolution.from_payload(payload)

    return run_with_retries(
        transport,
        request,
        guarded_builder,
        envelope=envelope,
        config=config,
    )


def python_status_effect(
    resolution: ChallengeResolution | None,
    conflict: Conflict,
    agent_outcomes: tuple[AgentOutcome, ...],
) -> str:
    """Python's authoritative status effect (item 7).

    Never trusts the agent's proposed effect: recomputes from the
    classification plus the *actual* recorded stances. A failed resolution
    turn (``None``) is UNRESOLVED by policy.
    """
    if resolution is None:
        effect = resolution_effect({"resolution": "UNRESOLVED"}, conflict)
    else:
        effect = resolution_effect(
            {
                "resolution": resolution.resolution,
                "agent_outcomes": [
                    {
                        "agent_name": name,
                        "stance": stance,
                    }
                    for name, stance in (
                        (
                            o.result.interpretation.agent_name,
                            o.result.interpretation.stance,
                        )
                        if o.ok and o.result is not None
                        else (o.agent_name, "UNAVAILABLE")
                        for o in agent_outcomes
                    )
                ],
            },
            conflict,
        )
    if effect == "READY":
        raise ContractViolation("challenge effects can never be READY (item 7)")
    return effect
