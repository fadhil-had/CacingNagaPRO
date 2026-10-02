"""Phase 3 — run the three analysts over one immutable snapshot (plan §7).

``run_agents`` executes Market once per run and Technical/Flow once per
candidate, sequentially (concurrency is a Phase 4 transport concern), then
persists every prompt/result through the Phase 2A store.

Failure isolation (exit criteria): one agent failure never affects another
agent's result, and it degrades the run state deterministically:

- all turns VALIDATED          -> run state unchanged (caller keeps COMPLETE)
- any per-candidate failure    -> run state becomes PARTIAL
- the Market Agent failure      -> run state becomes FAILED (market is
  mandatory for every downstream decision)

A failed turn never fabricates an interpretation: the failed slot is simply
absent from ``AgentRunReport``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from cacingnaga.contracts import (
    FlowInterpretation,
    MarketInterpretation,
    TechnicalInterpretation,
)
from cacingnaga.snapshot import AnalysisSnapshot, snapshot_to_agent_envelope
from cacingnaga.store import AuditStore
from cacingnaga.transport import AgentTransport, TransportConfig

from .base import AgentOutcome, redact
from .flow_agent import run as run_flow
from .market_agent import run as run_market
from .peer_review import consult_candidate
from .technical_agent import run as run_technical


@dataclass(frozen=True)
class AgentRunReport:
    """Everything one agent phase produced, keyed for Phase 4 synthesis."""

    snapshot_hash: str
    market: AgentOutcome
    technical: dict[str, AgentOutcome]          # ticker -> reconciled outcome
    flow: dict[str, AgentOutcome]               # ticker -> reconciled outcome
    envelope: Mapping[str, Any] = field(default_factory=dict)
    #: Phase 9 consultation turns, persisted beside the first pass they revised
    #: so the reconciled reading and the revision behind it are reconstructable.
    peer_turns: tuple[AgentOutcome, ...] = ()
    #: The independent first pass, kept only where consultation replaced it.
    first_pass_technical: dict[str, AgentOutcome] = field(default_factory=dict)
    first_pass_flow: dict[str, AgentOutcome] = field(default_factory=dict)

    @property
    def market_interpretation(self) -> MarketInterpretation | None:
        return self.market.result.interpretation if self.market.ok else None

    def technical_interpretation(self, ticker: str) -> TechnicalInterpretation | None:
        outcome = self.technical.get(ticker)
        return outcome.result.interpretation if outcome and outcome.ok else None

    def flow_interpretation(self, ticker: str) -> FlowInterpretation | None:
        outcome = self.flow.get(ticker)
        return outcome.result.interpretation if outcome and outcome.ok else None

    @property
    def has_failures(self) -> bool:
        outcomes = [self.market, *self.technical.values(), *self.flow.values()]
        return any(not o.ok for o in outcomes)

    @property
    def run_state(self) -> str:
        """Safe run state after the agent phase (plan §7 Phase 3, item 6).

        Phase 9 consultation turns are deliberately excluded: a failed second
        opinion keeps the first-pass reading, so it is an enrichment gap, not a
        degradation of the run. The first pass alone decides COMPLETE/PARTIAL.
        """
        if not self.market.ok:
            return "FAILED"
        if self.has_failures:
            return "PARTIAL"
        return "COMPLETE"

    def failed_agents(self) -> tuple[str, ...]:
        names = []
        if not self.market.ok:
            names.append(f"{self.market.agent_name}({self.market.error})")
        for ticker, outcome in self.technical.items():
            if not outcome.ok:
                names.append(f"{outcome.agent_name}:{ticker}({outcome.error})")
        for ticker, outcome in self.flow.items():
            if not outcome.ok:
                names.append(f"{outcome.agent_name}:{ticker}({outcome.error})")
        return tuple(names)


def run_agents(
    snapshot: AnalysisSnapshot,
    transport: AgentTransport,
    *,
    config: TransportConfig | None = None,
    run_id: str = "",
    tickers: tuple[str, ...] | None = None,
    peer_review: bool = False,
) -> AgentRunReport:
    """Run Market + per-candidate Technical/Flow turns over one snapshot.

    ``tickers`` restricts per-candidate agents (default: the whole pool).
    ``peer_review`` adds one Phase 9 consultation round in which the Technical
    and Flow agents for the same candidate see each other's reading and may
    revise. The reconciled reading replaces the first pass; both turns are kept
    in ``peer_turns`` for the audit trail. Consultation failures never change
    the run state (see :attr:`AgentRunReport.run_state`).
    """
    envelope = snapshot_to_agent_envelope(snapshot)
    candidates = [c for c in envelope["candidates"]
                  if tickers is None or c["facts"]["ticker"] in set(tickers)]

    market_outcome = run_market(transport, envelope, config=config, run_id=run_id)

    technical: dict[str, AgentOutcome] = {}
    flow: dict[str, AgentOutcome] = {}
    for candidate in candidates:
        ticker = candidate["facts"]["ticker"]
        technical[ticker] = run_technical(transport, candidate, config=config, run_id=run_id)
        flow[ticker] = run_flow(transport, candidate, config=config, run_id=run_id)

    peer_turns: tuple[AgentOutcome, ...] = ()
    first_pass_technical: dict[str, AgentOutcome] = {}
    first_pass_flow: dict[str, AgentOutcome] = {}
    if peer_review and market_outcome.ok:
        market_reading = (
            dict(market_outcome.result.interpretation.payload())
            if market_outcome.result is not None
            else None
        )
        collected: list[AgentOutcome] = []
        for candidate in candidates:
            ticker = candidate["facts"]["ticker"]
            first_technical, first_flow = technical[ticker], flow[ticker]
            reconciled_technical, reconciled_flow, turns = consult_candidate(
                transport,
                candidate,
                technical=first_technical,
                flow=first_flow,
                market_reading=market_reading,
                config=config,
                run_id=run_id,
            )
            # The first pass is retained only when a consultation actually
            # replaced it, so the audit trail holds both readings rather than
            # a duplicate of the same one.
            if reconciled_technical is not first_technical:
                first_pass_technical[ticker] = first_technical
            if reconciled_flow is not first_flow:
                first_pass_flow[ticker] = first_flow
            technical[ticker] = reconciled_technical
            flow[ticker] = reconciled_flow
            collected.extend(turns)
        peer_turns = tuple(collected)

    return AgentRunReport(
        snapshot_hash=snapshot.data_snapshot_hash,
        market=market_outcome,
        technical=technical,
        flow=flow,
        envelope=envelope,
        peer_turns=peer_turns,
        first_pass_technical=first_pass_technical,
        first_pass_flow=first_pass_flow,
    )


def persist_agent_outputs(
    store: AuditStore,
    run_id: str,
    report: AgentRunReport,
) -> list[str]:
    """Persist raw + validated outputs (plan §7 Phase 3, item 7).

    Failed turns are stored too — with ``validation_ok=False`` and the
    redacted error — so the audit trail shows the failure, never a gap. Phase 9
    consultation turns are persisted alongside the first pass they revised, so
    the reconciled reading and the change behind it are both reconstructable.
    """
    output_ids: list[str] = []
    outputs: list[AgentOutcome] = [report.market]
    outputs += list(report.first_pass_technical.values())
    outputs += list(report.first_pass_flow.values())
    outputs += list(report.technical.values())
    outputs += list(report.flow.values())
    outputs += list(report.peer_turns)
    for outcome in outputs:
        if outcome.ok:
            result = outcome.result
            output_ids.append(
                store.save_agent_output(
                    run_id,
                    agent_name=outcome.agent_name,
                    schema_version=result.schema_version,
                    ticker=outcome.ticker,
                    raw_response=redact(result.raw_response),
                    validated=result.interpretation.payload(),
                    validation_ok=True,
                    prompt_hash=result.prompt_hash,
                    latency_ms=result.latency_ms,
                    usage=dict(result.usage),
                )
            )
        else:
            output_ids.append(
                store.save_agent_output(
                    run_id,
                    agent_name=outcome.agent_name,
                    schema_version="1.0.0",
                    ticker=outcome.ticker,
                    raw_response="",
                    validated=None,
                    validation_ok=False,
                    validation_error=redact(outcome.error),
                )
            )
    return output_ids
