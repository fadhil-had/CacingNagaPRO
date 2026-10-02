"""Cross-agent consultation (peer review) tests — Phase 9.

The consultation round lets the Technical and Flow agents for one candidate see
each other's reading and revise. These tests pin the boundaries that make that
safe:

- a peer sees the other agent's *claim*, never the other agent's raw fact slice;
- a consultation turn answers the same contract, so no LLM can introduce a
  status, score, or price;
- evidence citations are checked exactly as in the first pass;
- a failed consultation keeps the first-pass reading and does **not** degrade the
  run state — a second opinion is enrichment, not a dependency;
- both turns land in the audit store.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from cacingnaga.config import AIAnalystConfig
from cacingnaga.errors import ContractViolation
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.snapshot import build_snapshot, snapshot_to_agent_envelope
from cacingnaga.store import AuditStore
from cacingnaga.transport import FakeTransport, TransportConfig

import deployment
from agents import peer_review
from agents.runner import persist_agent_outputs, run_agents
from deployment import CacingNagaSmokeTransport
from orchestrator import AnalysisService

CONFIG = AIAnalystConfig()


@pytest.fixture(scope="module")
def snapshot():
    return build_snapshot(market_frame(), default_frames(), CONFIG)


@pytest.fixture(scope="module")
def envelope(snapshot):
    return snapshot_to_agent_envelope(snapshot)


def _candidate(envelope, index=0):
    return envelope["candidates"][index]


def _ticker(candidate):
    return candidate["facts"]["ticker"]


def _ref(candidate, field="price"):
    return candidate["evidence_refs"][field]


def _flow_ref(candidate, field="mfi"):
    """Flow ids live in their own map, mirroring the Flow Agent's own slice."""
    return candidate["flow_evidence_refs"][field]


def _static(payload):
    """A FakeTransport handler returning one fixed payload."""
    return lambda _request: dict(payload)


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------


def test_peer_envelope_carries_one_candidate_and_the_peer_claims():
    candidate = _candidate(envelope := snapshot_to_agent_envelope(_snap()))
    env = peer_review.build_peer_envelope(
        peer_review.technical_slice(candidate),
        own_reading={"trend": "BULLISH"},
        peer_readings={"FlowAgent": {"flow": "DISTRIBUTION"}},
        market_reading={"regime": "NEUTRAL"},
    )
    assert len(env["candidates"]) == 1
    assert env["your_first_reading"] == {"trend": "BULLISH"}
    assert env["peer_readings"]["FlowAgent"]["flow"] == "DISTRIBUTION"
    assert env["market_reading"]["regime"] == "NEUTRAL"
    assert env["peer_round_version"] == peer_review.PEER_ROUND_VERSION


def _snap():
    return build_snapshot(market_frame(), default_frames(), CONFIG)


def test_absent_peer_reading_is_omitted_not_faked():
    """No validated peer reading must read as 'no view', never as agreement."""
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    env = peer_review.build_peer_envelope(
        peer_review.flow_slice(candidate),
        own_reading=None,
        peer_readings={"FlowAgent": {}, "MarketAgent": None},
        market_reading=None,
    )
    assert env["peer_readings"] == {}
    assert env["your_first_reading"] is None
    assert env["market_reading"] is None


# ---------------------------------------------------------------------------
# Slice boundaries: claims cross, raw facts do not
# ---------------------------------------------------------------------------


def test_technical_slice_excludes_raw_flow_facts():
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    row = peer_review.technical_slice(candidate)["candidates"][0]
    assert "flow" not in row
    assert "facts" in row
    peer_review.assert_peer_boundary(
        peer_review.technical_slice(candidate),
        agent_name=peer_review.TECHNICAL_REVIEW_AGENT,
    )


def test_flow_slice_excludes_raw_technical_facts():
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    row = peer_review.flow_slice(candidate)["candidates"][0]
    assert "facts" not in row
    assert "flow" in row
    peer_review.assert_peer_boundary(
        peer_review.flow_slice(candidate),
        agent_name=peer_review.FLOW_REVIEW_AGENT,
    )


def test_leaked_slice_is_refused():
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    leaky = {"candidates": [{"flow": candidate["flow"]}]}
    with pytest.raises(ContractViolation, match="must not leak raw flow facts"):
        peer_review.assert_peer_boundary(
            leaky, agent_name=peer_review.TECHNICAL_REVIEW_AGENT
        )


def test_multi_candidate_slice_is_refused():
    with pytest.raises(ContractViolation, match="exactly one candidate"):
        peer_review.assert_peer_boundary(
            {"candidates": [{"facts": {}}, {"facts": {}}]},
            agent_name=peer_review.TECHNICAL_REVIEW_AGENT,
        )


