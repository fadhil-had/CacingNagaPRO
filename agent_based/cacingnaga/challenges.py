"""Phase 6 — challenge/debate mechanism (plan §7 Phase 6).

The debate exists to *expose and resolve material contradictions*, never to
encourage unsupported agent storytelling. Guarantees:

1. **Versioned deterministic trigger** (item 1) — a challenge fires only from
   a known conflict rule id (or an explicit ``/debate`` request). Benign
   low-confidence differences produce no ``Conflict`` objects at all, so they
   can never trigger.
2. **Focused questions** (item 2) — each questioned agent receives its own
   reading, the conflicting evidence ids, and its allowed claim boundary.
   Agents see nothing about each other's internals beyond the conflict.
3. **Bounded** (item 9) — one round, ``CHALLENGE_MAX_TURNS`` attempts per
   agent, fixed question budget; a failed turn resolves UNRESOLVED and the
   preconfigured status effect stands.
4. **Python disposes** (item 7) — ``resolution_effect`` maps an unresolved or
   downgraded conflict to its preconfigured status effect; READY is never an
   available effect for an unresolved conflict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .conflicts import (
    CONFLICT_RULES_VERSION,
    SEVERITY_HARD,
    SEVERITY_MATERIAL,
    Conflict,
)
from .errors import ContractViolation

CHALLENGE_RULES_VERSION = "CHALLENGE_RULES_1"

#: One debate round per conflict (bounded, item 9).
CHALLENGE_MAX_TURNS = 2

#: Agents questioned per conflict rule (deterministic, versioned).
CHALLENGED_AGENTS = {
    "R1": ("TechnicalAgent", "FlowAgent"),
    "R2": ("TechnicalAgent",),
    "R3": ("MarketAgent", "TechnicalAgent"),
    "R4": ("FlowAgent",),
}

#: Known rule ids that may trigger a debate (unknown ids raise).
TRIGGERABLE_RULES = frozenset(CHALLENGED_AGENTS)

#: Preconfigured status effect per severity when a conflict stays unresolved
#: (item 7: WAIT or REJECT per the Phase 4 policy, never automatic READY).
#: Vocabulary mirrors ``conflicts.SEVERITY_STATUS_EFFECT``: BLOCK_READY caps
#: a hard conflict at WAIT this run; CAP_AT_WAIT holds material conflicts.
_UNRESOLVED_EFFECT = {
    SEVERITY_HARD: "BLOCK_READY",
    SEVERITY_MATERIAL: "CAP_AT_WAIT",
}


def challenge_should_trigger(
    conflicts: tuple[Conflict, ...],
    *,
    explicit_request: bool = False,
) -> bool:
    """Deterministic trigger (item 1): only known conflict rules, or explicit.

    Benign low-confidence differences produce no ``Conflict`` objects at all,
    so they cannot trigger. A MATERIAL/HARD conflict with a known rule id
    triggers exactly one debate.
    """
    if explicit_request:
        return True
    return any(c.rule_id in TRIGGERABLE_RULES for c in conflicts)


def _question_for(rule_id: str, agent_name: str, conflict: Conflict) -> str:
    """Focused question plus allowed claim boundary (item 2)."""
    boundary = (
        f"You may SUPPORT, REVISE, or WITHDRAW your own reading of "
        f"{conflict.ticker}; you may not introduce new prices, scores, or "
        "unsupported factual claims."
    )
    if rule_id == "R1":
        role = (
            "your technical structure reading is challenged by the flow "
            "reading" if agent_name == "TechnicalAgent"
            else "your distribution reading is challenged by the technical "
            "structure reading"
        )
        return (
            f"Conflict {rule_id}: {role} for {conflict.ticker}. Does your "
            f"evidence actually support your reading, or should it be "
            f"revised? {boundary} Acknowledge any data you do not have."
        )
    if rule_id == "R2":
        return (
            f"Conflict {rule_id}: your bullish reading for {conflict.ticker} "
            f"lacks core structure facts. {boundary} Acknowledge the missing "
            "data explicitly."
        )
    if rule_id == "R3":
        return (
            f"Conflict {rule_id}: the market regime reading and the "
            f"candidate reading for {conflict.ticker} disagree. {boundary} "
            "Acknowledge any missing data."
        )
    return (
        f"Conflict {rule_id}: your reading for {conflict.ticker} may go "
        f"beyond the available directional evidence. {boundary} Acknowledge "
        "the data you lack."
    )


def build_challenge_packet(
    conflict: Conflict,
    *,
    readings: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """One focused packet per challenged agent (item 2).

    ``readings`` maps agent name → that agent's own validated reading payload.
    Each packet carries the agent's reading, the conflict's evidence ids, the
    question, and the strict response schema — nothing else.
    """
    rule_id = conflict.rule_id
    if rule_id not in TRIGGERABLE_RULES:
        raise ContractViolation(f"conflict rule {rule_id!r} cannot trigger a challenge")
    agents = CHALLENGED_AGENTS[rule_id]
    return {
        "challenge_rules_version": CHALLENGE_RULES_VERSION,
        "conflict_rules_version": CONFLICT_RULES_VERSION,
        "conflict": conflict.payload(),
        "turns": [
            {
                "agent_name": agent,
                "conflict_rule_id": rule_id,
                "question": _question_for(rule_id, agent, conflict),
                "claim_boundary": "SUPPORT|REVISE|WITHDRAW your own reading only",
                "own_reading": dict(readings.get(agent, {})),
                "conflicting_evidence_ids": list(conflict.evidence_refs),
                "response_schema": {
                    "fields": [
                        "agent_name", "ticker", "conflict_rule_id", "stance",
                        "missing_data", "reasons", "evidence_refs",
                    ],
                },
            }
            for agent in agents
        ],
    }


def _agent_outcome_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validated-view of one agent turn for the ChallengeRecord."""
    return {
        "agent_name": payload.get("agent_name", ""),
        "stance": payload.get("stance", ""),
        "missing_data": list(payload.get("missing_data", ())),
        "reasons": list(payload.get("reasons", ())),
        "evidence_refs": list(payload.get("evidence_refs", ())),
    }


