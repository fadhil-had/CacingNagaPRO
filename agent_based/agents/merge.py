"""Phase 4 — Python final-decision merge (plan §7 Phase 4, work item 6).

``agent_proposed_status`` is recorded; Python derives ``final_status`` from
immutable facts, validated interpretations, hard gates, policy, and the
conflict status effects. Deterministic rules, applied in order:

1. **Conflict effects first** — a ``HARD_INVALIDATION`` conflict blocks READY
   this run (capped at WAIT with the conflict recorded); a
   ``MATERIAL_CONFLICT`` caps READY at WAIT until the Phase 6 challenge
   resolves it. ``MINOR_CAUTION`` never changes status.
2. **Upgrade veto** — the Decision Agent may not upgrade Python's status
   (WAIT/REJECT → READY) unless Python had zero hard-gate failures (i.e.
   Python itself derived READY and the proposal merely keeps it). A READY
   proposal over a gated-out candidate is vetoed to Python's status.
3. **Downgrades respected when justified** — the agent may downgrade
   (READY → WAIT/REJECT) with a ``status_change_reason``; Python accepts the
   more conservative status but never lets it back below... a downgrade of a
   candidate with hard-gate failures is a no-op (it was never READY).
4. **Python floor** — when no valid Decision Agent outcome exists (failed
   turn), the deterministic status stands unchanged (never improvised).
5. **Challenge lift (Phase 6)** — a debate that actually resolved a material
   conflict lifts the Phase 4 WAIT cap, but never past Python's own gate
   verdict (``challenge_lifted=True``); the hard gates always keep their
   floor.

Everything the merge consumed is recorded so the decision is traceable:
``AgentDecisionRecord`` carries the proposal, the conflicts, the veto/accept
reasons, and the final status.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from cacingnaga.conflicts import (
    SEVERITY_HARD,
    SEVERITY_MATERIAL,
    Conflict,
    merge_severity,
)
from cacingnaga.errors import ContractViolation


@dataclass(frozen=True)
class AgentDecisionRecord:
    """Traceable per-candidate decision synthesis (audit-grade)."""

    ticker: str
    python_status: str
    agent_proposed_status: str
    final_status: str
    score: float
    conflicts: tuple[Conflict, ...] = ()
    merge_notes: tuple[str, ...] = ()
    proposal_accepted: bool = False
    proposal_vetoed: bool = False
    veto_reason: str = ""
    challenge_ref: str | None = None    # Phase 6: id of the debate record

    def payload(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "python_status": self.python_status,
            "agent_proposed_status": self.agent_proposed_status,
            "final_status": self.final_status,
            "score": self.score,
            "conflicts": [c.payload() for c in self.conflicts],
            "merge_notes": list(self.merge_notes),
            "proposal_accepted": self.proposal_accepted,
            "proposal_vetoed": self.proposal_vetoed,
            "veto_reason": self.veto_reason,
            "challenge_ref": self.challenge_ref,
        }


# ---------------------------------------------------------------------------
# Phase 6 — challenge effect application (plan §7 Phase 6, item 7)
# ---------------------------------------------------------------------------


def merge_decision(
    outcome: Any,                      # PolicyOutcome
    decision: Any | None,              # DecisionInterpretation | None (failed turn)
    conflicts: tuple[Conflict, ...],
    *,
    challenge_lifted: bool = False,    # Phase 6: resolved material conflict
    challenge_ref: str | None = None,  # Phase 6: debate record id
) -> AgentDecisionRecord:
    """Apply the deterministic merge rules for one candidate.

    ``decision is None`` means the Decision Agent turn failed; Python's
    deterministic status then stands (rule 4).
    """
    if outcome.final_status not in ("READY", "WAIT", "REJECT"):
        raise ContractViolation(f"unknown python status: {outcome.final_status}")
    python_status = outcome.final_status
    notes: list[str] = []
    veto_reason = ""

    # Rule 1 — conflict status effects (versioned, preconfigured).
    worst = merge_severity(conflicts)
    status = python_status
    if worst == SEVERITY_HARD:
        if status == "READY":
            status = "WAIT"
            notes.append("HARD_INVALIDATION conflict blocks READY this run (capped at WAIT)")
        else:
            notes.append("HARD_INVALIDATION conflict recorded; status already non-READY")
    elif worst == SEVERITY_MATERIAL:
        if status == "READY":
            status = "WAIT"
            notes.append("MATERIAL_CONFLICT caps READY at WAIT until challenged (Phase 6)")
        else:
            notes.append("MATERIAL_CONFLICT recorded")

    proposed = getattr(decision, "proposed_status", None) if decision is not None else None
    if proposed is None:
        notes.append("decision agent unavailable; deterministic status stands")
        record = AgentDecisionRecord(
            ticker=outcome.ticker,
            python_status=python_status,
            agent_proposed_status=python_status,
            final_status=status,
            score=outcome.score,
            conflicts=conflicts,
            merge_notes=tuple(notes),
            challenge_ref=challenge_ref,
        )
        return _apply_lift(record, challenge_lifted, worst, python_status)
    if proposed not in ("READY", "WAIT", "REJECT"):
        raise ContractViolation(f"unknown agent proposed status: {proposed}")

    favor = {"READY": 2, "WAIT": 1, "REJECT": 0}
    accepted = False
    vetoed = False
    if proposed == status:
        accepted = True
        notes.append(f"proposal matches merged status ({status})")
    elif favor[proposed] > favor[status]:
        # Rule 2 — upgrade veto: the agent cannot override Python's gates or
        # conflict effects (READY over gated-out, WAIT over REJECT, ...).
        vetoed = True
        veto_reason = (
            f"{proposed} proposed over merged status {status}; Python hard "
            "gates and/or conflict effects own the floor"
        )
        notes.append(f"{proposed} upgrade vetoed (Python owns gates/conflicts)")
    else:
        # Rule 3 — downgrade (more conservative) accepted only when the agent
        # explains itself; otherwise the deterministic status stands.
        if not getattr(decision, "status_change_reason", ""):
            notes.append(
                f"downgrade to {proposed} without status_change_reason ignored "
                f"(stays {status})"
            )
        else:
            status = proposed
            accepted = True
            notes.append(f"justified downgrade to {proposed} accepted")

    record = AgentDecisionRecord(
        ticker=outcome.ticker,
        python_status=python_status,
        agent_proposed_status=proposed,
        final_status=status,
        score=outcome.score,
        conflicts=conflicts,
        merge_notes=tuple(notes),
        proposal_accepted=accepted,
        proposal_vetoed=vetoed,
        veto_reason=veto_reason,
        challenge_ref=challenge_ref,
    )
    return _apply_lift(record, challenge_lifted, worst, python_status)


def _apply_lift(
    record: AgentDecisionRecord,
    challenge_lifted: bool,
    worst: str | None,
    python_status: str,
) -> AgentDecisionRecord:
    """Shared post-merge lift for a resolved material conflict (Phase 6).

    Lift rule: a debate that actually resolved the material conflict removes
    the WAIT cap **only** up to Python's own gate verdict — never past the
    hard gates (a failed-turn or gated-out candidate stays put).
    """
    if (
        challenge_lifted
        and worst == SEVERITY_MATERIAL
        and record.final_status == "WAIT"
        and python_status == "READY"
    ):
        return dataclasses.replace(
            record,
            final_status="READY",
            merge_notes=record.merge_notes
            + ("challenge resolved the material conflict; cap lifted to READY",),
        )
    return record
