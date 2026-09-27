"""Phase 4 — AnalysisService: one traceable orchestration path (plan §7 Phase 4).

Sequence per run (work item 1):

1. Verify snapshot integrity (recompute ``data_snapshot_hash``).
2. Register the run in the store *before* any agent call (idempotent).
3. Agent phase (Phase 3): Market once, Technical/Flow per candidate.
4. Deterministic policy evaluation (Phase 2): scores, gates, status.
5. Versioned conflict detection (``conflicts.py``) per candidate.
6. Decision Agent with a fixed evidence packet; proposals recorded.
7. Python merge: conflict status effects + upgrade veto + justified
   downgrades → ``final_status`` (Python always owns rank via ``finalize_run``).
8. Finalize a validated ``RunResult`` (Section 6.10 invariants) and persist
   decisions + the full synthesis record; transition the run state.

Failure semantics: a Market Agent failure fails the whole run (FAILED);
a per-candidate failure yields PARTIAL — both map to ``NOT_EVALUATED``,
never ``NO_TRADE``. Replays against an already-terminal run make **zero**
transport calls and rebuild an identical result from the stored synthesis
(exit criterion: identical facts + captured outputs ⇒ identical final
status, rank, and payload).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Mapping

from cacingnaga.canonical import canonical_hash
from cacingnaga.config import AIAnalystConfig, config_payload
from cacingnaga.conflicts import (
    CONFLICT_RULES_VERSION,
    Conflict,
    detect_conflicts,
)
from cacingnaga.contracts import (
    CandidateDecision,
    DecisionInterpretation,
    FlowInterpretation,
    MarketFacts,
    MarketInterpretation,
    RunResult,
    TechnicalInterpretation,
)
from cacingnaga.errors import ContractViolation
from cacingnaga.policy import DecisionPolicy, PolicyOutcome
from cacingnaga.snapshot import AnalysisSnapshot, assert_snapshot_integrity
from cacingnaga.store import AuditStore
from cacingnaga.transport import AgentTransport, TransportConfig

from agents import decision_agent, runner as agent_runner
from agents.merge import AgentDecisionRecord, merge_decision


@dataclass(frozen=True)
class AnalysisServiceResult:
    """Everything one service run produced (audit + render ready)."""

    run_result: RunResult
    agent_report: Any | None                      # AgentRunReport (None on replay)
    decision_records: tuple[AgentDecisionRecord, ...]
    conflicts: tuple[Conflict, ...]
    replayed: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "run_result": _run_result_payload(self.run_result),
            "decision_records": [r.payload() for r in self.decision_records],
            "conflicts": [c.payload() for c in self.conflicts],
            "replayed": self.replayed,
        }


def _run_result_payload(result: RunResult) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "analysis_date": result.analysis_date,
        "as_of": result.as_of,
        "market": result.market.payload(),
        "market_facts": result.market_facts.payload(),
        "recommendations": [d.payload() for d in result.recommendations],
        "wait": [d.payload() for d in result.wait],
        "rejected": [d.payload() for d in result.rejected],
        "run_status": result.run_status,
        "decision_outcome": result.decision_outcome,
        "warnings": list(result.warnings),
        "data_snapshot_hash": result.data_snapshot_hash,
        "config_hash": result.config_hash,
    }


def _obj_from_payload(cls: type, payload: Mapping[str, Any]) -> Any:
    """Rebuild a dataclass from its JSON payload, ignoring foreign keys.

    JSON lists become tuples so rebuilt objects equal the live ones
    (frozen dataclasses declare tuple fields; tuple != list in comparisons).
    """
    names = {f.name for f in dataclasses.fields(cls)}
    clean = {
        k: tuple(v) if isinstance(v, list) else v
        for k, v in payload.items()
        if k in names
    }
    return cls(**clean)


def _run_result_from_payload(payload: Mapping[str, Any]) -> RunResult:
    """Deterministic rebuild of a stored RunResult (replay path)."""
    result = RunResult(
        run_id=payload["run_id"],
        analysis_date=payload["analysis_date"],
        as_of=payload["as_of"],
        market=MarketInterpretation.from_payload(payload["market"]),
        market_facts=_obj_from_payload(MarketFacts, payload["market_facts"]),
        recommendations=tuple(
            _obj_from_payload(CandidateDecision, d) for d in payload["recommendations"]
        ),
        wait=tuple(_obj_from_payload(CandidateDecision, d) for d in payload["wait"]),
        rejected=tuple(_obj_from_payload(CandidateDecision, d) for d in payload["rejected"]),
        run_status=payload["run_status"],
        decision_outcome=payload["decision_outcome"],
        warnings=tuple(payload["warnings"]),
        data_snapshot_hash=payload["data_snapshot_hash"],
        config_hash=payload["config_hash"],
    )
    result.validate()
    return result


def _degraded_result(
    snapshot: AnalysisSnapshot, run_id: str, config_hash: str, *, status: str
) -> RunResult:
    """PARTIAL/FAILED run result: NOT_EVALUATED, no recommendations."""
    result = RunResult(
        run_id=run_id,
        analysis_date=snapshot.analysis_date,
        as_of=snapshot.as_of,
        market_facts=snapshot.market,
        run_status=status,
        decision_outcome="NOT_EVALUATED",
        warnings=snapshot.warnings,
        data_snapshot_hash=snapshot.data_snapshot_hash,
        config_hash=config_hash,
    )
    result.validate()
    return result


class AnalysisService:
    """Python-owned run lifecycle around the three analyst agents."""

    SERVICE_AGENT_NAME = "AnalysisService"
    SYNTHESIS_SCHEMA_VERSION = "1.0.0"

    def __init__(
        self,
        config: AIAnalystConfig,
        store: AuditStore,
        transport: AgentTransport,
        *,
        transport_config: TransportConfig | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._transport = transport
        self._transport_config = transport_config
        self._policy = DecisionPolicy(config)
        self._config_hash = canonical_hash(config_payload(config))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_full_analysis(
        self,
        snapshot: AnalysisSnapshot,
        *,
        run_id: str,
        trigger: str = "manual",
    ) -> AnalysisServiceResult:
        """Execute (or replay) one full analysis run over the whole pool.

        The idempotency key is snapshot-level, so the service always runs the
        full candidate pool; per-ticker selection remains a Phase 3 runner
        feature used by tests and focused tools.
        """
        # 1. Recompute the immutable snapshot hash before anything else.
        assert_snapshot_integrity(snapshot)

        # 2. Idempotent registration BEFORE any agent call (plan work item 1).
        stored_run, created = self._store.create_run(
            run_id=run_id,
            snapshot=snapshot,
            config=self._config,
            snapshot_payload=snapshot.payload(),
            trigger=trigger,
        )
        if not created and stored_run.run_status in ("PARTIAL", "FAILED"):
            # A degraded run replays its degraded state — never re-runs.
            return AnalysisServiceResult(
                run_result=_degraded_result(
                    snapshot, stored_run.run_id, self._config_hash,
                    status=stored_run.run_status,
                ),
                agent_report=None,
                decision_records=(),
                conflicts=(),
                replayed=True,
            )
        if not created and stored_run.run_status == "COMPLETE":
            # Deterministic replay under the ORIGINAL run id (a repeated
            # trigger may carry a fresh run id; the store returns the first).
            return self._replay_from_store(stored_run.run_id)

        # The stored snapshot payload must recompute to the pinned hash.
        if not self._store.verify_snapshot_hash(snapshot.data_snapshot_hash):
            self._store.transition_run(run_id, "FAILED")
            raise ContractViolation(
                "stored snapshot payload does not recompute to data_snapshot_hash"
            )

        # 3. Agent phase over the full pre-agent pool.
        report = agent_runner.run_agents(
            snapshot,
            self._transport,
            config=self._transport_config,
            run_id=run_id,
        )
        agent_runner.persist_agent_outputs(self._store, run_id, report)

        if report.run_state in ("PARTIAL", "FAILED"):
            self._store.transition_run(run_id, report.run_state)
            result = _degraded_result(
                snapshot, run_id, self._config_hash, status=report.run_state
            )
            return AnalysisServiceResult(
                run_result=result,
                agent_report=report,
                decision_records=(),
                conflicts=(),
            )

        # 4–8. Synthesis on a fully validated analyst phase.
        outcomes, decision_records, conflicts, decision_ok, market_interp = (
            self._synthesize(snapshot, report, run_id=run_id)
        )
        if decision_ok:
            # 8. Python finalizes: status lists, deterministic rank, invariants.
            result = self._policy.finalize_run(
                snapshot,
                outcomes,
                run_id=run_id,
                market_interpretation=market_interp,
            )
            final_status = "COMPLETE"
        else:
            # Hermes-outage policy (§4 item 10): a Decision Agent failure is a
            # provider failure — the run degrades to PARTIAL (NOT_EVALUATED),
            # never to a local improvised recommendation and never to NO_TRADE.
            result = _degraded_result(
                snapshot, run_id, self._config_hash, status="PARTIAL"
            )
            final_status = "PARTIAL"
        self._persist_synthesis(run_id, result, decision_records, conflicts)
        self._store.transition_run(run_id, final_status)
        return AnalysisServiceResult(
            run_result=result,
            agent_report=report,
            decision_records=decision_records,
            conflicts=conflicts,
        )

    # ------------------------------------------------------------------
    # Synthesis: policy → conflicts → decision agent → merge → finalize
    # ------------------------------------------------------------------

    def _synthesize(
        self,
        snapshot: AnalysisSnapshot,
        report: Any,
        *,
        run_id: str,
    ) -> tuple[list[PolicyOutcome], tuple[AgentDecisionRecord, ...], tuple[Conflict, ...], bool, Any]:
        """Policy → conflicts → Decision Agent → merge for every candidate.

        Returns ``(outcomes, decision_records, conflicts, decision_ok,
        market_interpretation)``; ``decision_ok`` is False when any Decision
        Agent turn failed (the caller then degrades the run to PARTIAL).
        """
        market_interp = report.market_interpretation
        if market_interp is None:
            raise ContractViolation("synthesis requires a validated market interpretation")

        candidates = list(zip(snapshot.candidates, snapshot.flows))
        if report.technical or report.flow:
            candidates = [
                (facts, flow)
                for facts, flow in candidates
                if facts.ticker in set(report.technical)
            ]

        outcomes: list[PolicyOutcome] = []
        decision_records: list[AgentDecisionRecord] = []
        all_conflicts: list[Conflict] = []
        decision_ok = True

        envelope = report.envelope
        candidate_slices = {
            c["facts"]["ticker"]: c for c in envelope["candidates"]
        }

        for facts, flow in candidates:
            ticker = facts.ticker
            # 4. Deterministic policy evaluation.
            outcome = self._policy.evaluate(facts, flow, snapshot.market)

            # 5. Versioned conflict detection over validated readings.
            conflicts = detect_conflicts(
                ticker,
                facts=facts,
                flow_facts=flow,
                technical=report.technical_interpretation(ticker),
                flow=report.flow_interpretation(ticker),
                market=market_interp,
            )
            all_conflicts.extend(conflicts)

            # 6. Decision Agent over the fixed evidence packet.
            packet = decision_agent.build_evidence_packet(
                candidate_slices.get(ticker, {"facts": facts.payload()}),
                market_reading=market_interp.payload(),
                technical_reading=(
                    report.technical_interpretation(ticker).payload()
                    if report.technical_interpretation(ticker) is not None else None
                ),
                flow_reading=(
                    report.flow_interpretation(ticker).payload()
                    if report.flow_interpretation(ticker) is not None else None
                ),
                policy_assessment=outcome.payload(),
                conflicts=tuple(c.payload() for c in conflicts),
            )
            decision_outcome = decision_agent.run(
                self._transport,
                packet,
                ticker=ticker,
                config=self._transport_config,
                run_id=run_id,
            )
            if decision_outcome.ok:
                proposal = decision_outcome.result.interpretation
                self._store.save_agent_output(
                    run_id,
                    agent_name=decision_agent.AGENT_NAME,
                    schema_version=decision_outcome.result.schema_version,
                    ticker=ticker,
                    raw_response=decision_outcome.result.raw_response,
                    validated=proposal.payload(),
                    validation_ok=True,
                    prompt_hash=decision_outcome.result.prompt_hash,
                    latency_ms=decision_outcome.result.latency_ms,
                    usage=dict(decision_outcome.result.usage),
                )
            else:
                proposal = None
                decision_ok = False
                self._store.save_agent_output(
                    run_id,
                    agent_name=decision_agent.AGENT_NAME,
                    schema_version="1.0.0",
                    ticker=ticker,
                    raw_response="",
                    validated=None,
                    validation_ok=False,
                    validation_error=decision_outcome.error,
                )

            # 7. Python merge (conflicts, veto, downgrades) → final status.
            record = merge_decision(outcome, proposal, conflicts)
            decision_records.append(record)
            outcomes.append(
                dataclasses.replace(
                    outcome,
                    final_status=record.final_status,
                    agent_proposed_status=record.agent_proposed_status,
                    reasons=outcome.reasons
                    + tuple(f"conflict: {c.description}" for c in conflicts),
                    risks=outcome.risks
                    + tuple(record.merge_notes),
                )
            )

        return outcomes, tuple(decision_records), tuple(all_conflicts), decision_ok, market_interp

    # ------------------------------------------------------------------
    # Persistence + replay
    # ------------------------------------------------------------------

    def _persist_synthesis(
        self,
        run_id: str,
        result: RunResult,
        decision_records: tuple[AgentDecisionRecord, ...],
        conflicts: tuple[Conflict, ...],
    ) -> None:
        rank_by_ticker = {d.ticker: d.rank for d in result.recommendations}
        self._store.save_decisions(
            run_id,
            [
                (
                    d.ticker,
                    d.final_status,
                    d.agent_proposed_status,
                    d.score,
                    rank_by_ticker.get(d.ticker),
                )
                for d in (*result.recommendations, *result.wait, *result.rejected)
            ],
        )
        synthesis = {
            "conflict_rules_version": CONFLICT_RULES_VERSION,
            "run_result": _run_result_payload(result),
            "decision_records": [r.payload() for r in decision_records],
            "conflicts": [c.payload() for c in conflicts],
        }
        self._store.save_agent_output(
            run_id,
            agent_name=self.SERVICE_AGENT_NAME,
            schema_version=self.SYNTHESIS_SCHEMA_VERSION,
            ticker=None,
            validated=synthesis,
            validation_ok=True,
        )

    def _replay_from_store(self, run_id: str) -> AnalysisServiceResult:
        """Rebuild the identical result from stored validated outputs."""
        synthesis = self._latest_synthesis(run_id)
        if synthesis is None:
            raise ContractViolation(f"no stored synthesis for completed run {run_id}")
        result = _run_result_from_payload(synthesis["run_result"])
        records = tuple(
            AgentDecisionRecord(
                ticker=r["ticker"],
                python_status=r["python_status"],
                agent_proposed_status=r["agent_proposed_status"],
                final_status=r["final_status"],
                score=r["score"],
                conflicts=tuple(
                    Conflict(
                        rule_id=c["rule_id"],
                        severity=c["severity"],
                        conflict_type=c["conflict_type"],
                        ticker=c["ticker"],
                        description=c["description"],
                        evidence_refs=tuple(c["evidence_refs"]),
                        status_effect=c["status_effect"],
                    )
                    for c in r["conflicts"]
                ),
                merge_notes=tuple(r["merge_notes"]),
                proposal_accepted=r["proposal_accepted"],
                proposal_vetoed=r["proposal_vetoed"],
                veto_reason=r["veto_reason"],
            )
            for r in synthesis["decision_records"]
        )
        conflicts = tuple(
            Conflict(
                rule_id=c["rule_id"],
                severity=c["severity"],
                conflict_type=c["conflict_type"],
                ticker=c["ticker"],
                description=c["description"],
                evidence_refs=tuple(c["evidence_refs"]),
                status_effect=c["status_effect"],
            )
            for c in synthesis["conflicts"]
        )
        return AnalysisServiceResult(
            run_result=result,
            agent_report=None,
            decision_records=records,
            conflicts=conflicts,
            replayed=True,
        )

    def _latest_synthesis(self, run_id: str) -> dict[str, Any] | None:
        found: dict[str, Any] | None = None
        for output in self._store.get_agent_outputs(run_id):
            if output["agent_name"] == self.SERVICE_AGENT_NAME and output["validation_ok"]:
                found = output["validated"]
        return found
