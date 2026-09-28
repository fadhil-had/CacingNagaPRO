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

from cacingnaga.canonical import canonical_hash, canonical_json
from cacingnaga.challenges import (
    CHALLENGE_RULES_VERSION,
    TRIGGERABLE_RULES,
    ChallengeRecord,
    challenge_id_for,
)
from cacingnaga.contracts import ChallengeResolution
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

from agents import challenge_agent, decision_agent, runner as agent_runner
from agents.merge import AgentDecisionRecord, merge_decision


@dataclass(frozen=True)
class AnalysisServiceResult:
    """Everything one service run produced (audit + render ready)."""

    run_result: RunResult
    agent_report: Any | None                      # AgentRunReport (None on replay)
    decision_records: tuple[AgentDecisionRecord, ...]
    conflicts: tuple[Conflict, ...]
    challenges: tuple[ChallengeRecord, ...] = ()  # Phase 6 debate records
    replayed: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "run_result": _run_result_payload(self.run_result),
            "decision_records": [r.payload() for r in self.decision_records],
            "conflicts": [c.payload() for c in self.conflicts],
            "challenges": [c.payload() for c in self.challenges],
            "replayed": self.replayed,
        }


def _challenge_from_payload(payload: Mapping[str, Any]) -> ChallengeRecord:
    """Deterministic rebuild of a stored ChallengeRecord (replay path)."""
    conflict_payload = payload["conflict"]
    conflict = Conflict(
        rule_id=conflict_payload["rule_id"],
        severity=conflict_payload["severity"],
        conflict_type=conflict_payload["conflict_type"],
        ticker=conflict_payload["ticker"],
        description=conflict_payload["description"],
        evidence_refs=tuple(conflict_payload["evidence_refs"]),
        status_effect=conflict_payload["status_effect"],
    )
    # Rebuilt directly field-by-field (never from agent-authored JSON): the
    # stored record is Python-generated, and a failed resolution turn is
    # stored as the UNRESOLVED stub with no citations by design.
    res = payload["resolution"]
    resolution = ChallengeResolution(
        ticker=res.get("ticker", ""),
        conflict_rule_id=res.get("conflict_rule_id", ""),
        resolution=res.get("resolution", "UNRESOLVED"),
        status_effect=res.get("status_effect", "CAP_AT_WAIT"),
        summary=res.get("summary", ""),
        reasons=tuple(res.get("reasons", ())),
        evidence_refs=tuple(res.get("evidence_refs", ())),
    )
    return ChallengeRecord(
        challenge_id=payload["challenge_id"],
        run_id=payload["run_id"],
        ticker=payload["ticker"],
        conflict=conflict,
        packet=dict(payload["packet"]),
        agent_outcomes=tuple(
            dict(o) for o in payload["agent_outcomes"]
        ),
        resolution=resolution,
        resolved=payload["resolved"],
        status_effect=payload.get("status_effect", "NONE"),
        raw_output_ids=tuple(payload.get("raw_output_ids", ())),
    )


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
        challenge_tickers: tuple[str, ...] | None = None,
    ) -> AnalysisServiceResult:
        """Execute (or replay) one full analysis run over the whole pool.

        The idempotency key is snapshot-level, so the service always runs the
        full candidate pool; per-ticker selection remains a Phase 3 runner
        feature used by tests and focused tools. ``challenge_tickers`` marks
        the explicit ``/debate TICKER`` request path (Phase 6 item 1) — those
        candidates run their debate even without a triggerable conflict.
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
        (
            outcomes,
            decision_records,
            conflicts,
            challenge_records,
            decision_ok,
            market_interp,
        ) = self._synthesize(
            snapshot,
            report,
            run_id=run_id,
            challenge_tickers=challenge_tickers,
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
        self._persist_synthesis(
            run_id, result, decision_records, conflicts, challenge_records
        )
        self._store.transition_run(run_id, final_status)
        return AnalysisServiceResult(
            run_result=result,
            agent_report=report,
            decision_records=decision_records,
            conflicts=conflicts,
            challenges=challenge_records,
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
        challenge_tickers: tuple[str, ...] | None = None,
    ) -> tuple[
        list[PolicyOutcome],
        tuple[AgentDecisionRecord, ...],
        tuple[Conflict, ...],
        tuple[ChallengeRecord, ...],
        bool,
        Any,
    ]:
        """Policy → conflicts → debate → Decision Agent → merge per candidate.

        Phase 6 sits between conflict detection and the Decision Agent: a
        triggered debate (versioned rule or explicit ``/debate`` request)
        runs first, and its Python-computed effect feeds the merge
        (``challenge_lifted``).        Returns ``(outcomes, decision_records, conflicts, challenge_records,
        decision_ok, market_interpretation)``; ``decision_ok`` is False when
        any Decision Agent turn failed (the caller then degrades the run to
        PARTIAL).
        """
        market_interp = report.market_interpretation
        if market_interp is None:
            raise ContractViolation("synthesis requires a validated market interpretation")
        explicit = {t.upper() for t in (challenge_tickers or ())}

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
        challenge_records: list[ChallengeRecord] = []
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

            # 5b. Phase 6 debate — versioned deterministic trigger or explicit
            # /debate request; Python computes the effect, the merge applies it.
            # Default: MATERIAL/HARD conflicts with a known rule id debate
            # automatically. An explicit /debate TICKER additionally debates
            # MINOR-caution conflicts for that ticker (the operator asked).
            challenge_lifted = False
            challenge_ref: str | None = None
            for conflict in conflicts:
                triggerable = (
                    conflict.rule_id in TRIGGERABLE_RULES
                    and conflict.severity != "MINOR_CAUTION"
                )
                explicit_ask = (
                    ticker in explicit
                    and conflict.rule_id in TRIGGERABLE_RULES
                )
                if not (triggerable or explicit_ask):
                    continue
                debate_record, lifted = self._run_challenge(
                    conflict, report, run_id=run_id
                )
                self._store.save_agent_output(
                    run_id,
                    agent_name=self.SERVICE_AGENT_NAME,
                    schema_version="1.0.0",
                    ticker=ticker,
                    validated={
                        "kind": "challenge_record",
                        "challenge_id": debate_record.challenge_id,
                        "status_effect": debate_record.status_effect,
                    },
                    validation_ok=True,
                )
                challenge_records.append(debate_record)
                challenge_ref = debate_record.challenge_id
                if lifted:
                    challenge_lifted = True

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

            # 7. Python merge (conflicts, veto, downgrades, challenge lift)
            # → final status.
            record = merge_decision(
                outcome,
                proposal,
                conflicts,
                challenge_lifted=challenge_lifted,
                challenge_ref=challenge_ref,
            )
            decision_records.append(record)
            outcomes.append(
                dataclasses.replace(
                    outcome,
                    final_status=record.final_status,
                    agent_proposed_status=record.agent_proposed_status,
                challenge_ref=record.challenge_ref,
                    reasons=outcome.reasons
                    + tuple(f"conflict: {c.description}" for c in conflicts),
                    risks=outcome.risks
                    + tuple(record.merge_notes),
                )
            )

        return (
            outcomes,
            tuple(decision_records),
            tuple(all_conflicts),
            tuple(challenge_records),
            decision_ok,
            market_interp,
        )

    # ------------------------------------------------------------------
    # Persistence + replay
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Phase 6 — the debate (bounded rounds, Python-owned effects)
    # ------------------------------------------------------------------

    def _run_challenge(
        self,
        conflict: Conflict,
        report: Any,
        *,
        run_id: str,
    ) -> tuple[ChallengeRecord, bool]:
        """One bounded debate for one conflict; returns (record, lifted).

        ``lifted`` is True only when the record shows a resolved material
        conflict — the signal the merge uses to lift the Phase 4 WAIT cap.
        """
        ticker = conflict.ticker
        readings = {
            "TechnicalAgent": (
                report.technical_interpretation(ticker).payload()
                if report.technical_interpretation(ticker) is not None else {}
            ),
            "FlowAgent": (
                report.flow_interpretation(ticker).payload()
                if report.flow_interpretation(ticker) is not None else {}
            ),
            "MarketAgent": (
                report.market_interpretation.payload()
                if report.market_interpretation is not None else {}
            ),
        }
        challenge_id = challenge_id_for(run_id, conflict)

        # Bounded debate round (items 2–4, 9).
        agent_outcomes, packet = challenge_agent.run_challenge_turns(
            self._transport,
            conflict,
            readings=readings,
            ticker=ticker,
            run_id=run_id,
            config=self._transport_config,
        )
        raw_ids: list[str] = []
        for outcome in agent_outcomes:
            if outcome.ok and outcome.result is not None:
                self._store.save_agent_output(
                    run_id,
                    agent_name=challenge_agent.CHALLENGE_AGENT_NAME,
                    schema_version=outcome.result.schema_version,
                    ticker=ticker,
                    raw_response=outcome.result.raw_response,
                    validated=outcome.result.interpretation.payload(),
                    validation_ok=True,
                    prompt_hash=outcome.result.prompt_hash,
                    latency_ms=outcome.result.latency_ms,
                    usage=dict(outcome.result.usage),
                )
                raw_ids.append(
                    canonical_hash(
                        {
                            "run_id": run_id,
                            "agent": challenge_agent.CHALLENGE_AGENT_NAME,
                            "ticker": ticker,
                            "raw": outcome.result.raw_response,
                        }
                    )[:16]
                )
            else:
                self._store.save_agent_output(
                    run_id,
                    agent_name=challenge_agent.CHALLENGE_AGENT_NAME,
                    schema_version="1.0.0",
                    ticker=ticker,
                    raw_response="",
                    validated=None,
                    validation_ok=False,
                    validation_error=outcome.error,
                )

        # Decision Agent classifies the debate (item 5).
        resolution_outcome = challenge_agent.run_resolution_turn(
            self._transport,
            conflict,
            agent_outcomes=agent_outcomes,
            ticker=ticker,
            run_id=run_id,
            config=self._transport_config,
        )
        resolution = (
            resolution_outcome.result.interpretation
            if resolution_outcome.ok and resolution_outcome.result is not None
            else None
        )
        if resolution is not None:
            self._store.save_agent_output(
                run_id,
                agent_name=challenge_agent.RESOLUTION_AGENT_NAME,
                schema_version=resolution_outcome.result.schema_version,
                ticker=ticker,
                raw_response=resolution_outcome.result.raw_response,
                validated=resolution.payload(),
                validation_ok=True,
                prompt_hash=resolution_outcome.result.prompt_hash,
                latency_ms=resolution_outcome.result.latency_ms,
                usage=dict(resolution_outcome.result.usage),
            )
        else:
            self._store.save_agent_output(
                run_id,
                agent_name=challenge_agent.RESOLUTION_AGENT_NAME,
                schema_version="1.0.0",
                ticker=ticker,
                raw_response="",
                validated=None,
                validation_ok=False,
                validation_error=resolution_outcome.error,
            )

        # Python computes the effect from the classification + recorded stances
        # (item 7); the agent's proposed effect is never authoritative.
        effect = challenge_agent.python_status_effect(
            resolution, conflict, agent_outcomes
        )
        from agents.challenge_agent import turns_view

        # Normalize the packet through canonical JSON so the live record and
        # the stored (round-tripped) record are byte-identical — tuple/list
        # drift between them would break replay equality otherwise.
        import json as _json

        record = ChallengeRecord(
            challenge_id=challenge_id,
            run_id=run_id,
            ticker=ticker,
            conflict=conflict,
            packet=_json.loads(canonical_json(packet)),
            agent_outcomes=tuple(turns_view(agent_outcomes)),
            resolution=resolution,
            resolved=(
                resolution is not None and resolution.resolution == "REVISED"
                and effect == "NONE"
            ),
            status_effect=effect,
            raw_output_ids=tuple(raw_ids),
        )
        self._store.save_challenge(
            run_id,
            challenge_id=challenge_id,
            ticker=ticker,
            conflict_type=conflict.conflict_type,
            status_effect=effect,
            record=record.payload(),
        )
        lifted = bool(effect == "NONE" and conflict.severity == "MATERIAL_CONFLICT")
        return record, lifted

    def _persist_synthesis(
        self,
        run_id: str,
        result: RunResult,
        decision_records: tuple[AgentDecisionRecord, ...],
        conflicts: tuple[Conflict, ...],
        challenges: tuple[ChallengeRecord, ...] = (),
    ) -> None:
        rank_by_ticker = {d.ticker: d.rank for d in result.recommendations}
        challenge_refs = {
            d.ticker: d.challenge_ref
            for d in (*result.recommendations, *result.wait, *result.rejected)
            if d.challenge_ref
        }
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
            challenge_refs=challenge_refs,
        )
        synthesis = {
            "conflict_rules_version": CONFLICT_RULES_VERSION,
            "challenge_rules_version": CHALLENGE_RULES_VERSION,
            "run_result": _run_result_payload(result),
            "decision_records": [r.payload() for r in decision_records],
            "conflicts": [c.payload() for c in conflicts],
            "challenge_records": [c.payload() for c in challenges],
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
                challenge_ref=r.get("challenge_ref"),
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
        challenges = tuple(
            _challenge_from_payload(c)
            for c in synthesis.get("challenge_records", ())
        )
        return AnalysisServiceResult(
            run_result=result,
            agent_report=None,
            decision_records=records,
            conflicts=conflicts,
            challenges=challenges,
            replayed=True,
        )

    def _latest_synthesis(self, run_id: str) -> dict[str, Any] | None:
        found: dict[str, Any] | None = None
        for output in self._store.get_agent_outputs(run_id):
            # Challenge markers share the service agent name; only the true
            # synthesis carries the run_result payload.
            if (
                output["agent_name"] == self.SERVICE_AGENT_NAME
                and output["validation_ok"]
                and isinstance(output["validated"], dict)
                and "run_result" in output["validated"]
            ):
                found = output["validated"]
        return found
