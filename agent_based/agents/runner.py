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
from .technical_agent import run as run_technical


@dataclass(frozen=True)
class AgentRunReport:
    """Everything one agent phase produced, keyed for Phase 4 synthesis."""

    snapshot_hash: str
    market: AgentOutcome
    technical: dict[str, AgentOutcome]          # ticker -> outcome
    flow: dict[str, AgentOutcome]               # ticker -> outcome
    envelope: Mapping[str, Any] = field(default_factory=dict)

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
        """Safe run state after the agent phase (plan §7 Phase 3, item 6)."""
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
) -> AgentRunReport:
    """Run Market + per-candidate Technical/Flow turns over one snapshot.

    ``tickers`` restricts per-candidate agents (default: the whole pool).
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

    return AgentRunReport(
        snapshot_hash=snapshot.data_snapshot_hash,
        market=market_outcome,
        technical=technical,
        flow=flow,
        envelope=envelope,
    )


def persist_agent_outputs(
    store: AuditStore,
    run_id: str,
    report: AgentRunReport,
) -> list[str]:
    """Persist raw + validated outputs (plan §7 Phase 3, item 7).

    Failed turns are stored too — with ``validation_ok=False`` and the
    redacted error — so the audit trail shows the failure, never a gap.
    """
    output_ids: list[str] = []
    outputs: list[AgentOutcome] = [report.market]
    outputs += list(report.technical.values())
    outputs += list(report.flow.values())
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
