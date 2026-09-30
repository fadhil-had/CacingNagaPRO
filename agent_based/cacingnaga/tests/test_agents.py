"""Phase 3 — agent contracts and individual analysts (plan §7 Phase 3).

Exit criteria under test:
- each agent has isolated schema/fixture tests,
- a malicious response (ticker/number mutation, forbidden fields, fabricated
  evidence) cannot affect the result,
- missing optional data produces flagged missing facts, never silent neutrals,
- three independent assessments come from one snapshot,
- one agent failure is isolated and yields a safe PARTIAL/FAILED state,
- raw + validated outputs persist through the Phase 2A store.

The CUAN scenario (technical-bullish / flow-distribution) is exercised here so
Phase 6 challenge tests have a ready-made conflict record.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
sys.path.insert(0, str(BASE))

from cacingnaga.config import AIAnalystConfig
from cacingnaga.contracts import (
    FlowInterpretation,
    MarketInterpretation,
    TechnicalInterpretation,
)
from cacingnaga.errors import ContractViolation
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.snapshot import (
    build_snapshot,
    fact_evidence_refs,
    snapshot_to_agent_envelope,
)
from cacingnaga.store import AuditStore
from cacingnaga.transport import (
    AgentResponse,
    FakeTransport,
    TransportConfig,
    extract_json_payload,
)

from agents import flow_agent, market_agent, runner, technical_agent
from agents.base import redact, run_with_retries, validate_payload

CONFIG = AIAnalystConfig()


@pytest.fixture(scope="module")
def snapshot():
    return build_snapshot(market_frame(), default_frames(), CONFIG)


@pytest.fixture(scope="module")
def envelope(snapshot):
    return snapshot_to_agent_envelope(snapshot)


@pytest.fixture(scope="module")
def cuan(envelope):
    return next(c for c in envelope["candidates"] if c["facts"]["ticker"] == "CUAN.JK")


# ---------------------------------------------------------------------------
# Handler builders (deterministic "model responses" citing real evidence ids)
# ---------------------------------------------------------------------------


def market_handler_factory(env):
    refs = env["market"]["evidence_refs"]

    def handler(request):
        return {
            "regime": "BULLISH",
            "confidence_band": "MEDIUM",
            "swing_environment": "FAVORABLE",
            "secondary_direction": None,
            "reasons": [
                f"close holds above rising EMAs per {refs['close']} and {refs['ema20']}",
                f"breadth confirms participation per {refs['breadth50']}",
            ],
            "risk_flags": [f"market RSI is stretched per {refs['rsi']}"],
            "evidence_refs": [refs["close"], refs["ema20"], refs["breadth50"], refs["rsi"]],
        }

    return handler


def technical_handler_factory(candidate):
    refs = candidate["evidence_refs"]

    def handler(request):
        return {
            "ticker": candidate["facts"]["ticker"],
            "trend": "BULLISH",
            "setup": candidate["facts"]["setup"],       # echoes the Python label
            "momentum": "NEGATIVE",
            "confidence_band": "MEDIUM",
            "reasons": [
                f"EMA structure still bullish per {refs['ema20']} and {refs['ema50']}",
                f"price pulled back into the EMA20 zone per {refs['price']}",
            ],
            "risks": [f"momentum rolled over per {refs['macd_hist']}"],
            "evidence_refs": [refs["ema20"], refs["ema50"], refs["price"], refs["macd_hist"]],
            "missing_facts": [name for name in ("support", "resistance")
                              if candidate["facts"].get(name) is None],
        }

    return handler


def flow_handler_factory(candidate):
    refs = candidate["flow_evidence_refs"]
    flow = candidate["flow"]

    def handler(request):
        return {
            "ticker": candidate["facts"]["ticker"],
            "flow": "DISTRIBUTION",
            "strength": "MEDIUM",
            "confidence_band": "MEDIUM",
            "evidence": [
                f"MFI pinned at the floor per {refs['mfi']}",
                f"CMF negative with heavy down-bar volume per {refs['cmf']} and {refs['obv_slope']}",
            ],
            "risks": ["OHLCV-derived flow is an indication only; it cannot identify who actually transacted"],
            "evidence_refs": [refs["mfi"], refs["cmf"], refs["obv_slope"]],
            "available_indicators": list(flow["available_indicators"]),
            "missing_indicators": list(flow["missing_indicators"]),
        }

    return handler


# ---------------------------------------------------------------------------
# Transport layer
# ---------------------------------------------------------------------------


def test_transport_config_rejects_bad_bounds():
    with pytest.raises(ContractViolation):
        TransportConfig(timeout_seconds=0).validate()
    with pytest.raises(ContractViolation):
        TransportConfig(max_attempts=9).validate()
    with pytest.raises(ContractViolation):
        TransportConfig(retry_backoff_seconds=-1).validate()


def test_fake_transport_routes_per_agent_and_ticker_and_records_calls(envelope, cuan):
    transport = FakeTransport({
        "MarketAgent": market_handler_factory(envelope),
        f"TechnicalAgent:{cuan['facts']['ticker']}": technical_handler_factory(cuan),
    })
    market_agent.run(transport, envelope)
    technical_agent.run(transport, cuan)
    assert [c.agent_name for c in transport.calls] == ["MarketAgent", "TechnicalAgent"]
    assert transport.calls[1].ticker == "CUAN.JK"


def test_fake_transport_unknown_agent_is_a_failed_turn(envelope):
    """No handler is a provider failure: bounded, isolated, never raised."""
    outcome = market_agent.run(FakeTransport(), envelope, config=TransportConfig(max_attempts=1))
    assert not outcome.ok
    assert "no handler" in outcome.error


def test_transport_failure_result_is_never_raised_out_of_the_boundary(envelope):
    """run_with_retries returns an outcome; the caller decides run state."""
    outcome = run_with_retries(
        FakeTransport(),
        market_agent.build_request(envelope),
        market_agent.build_interpretation,
        envelope={"market": envelope["market"]},
        config=TransportConfig(max_attempts=1),
    )
    assert outcome.status == "FAILED" and outcome.result is None


def test_extract_json_payload_handles_fenced_and_bare_objects():
    assert extract_json_payload('{"a": 1}') == {"a": 1}
    assert extract_json_payload('noise\n```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    with pytest.raises(ContractViolation):
        extract_json_payload("no json here")
    with pytest.raises(ContractViolation):
        extract_json_payload("[1, 2, 3]")


def test_redact_strips_credential_shaped_strings():
    text = "call failed for key sk-abcdef1234567890 with Authorization: Bearer tok123"
    out = redact(text)
    assert "sk-abcdef1234567890" not in out and "Bearer tok123" not in out
    assert "[REDACTED]" in out


# ---------------------------------------------------------------------------
# Snapshot envelope: stable evidence ids for every fact
# ---------------------------------------------------------------------------


def test_fact_evidence_refs_match_fact_evidence_ids(snapshot):
    refs = fact_evidence_refs(snapshot.market.payload())
    assert refs["close"] == snapshot.market.evidence_id("close")
    assert refs["rsi"] == snapshot.market.evidence_id("rsi")
    # Metadata keys are not evidence.
    assert "fact_type" not in refs and "fact_version" not in refs


def test_envelope_carries_evidence_refs_for_every_fact_field(envelope, cuan):
    metadata = {"type", "fact_type", "fact_version"}
    market_fields = set(envelope["market"]["facts"]) - metadata
    assert set(envelope["market"]["evidence_refs"]) == market_fields
    tech_fields = set(cuan["facts"]) - metadata
    assert set(cuan["evidence_refs"]) == tech_fields
    flow_fields = set(cuan["flow"]) - metadata
    assert set(cuan["flow_evidence_refs"]) == flow_fields
    # Technical and flow refs never collide as ids (type prefix differs).
    assert not set(cuan["evidence_refs"].values()) & set(cuan["flow_evidence_refs"].values())
    # Shared field names (ticker, as_of) are fine: the ids disambiguate them.
    assert set(cuan["evidence_refs"]) & set(cuan["flow_evidence_refs"])


def test_envelope_includes_snapshot_hash(envelope, snapshot):
    assert envelope["snapshot_hash"] == snapshot.data_snapshot_hash


# ---------------------------------------------------------------------------
# Validation pipeline: hostile responses cannot pass
# ---------------------------------------------------------------------------


def test_run_success_returns_validated_outcome_with_citations(envelope):
    transport = FakeTransport({"MarketAgent": market_handler_factory(envelope)})
    outcome = market_agent.run(transport, envelope, run_id="run-x")
    assert outcome.ok and outcome.status == "VALIDATED"
    interp = outcome.result.interpretation
    assert isinstance(interp, MarketInterpretation)
    assert interp.regime == "BULLISH" and interp.swing_environment == "FAVORABLE"
    assert outcome.result.evidence_refs  # citations captured
    assert outcome.result.prompt_hash    # prompt is pinned for the audit trail


def test_forbidden_field_injection_is_rejected(envelope):
    refs = envelope["market"]["evidence_refs"]
    base = market_handler_factory(envelope)(None)

    def hostile(request):
        payload = dict(base)
        payload["score"] = 0.99          # model tries to own the score
        payload["rank"] = 1
        return payload

    outcome = market_agent.run(FakeTransport({"MarketAgent": hostile}), envelope)
    assert not outcome.ok
    assert "forbidden agent fields" in outcome.error
    assert outcome.result is None        # no fabricated result leaks out


def test_fabricated_evidence_citation_is_rejected(envelope):
    base = market_handler_factory(envelope)(None)

    def hostile(request):
        payload = dict(base)
        payload["evidence_refs"] = ["MarketFacts:000000000000:close"]
        return payload

    outcome = market_agent.run(FakeTransport({"MarketAgent": hostile}), envelope)
    assert not outcome.ok
    assert "absent from the envelope" in outcome.error


def test_response_without_any_citation_is_rejected(envelope):
    base = market_handler_factory(envelope)(None)

    def uncited(request):
        payload = dict(base)
        payload["reasons"] = ["trust me, it is bullish"]
        payload["evidence_refs"] = []
        return payload

    outcome = market_agent.run(
        FakeTransport({"MarketAgent": uncited}), envelope,
        config=TransportConfig(max_attempts=1),
    )
    assert not outcome.ok
    assert "cites no evidence ids" in outcome.error


def test_ticker_mutation_is_rejected(cuan):
    base = technical_handler_factory(cuan)(None)

    def hostile(request):
        payload = dict(base)
        payload["ticker"] = "ERTX.JK"    # impersonation attempt
        return payload

    outcome = technical_agent.run(FakeTransport({"TechnicalAgent:CUAN.JK": hostile}), cuan)
    assert not outcome.ok
    assert "ticker mutation rejected" in outcome.error


def test_numeric_fact_mutation_cannot_reach_the_contract(cuan):
    """A payload *shape* is contract-validated; numbers stay Python-owned."""
    base = flow_handler_factory(cuan)(None)
    hostile = dict(base)
    hostile["mfi"] = 80.0                # flow agent cannot smuggle values back
    with pytest.raises(ContractViolation):
        FlowInterpretation.from_payload(hostile)


def test_bandar_certainty_claim_is_rejected(cuan):
    base = flow_handler_factory(cuan)(None)

    def hostile(request):
        payload = dict(base)
        payload["evidence"] = [f"bandar accumulation confirmed per {cuan['flow_evidence_refs']['cmf']}"]
        return payload

    outcome = flow_agent.run(FakeTransport({"FlowAgent:CUAN.JK": hostile}), cuan)
    assert not outcome.ok
    assert "bandar" in outcome.error


def test_market_scope_never_sees_candidates(envelope):
    with pytest.raises(ContractViolation):
        market_agent.assert_market_scope(envelope)   # full envelope has candidates
    market_agent.assert_market_scope({"market": envelope["market"]})  # slice is fine


def test_per_candidate_slices_are_minimal(envelope, cuan):
    tech_transport = FakeTransport({"TechnicalAgent:CUAN.JK": technical_handler_factory(cuan)})
    technical_agent.run(tech_transport, cuan)
    sent = tech_transport.calls[0].envelope["candidates"][0]
    assert set(sent) == {"facts", "evidence_refs", "evidence_ids"}   # no flow payload
    flow_transport = FakeTransport({"FlowAgent:CUAN.JK": flow_handler_factory(cuan)})
    flow_agent.run(flow_transport, cuan)
    sent = flow_transport.calls[0].envelope["candidates"][0]
    assert set(sent) == {"flow", "evidence_refs", "evidence_ids"}    # no technical facts


# ---------------------------------------------------------------------------
# Bounded retries / timeouts -> PARTIAL or FAILED, never fabrication
# ---------------------------------------------------------------------------


def test_transport_exhaustion_yields_failed_outcome(envelope):
    def always_down(request):
        raise RuntimeError("provider unavailable")

    outcome = market_agent.run(
        FakeTransport({"MarketAgent": always_down}), envelope,
        config=TransportConfig(max_attempts=2, retry_backoff_seconds=0.0),
    )
    assert outcome.status == "FAILED" and outcome.attempts == 2
    assert "RuntimeError" in outcome.error
    assert outcome.result is None


def test_timeout_budget_yields_partial_outcome(envelope):
    import time

    def slow_failure(request):
        time.sleep(0.05)
        raise RuntimeError("slow provider")

    outcome = market_agent.run(
        FakeTransport({"MarketAgent": slow_failure}), envelope,
        config=TransportConfig(timeout_seconds=0.01, max_attempts=3,
                               retry_backoff_seconds=0.0),
    )
    assert outcome.status == "PARTIAL"   # budget exhausted before retrying
    assert outcome.result is None


def test_retry_succeeds_after_transient_failure(envelope):
    state = {"calls": 0}
    base = market_handler_factory(envelope)(None)

    def flaky(request):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("transient")
        return base

    outcome = market_agent.run(
        FakeTransport({"MarketAgent": flaky}), envelope,
        config=TransportConfig(max_attempts=2, retry_backoff_seconds=0.0),
    )
    assert outcome.ok and outcome.attempts == 2


def test_validation_failure_consumes_attempts_then_fails(envelope):
    base = market_handler_factory(envelope)(None)

    def bad_enum(request):
        payload = dict(base)
        payload["regime"] = "SUPER_BULLISH"      # not in the strict enum
        return payload

    outcome = market_agent.run(
        FakeTransport({"MarketAgent": bad_enum}), envelope,
        config=TransportConfig(max_attempts=2, retry_backoff_seconds=0.0),
    )
    assert outcome.status == "FAILED" and outcome.attempts == 2
    assert "regime" in outcome.error


# ---------------------------------------------------------------------------
# Runner: three independent assessments from one snapshot, failure isolation
# ---------------------------------------------------------------------------


def test_run_agents_produces_three_independent_assessments(snapshot, envelope):
    transport = FakeTransport({
        "MarketAgent": market_handler_factory(envelope),
        **{f"TechnicalAgent:{c['facts']['ticker']}": technical_handler_factory(c)
           for c in envelope["candidates"]},
        **{f"FlowAgent:{c['facts']['ticker']}": flow_handler_factory(c)
           for c in envelope["candidates"]},
    })
    report = runner.run_agents(snapshot, transport, run_id="run-all")
    assert report.snapshot_hash == snapshot.data_snapshot_hash
    assert report.market.ok
    assert set(report.technical) == {c["facts"]["ticker"] for c in envelope["candidates"]}
    assert all(o.ok for o in report.technical.values())
    assert all(o.ok for o in report.flow.values())
    assert report.run_state == "COMPLETE"
    assert not report.failed_agents()
    # 1 market + 2 per candidate turns on the same immutable snapshot.
    assert len(transport.calls) == 1 + 2 * len(envelope["candidates"])


def test_run_agents_respects_ticker_filter(snapshot, envelope, cuan):
    transport = FakeTransport({
        "MarketAgent": market_handler_factory(envelope),
        f"TechnicalAgent:CUAN.JK": technical_handler_factory(cuan),
        f"FlowAgent:CUAN.JK": flow_handler_factory(cuan),
    })
    report = runner.run_agents(snapshot, transport, tickers=("CUAN.JK",))
    assert len(transport.calls) == 3
    assert set(report.technical) == {"CUAN.JK"}


def test_market_failure_isolates_to_failed_run_state(snapshot, envelope, cuan):
    def dead_market(request):
        raise RuntimeError("market provider down")

    transport = FakeTransport({
        "MarketAgent": dead_market,
        "TechnicalAgent:CUAN.JK": technical_handler_factory(cuan),
        "FlowAgent:CUAN.JK": flow_handler_factory(cuan),
    })
    report = runner.run_agents(snapshot, transport, tickers=("CUAN.JK",))
    assert not report.market.ok
    assert report.market_interpretation is None
    assert report.technical_interpretation("CUAN.JK") is not None   # isolated
    assert report.flow_interpretation("CUAN.JK") is not None
    assert report.run_state == "FAILED"


def test_single_candidate_failure_yields_partial_run_state(snapshot, envelope, cuan):
    def dead_technical(request):
        raise RuntimeError("boom")

    handlers = {
        "MarketAgent": market_handler_factory(envelope),
        **{f"TechnicalAgent:{c['facts']['ticker']}": technical_handler_factory(c)
           for c in envelope["candidates"]},
        **{f"FlowAgent:{c['facts']['ticker']}": flow_handler_factory(c)
           for c in envelope["candidates"]},
        "TechnicalAgent:CUAN.JK": dead_technical,       # exactly one failure
    }
    report = runner.run_agents(snapshot, FakeTransport(handlers))
    assert report.technical["CUAN.JK"].status == "FAILED"
    assert report.technical["BKSL.JK"].ok               # other agents unaffected
    assert report.flow["CUAN.JK"].ok
    assert report.run_state == "PARTIAL"
    assert any("CUAN.JK" in name for name in report.failed_agents())


# ---------------------------------------------------------------------------
# CUAN conflict fixture: material for Phase 6 challenge tests
# ---------------------------------------------------------------------------


def test_cuan_conflict_fixture_technical_bullish_flow_distribution(snapshot, envelope, cuan):
    transport = FakeTransport({
        "MarketAgent": market_handler_factory(envelope),
        "TechnicalAgent:CUAN.JK": technical_handler_factory(cuan),
        "FlowAgent:CUAN.JK": flow_handler_factory(cuan),
    })
    report = runner.run_agents(snapshot, transport, tickers=("CUAN.JK",))
    tech = report.technical_interpretation("CUAN.JK")
    flow = report.flow_interpretation("CUAN.JK")
    # The canonical Phase 6 trigger: bullish structure vs distribution flow.
    assert tech.trend == "BULLISH" and tech.setup == "PULLBACK"
    assert flow.flow == "DISTRIBUTION"
    assert tech.evidence_refs and flow.evidence_refs
    # Flow stays an indication: no certainty about who actually transacted.
    assert any("indication" in r.lower() for r in (*flow.evidence, *flow.risks))
    assert flow.available_indicators and not flow.missing_indicators


def test_cuan_conflict_values_match_the_fixture(snapshot, cuan):
    facts = cuan["facts"]
    flow = cuan["flow"]
    assert facts["setup"] == "PULLBACK"                     # EMA20>EMA50>EMA200 intact
    assert facts["ema20"] > facts["ema50"] > facts["ema200"]
    assert flow["mfi"] is not None and flow["mfi"] <= 5.0   # distribution extreme
    assert flow["cmf"] is not None and flow["cmf"] < 0.0


# ---------------------------------------------------------------------------
# Persistence through the Phase 2A store
# ---------------------------------------------------------------------------


def test_persist_agent_outputs_writes_raw_and_validated(tmp_path, snapshot, envelope, cuan):
    transport = FakeTransport({
        "MarketAgent": market_handler_factory(envelope),
        "TechnicalAgent:CUAN.JK": technical_handler_factory(cuan),
        "FlowAgent:CUAN.JK": flow_handler_factory(cuan),
    })
    report = runner.run_agents(snapshot, transport, tickers=("CUAN.JK",), run_id="run-p3")
    with AuditStore(tmp_path / "agents.sqlite3") as store:
        store.create_run(run_id="run-p3", snapshot=snapshot, config=CONFIG,
                         snapshot_payload=snapshot.payload())
        ids = runner.persist_agent_outputs(store, "run-p3", report)
        assert len(ids) == 3
        outputs = store.get_agent_outputs("run-p3")
        assert {o["agent_name"] for o in outputs} == {"MarketAgent", "TechnicalAgent", "FlowAgent"}
        assert all(o["validation_ok"] for o in outputs)
        by_agent = {o["agent_name"]: o for o in outputs}
        # Validated payloads round-trip into the contract objects.
        market = MarketInterpretation.from_payload(by_agent["MarketAgent"]["validated"])
        assert market.regime == "BULLISH"
        tech = TechnicalInterpretation.from_payload(by_agent["TechnicalAgent"]["validated"])
        assert tech.ticker == "CUAN.JK"
        flow = FlowInterpretation.from_payload(by_agent["FlowAgent"]["validated"])
        assert flow.flow == "DISTRIBUTION"
        # Prompt/usage metadata persisted (plan §7 Phase 3, item 7).
        assert by_agent["MarketAgent"]["prompt_hash"]
        assert by_agent["MarketAgent"]["latency_ms"] >= 0
        assert by_agent["MarketAgent"]["raw_response"].startswith("{")


def test_persist_agent_outputs_records_failures_not_gaps(tmp_path, snapshot, envelope, cuan):
    def dead_flow(request):
        raise RuntimeError("provider exploded with token sk-secret123456789")

    transport = FakeTransport({
        "MarketAgent": market_handler_factory(envelope),
        "TechnicalAgent:CUAN.JK": technical_handler_factory(cuan),
        "FlowAgent:CUAN.JK": dead_flow,
    })
    report = runner.run_agents(
        snapshot, transport, tickers=("CUAN.JK",),
        config=TransportConfig(max_attempts=1), run_id="run-p3f",
    )
    with AuditStore(tmp_path / "agents.sqlite3") as store:
        store.create_run(run_id="run-p3f", snapshot=snapshot, config=CONFIG,
                         snapshot_payload=snapshot.payload())
        runner.persist_agent_outputs(store, "run-p3f", report)
        outputs = store.get_agent_outputs("run-p3f")
        by_agent = {o["agent_name"]: o for o in outputs}
        assert by_agent["FlowAgent"]["validation_ok"] is False
        assert by_agent["FlowAgent"]["validated"] is None
        assert "RuntimeError" in by_agent["FlowAgent"]["validation_error"]
        # Secrets are redacted before anything hits the store.
        assert "sk-secret123456789" not in (by_agent["FlowAgent"]["validation_error"] or "")
        assert by_agent["TechnicalAgent"]["validation_ok"] is True   # isolation held


def test_agent_reports_do_not_leak_full_envelope(snapshot, envelope):
    """The Market request envelope must be the market slice only (item 4)."""
    transport = FakeTransport({"MarketAgent": market_handler_factory(envelope)})
    market_agent.run(transport, envelope)
    sent = transport.calls[0].envelope
    assert set(sent) == {"market"}
    assert "candidates" not in sent