# ---------------------------------------------------------------------------
# The consultation turn itself
# ---------------------------------------------------------------------------


def test_consultation_turn_shows_the_peers_reading_in_the_prompt():
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    transport = FakeTransport()
    peer_review.run_technical_review(
        transport,
        candidate,
        own_reading={"trend": "BULLISH"},
        peer_readings={"FlowAgent": {"flow": "DISTRIBUTION"}},
        market_reading={"regime": "BEARISH"},
        config=TransportConfig(max_attempts=1),
    )
    sent = transport.calls[0].envelope
    assert sent["peer_readings"]["FlowAgent"]["flow"] == "DISTRIBUTION"
    assert sent["market_reading"]["regime"] == "BEARISH"
    # The raw flow facts of the peer did not travel with the claim.
    assert "flow" not in sent["candidates"][0]


def test_consultation_turn_uses_the_peer_review_agent_name():
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    transport = FakeTransport()
    outcome = peer_review.run_technical_review(
        transport, candidate, config=TransportConfig(max_attempts=1)
    )
    assert transport.calls[0].agent_name == peer_review.TECHNICAL_REVIEW_AGENT
    assert outcome.agent_name == peer_review.TECHNICAL_REVIEW_AGENT


def test_consultation_cannot_smuggle_a_status_or_score():
    """The forbidden-field guard applies to the peer turn as well."""
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    facts = candidate["facts"]
    payload = {
        "ticker": _ticker(candidate),
        "trend": "BULLISH",
        "setup": facts["setup"],
        "momentum": "POSITIVE",
        "confidence_band": "HIGH",
        "reasons": f"ema20 per {_ref(candidate, 'ema20')}",
        "risks": [],
        "evidence_refs": [_ref(candidate, "ema20")],
        "missing_facts": [],
        "final_status": "READY",
        "score": 0.99,
    }
    outcome = peer_review.run_technical_review(
        FakeTransport({peer_review.TECHNICAL_REVIEW_AGENT: _static(payload)}),
        candidate,
        config=TransportConfig(max_attempts=1),
    )
    assert outcome.status == "FAILED"
    assert "forbidden" in outcome.error


def test_consultation_cannot_change_the_ticker():
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    facts = candidate["facts"]
    payload = {
        "ticker": "OTHER.JK",
        "trend": "BULLISH",
        "setup": facts["setup"],
        "momentum": "POSITIVE",
        "confidence_band": "HIGH",
        "reasons": ["x"],
        "risks": [],
        "evidence_refs": [_ref(candidate, "ema20")],
        "missing_facts": [],
    }
    outcome = peer_review.run_technical_review(
        FakeTransport({peer_review.TECHNICAL_REVIEW_AGENT: _static(payload)}),
        candidate,
        config=TransportConfig(max_attempts=1),
    )
    assert outcome.status == "FAILED"
    assert "ticker mutation rejected" in outcome.error


def test_consultation_still_enforces_evidence_citations():
    """A peer cannot lend an agent a fabricated evidence id."""
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    facts = candidate["facts"]
    payload = {
        "ticker": _ticker(candidate),
        "trend": "BULLISH",
        "setup": facts["setup"],
        "momentum": "POSITIVE",
        "confidence_band": "HIGH",
        "reasons": ["invented support per TechnicalFacts:deadbeef0000:support"],
        "risks": [],
        "evidence_refs": ["TechnicalFacts:deadbeef0000:support"],
        "missing_facts": [],
    }
    outcome = peer_review.run_technical_review(
        FakeTransport({peer_review.TECHNICAL_REVIEW_AGENT: _static(payload)}),
        candidate,
        config=TransportConfig(max_attempts=1),
    )
    assert outcome.status == "FAILED"
    assert "evidence" in outcome.error.lower()


def test_consultation_may_cite_its_own_slice_ids():
    """A peer may cite ids from this candidate, so cross-reading is possible."""
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    facts = candidate["facts"]
    refs = [_ref(candidate, "ema20"), _flow_ref(candidate)]
    payload = {
        "ticker": _ticker(candidate),
        "trend": "BULLISH",
        "setup": facts["setup"],
        "momentum": "POSITIVE",
        "confidence_band": "HIGH",
        "reasons": [f"structure per {refs[0]}; flow disagrees per {refs[1]}"],
        "risks": ["flow conflict acknowledged"],
        "evidence_refs": refs,
        "missing_facts": [],
    }
    outcome = peer_review.run_technical_review(
        FakeTransport({peer_review.TECHNICAL_REVIEW_AGENT: _static(payload)}),
        candidate,
        config=TransportConfig(max_attempts=1),
    )
    assert outcome.ok, outcome.error