@dataclass(frozen=True)
class ChallengeRecord:
    """Complete audit record of one debate (item 6).

    Carries the rule versions, the packet (questions + evidence), the
    raw/validated responses of every agent turn, and the final resolution
    with its explicit status effect. ``challenge_id`` is deterministic per
    (run, ticker, rule) so replays and re-triggers reuse the same record.
    """

    challenge_id: str
    run_id: str
    ticker: str
    conflict: Conflict
    packet: dict[str, Any]
    agent_outcomes: tuple[dict[str, Any], ...]      # validated turns (+errors)
    resolution: Any                                  # ChallengeResolution
    resolved: bool
    status_effect: str = "NONE"                      # Python's applied effect
    raw_output_ids: tuple[str, ...] = ()             # agent_outputs ids (raw text)

    def payload(self) -> dict[str, Any]:
        return {
            "challenge_id": self.challenge_id,
            "run_id": self.run_id,
            "ticker": self.ticker,
            "conflict": self.conflict.payload(),
            "packet": self.packet,
            "agent_outcomes": [dict(o) for o in self.agent_outcomes],
            "resolution": self._resolution_payload(),
            "resolved": self.resolved,
            "status_effect": self.status_effect,
            "raw_output_ids": list(self.raw_output_ids),
            "challenge_rules_version": CHALLENGE_RULES_VERSION,
        }

    def _resolution_payload(self) -> dict[str, Any]:
        """Resolution payload; a failed resolution turn serializes as the
        canonical UNRESOLVED stub (never crashes, never improvises)."""
        if self.resolution is not None:
            return self.resolution.payload()
        return {
            "ticker": self.ticker,
            "conflict_rule_id": self.conflict.rule_id,
            "resolution": "UNRESOLVED",
            "status_effect": self.status_effect,
            "summary": "resolution turn failed; preconfigured policy applied",
            "reasons": [],
            "evidence_refs": [],
        }


def challenge_id_for(run_id: str, conflict: Conflict) -> str:
    """Deterministic challenge id (replay-stable, item 6)."""
    from .canonical import canonical_hash

    key = canonical_hash(
        {
            "run_id": run_id,
            "ticker": conflict.ticker,
            "rule_id": conflict.rule_id,
            "conflict_type": conflict.conflict_type,
            "rules_version": CONFLICT_RULES_VERSION,
        }
    )
    return f"CH-{key[:16]}"


def resolution_effect(
    resolution: Mapping[str, Any],
    conflict: Conflict,
) -> str:
    """Python's preconfigured status effect for one resolution (item 7).

    The Decision Agent classifies the debate (CONFIRMED / REVISED /
    UNRESOLVED); Python maps that classification to the effect:

    - CONFIRMED  — the challenge upheld the conflict: preconfigured effect
      for the conflict's severity (material → WAIT cap, hard → block READY).
    - REVISED    — the challenged agent withdrew/revised its reading, i.e.
      the reading that caused the conflict no longer stands → ``NONE``. The
      lift applies **only** when the record actually shows a REVISE/WITHDRAW
      stance from a challenged agent; a claimed revision without one is
      storytelling and is treated as UNRESOLVED.
    - UNRESOLVED — agents disagreed or a turn failed: preconfigured effect.

    READY is never a producible effect (item 7). Unknown resolutions raise.
    """
    kind = resolution.get("resolution", "UNRESOLVED")
    fallback = _UNRESOLVED_EFFECT.get(conflict.severity, "CAP_AT_WAIT")
    if kind == "REVISED":
        # A REVISED debate can remove the conflict's effect *only if* the
        # challenged reading is actually withdrawn/revised in the record.
        stances = {
            o.get("agent_name"): o.get("stance")
            for o in resolution.get("agent_outcomes", ()) or ()
            if isinstance(o, Mapping)
        }
        expected = CHALLENGED_AGENTS.get(conflict.rule_id, ())
        revised = any(stances.get(a) in ("REVISE", "WITHDRAW") for a in expected)
        return "NONE" if revised else fallback
    if kind in ("CONFIRMED", "UNRESOLVED"):
        return fallback
    raise ContractViolation(f"unknown challenge resolution kind: {kind!r}")