def test_flow_consultation_keeps_the_bandar_certainty_ban():
    candidate = _candidate(snapshot_to_agent_envelope(_snap()))
    flow = candidate["flow"]
    payload = {
        "ticker": _ticker(candidate),
        "flow": "DISTRIBUTION",
        "strength": "STRONG",
        "confidence_band": "HIGH",
        "evidence": ["bandar confirmed heavy selling"],
        "risks": [],
        "evidence_refs": [_flow_ref(candidate)],
        "available_indicators": list(flow["available_indicators"]),
        "missing_indicators": list(flow["missing_indicators"]),
    }
    outcome = peer_review.run_flow_review(
        FakeTransport({peer_review.FLOW_REVIEW_AGENT: _static(payload)}),
        candidate,
        config=TransportConfig(max_attempts=1),
    )
    assert outcome.status == "FAILED"
    assert "certainty claim" in outcome.error


# ---------------------------------------------------------------------------
# Reconciliation: the revision replaces the first pass
# ---------------------------------------------------------------------------


def test_reconciled_reading_replaces_the_first_pass(snapshot):
    """A validated revision is what synthesis must see."""
    transport = CacingNagaSmokeTransport()
    plain = run_agents(snapshot, transport, peer_review=False)
    consulted = run_agents(snapshot, CacingNagaSmokeTransport(), peer_review=True)

    assert consulted.peer_turns, "the consultation round produced no turns"
    assert consulted.run_state == plain.run_state
    # The reconciled report exposes the consultation outcome per agent.
    names = {turn.agent_name for turn in consulted.peer_turns}
    assert peer_review.TECHNICAL_REVIEW_AGENT in names
    assert peer_review.FLOW_REVIEW_AGENT in names


def test_consultation_off_by_default_keeps_the_first_pass_only(snapshot):
    report = run_agents(snapshot, CacingNagaSmokeTransport(), peer_review=False)
    assert report.peer_turns == ()
    assert report.first_pass_technical == {}
    assert report.first_pass_flow == {}


def test_failed_consultation_keeps_the_first_pass_and_run_state(snapshot):
    """Degrading one consultation must not turn a COMPLETE run into PARTIAL."""
    baseline = run_agents(snapshot, CacingNagaSmokeTransport(), peer_review=False)
    assert baseline.run_state == "COMPLETE"

    class BrokenPeers(CacingNagaSmokeTransport):
        def complete(self, request):
            if request.agent_name in (
                peer_review.TECHNICAL_REVIEW_AGENT,
                peer_review.FLOW_REVIEW_AGENT,
            ):
                raise ContractViolation("provider outage during consultation")
            return super().complete(request)

    report = run_agents(snapshot, BrokenPeers(), peer_review=True)
    assert report.run_state == "COMPLETE", "consultation failure degraded the run"
    assert report.failed_agents() == ()
    # The first-pass readings are retained unchanged.
    for ticker, outcome in report.technical.items():
        assert outcome.agent_name == "TechnicalAgent"
        assert report.first_pass_technical.get(ticker) is None
    for turn in report.peer_turns:
        assert turn.status == "FAILED"


def test_first_pass_is_retained_only_where_a_revision_replaced_it(snapshot):
    """Audit must hold both readings, never a duplicate of the same one."""
    report = run_agents(snapshot, CacingNagaSmokeTransport(), peer_review=True)
    reconciled = set(report.technical) | set(report.flow)
    retained = set(report.first_pass_technical) | set(report.first_pass_flow)
    assert retained <= reconciled
    for ticker, outcome in report.first_pass_technical.items():
        assert outcome.agent_name == "TechnicalAgent"
        assert report.technical[ticker] is not outcome
    for ticker, outcome in report.first_pass_flow.items():
        assert outcome.agent_name == "FlowAgent"
        assert report.flow[ticker] is not outcome


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def test_both_turns_are_persisted_for_audit(tmp_path, snapshot):
    store = AuditStore(tmp_path / "peer.sqlite3")
    store.create_run(
        run_id="peer-run",
        snapshot=snapshot,
        config=CONFIG,
        snapshot_payload=snapshot.payload(),
        trigger="manual",
    )
    report = run_agents(snapshot, CacingNagaSmokeTransport(), run_id="peer-run",
                        peer_review=True)
    persist_agent_outputs(store, "peer-run", report)

    outputs = store.get_agent_outputs("peer-run")
    agents = {o["agent_name"] for o in outputs}
    assert "TechnicalAgent" in agents
    assert "FlowAgent" in agents
    assert peer_review.TECHNICAL_REVIEW_AGENT in agents
    assert peer_review.FLOW_REVIEW_AGENT in agents

    # The consultation turn is auditable on its own terms.
    turn = [o for o in outputs
            if o["agent_name"] == peer_review.TECHNICAL_REVIEW_AGENT][0]
    assert turn["validation_ok"] is True
    assert turn["prompt_hash"]
    assert turn["validated"]["trend"] in ("BULLISH", "NEUTRAL", "BEARISH")
    # Shadow mode confirms its own reading rather than inventing a revision,
    # and says so in the audit trail.
    assert any(
        "CACINGNAGA_SMOKE_V1" in reason
        for reason in turn["validated"]["reasons"]
    )


def test_full_run_with_consultation_still_completes(tmp_path, snapshot):
    """End-to-end: the live-shaped pipeline passes with consultation on."""
    store = AuditStore(tmp_path / "peer_e2e.sqlite3")
    service = AnalysisService(
        CONFIG, store, CacingNagaSmokeTransport(), peer_review=True
    )
    result = service.run_full_analysis(snapshot, run_id="peer-e2e")
    run = result.run_result
    run.validate()
    assert run.run_status == "COMPLETE"
    assert len(run.recommendations) <= 3
    assert [d.rank for d in run.recommendations] == \
        list(range(1, len(run.recommendations) + 1))


# ---------------------------------------------------------------------------
# Deployment wiring
# ---------------------------------------------------------------------------


def test_deployment_config_defaults_to_shadow_without_llm_section(tmp_path):
    config = deployment.DeploymentConfig(
        universe_source="fixed", universe_fixed=("BBCA.JK",), data_period="2y"
    )
    config.validate()
    assert config.llm_enabled is False
    assert config.peer_review is False


def test_deployment_reads_llm_and_peer_review_sections(tmp_path):
    text = """
[meta]
version = "DEPLOYMENT_CONFIG_1"
[scheduler.universe]
source = "fixed"
tickers = ["BBCA.JK"]
[agents]
peer_review = true
[llm]
enabled = true
api_key_env = "AGENT_LLM_API_KEY"
default_model = "gemini-2.5-pro"
timeout_seconds = 30.0
[llm.models]
MarketAgent = "gemini-2.5-pro"
FlowAgent = "gemini-2.5-flash-lite"
"""
    path = tmp_path / "ai_team.toml"
    path.write_text(text, encoding="utf-8")
    config = deployment.load_deployment_config(path, env={})

    assert config.llm_enabled is True
    assert config.peer_review is True
    assert config.llm_default_model == "gemini-2.5-pro"
    assert config.llm_timeout_seconds == 30.0
    assert config.llm_models == {
        "MarketAgent": "gemini-2.5-pro",
        "FlowAgent": "gemini-2.5-flash-lite",
    }
    # The derived run config routes each named agent to its own model.
    assert config.llm_config().route_for("MarketAgent").model == "gemini-2.5-pro"
    assert config.llm_config().route_for("FlowAgent").model == "gemini-2.5-flash-lite"
    # The deployment payload is auditable and carries no secret value.
    assert "llm" in config.payload()
    assert "peer_review" in config.payload()


def test_deployment_rejects_out_of_bounds_llm_settings(tmp_path):
    text = """
[meta]
version = "DEPLOYMENT_CONFIG_1"
[scheduler.universe]
source = "fixed"
tickers = ["BBCA.JK"]
[llm]
enabled = true
max_attempts = 99
"""
    path = tmp_path / "ai_team.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ContractViolation, match="max_attempts"):
        deployment.load_deployment_config(path, env={})


def test_entry_transport_selection(monkeypatch):
    """``auto`` uses the live provider only when enabled and keyed."""
    import importlib.util

    path = BASE.parent / "scripts" / "ai_team.py"
    spec = importlib.util.spec_from_file_location("ai_team_peer_entry", path)
    entry = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = entry
    spec.loader.exec_module(entry)

    deploy = deployment.DeploymentConfig(
        universe_source="fixed", universe_fixed=("BBCA.JK",), data_period="2y"
    )
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)
    assert isinstance(
        entry._build_transport(deploy, mode="auto"), CacingNagaSmokeTransport
    )
    assert isinstance(
        entry._build_transport(deploy, mode="smoke"), CacingNagaSmokeTransport
    )
    with pytest.raises(ContractViolation, match=r"\$AGENT_LLM_API_KEY"):
        entry._build_transport(deploy, mode="live")
