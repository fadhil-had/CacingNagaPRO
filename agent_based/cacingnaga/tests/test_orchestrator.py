"""Phase 4 — Decision, Conflict Handling, and orchestration (plan §7 Phase 4).

Exit criteria under test:
- a fixture run completes and persists without Telegram,
- READY requires mandatory inputs, a valid risk plan/trigger, and no
  unresolved hard conflict,
- zero-to-three READY recommendations satisfying every run invariant,
- a valid run with no qualified candidates returns NO_TRADE,
- identical facts/configuration and captured validated agent outputs produce
  identical final status, rank, and payload (replay determinism),
- a Decision Agent outage produces PARTIAL/NOT_EVALUATED, never NO_TRADE.

Conflict rules (conflicts.py) and the merge veto are unit-tested directly so
Phase 6 challenge tests can rely on the recorded conflict facts.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
sys.path.insert(0, str(BASE))

from cacingnaga.config import AIAnalystConfig
from cacingnaga.conflicts import (
    CONFLICT_RULES_VERSION,
    SEVERITY_HARD,
    SEVERITY_MATERIAL,
    SEVERITY_MINOR,
    Conflict,
    detect_conflicts,
    merge_severity,
)
from cacingnaga.errors import ContractViolation
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.snapshot import build_snapshot, snapshot_to_agent_envelope
from cacingnaga.store import AuditStore
from cacingnaga.transport import FakeTransport, TransportConfig

from agents.merge import merge_decision
from orchestrator import AnalysisService

CONFIG = AIAnalystConfig()


# ---------------------------------------------------------------------------
# Shared handler factories (deterministic validated readings)
# ---------------------------------------------------------------------------


def market_handler_factory(env, regime="BULLISH"):
    refs = env["market"]["evidence_refs"]

    def handler(request):
        return {
            "regime": regime,
            "confidence_band": "MEDIUM",
            "swing_environment": "FAVORABLE" if regime == "BULLISH" else "UNFAVORABLE",
            "secondary_direction": None,
            "reasons": [
                f"close above rising EMAs per {refs['close']} and {refs['ema20']}",
                f"breadth confirms participation per {refs['breadth50']}",
            ],
            "risk_flags": [f"market RSI stretched per {refs['rsi']}"],
            "evidence_refs": [refs["close"], refs["ema20"], refs["breadth50"], refs["rsi"]],
        }

    return handler


def technical_handler_factory(candidate):
    refs = candidate["evidence_refs"]

    def handler(request):
        return {
            "ticker": candidate["facts"]["ticker"],
            "trend": "BULLISH",
            "setup": candidate["facts"]["setup"],
            "momentum": "POSITIVE",
            "confidence_band": "MEDIUM",
            "reasons": [
                f"EMA structure bullish per {refs['ema20']} and {refs['ema50']}",
                f"price in the entry zone per {refs['price']}",
            ],
            "risks": [f"momentum context per {refs['macd_hist']}"],
            "evidence_refs": [refs["ema20"], refs["ema50"], refs["price"], refs["macd_hist"]],
            "missing_facts": [],
        }

    return handler


def flow_handler_factory(candidate, flow_state="ACCUMULATION"):
    refs = candidate["flow_evidence_refs"]

    def handler(request):
        return {
            "ticker": candidate["facts"]["ticker"],
            "flow": flow_state,
            "strength": "MEDIUM",
            "confidence_band": "MEDIUM",
            "evidence": [
                f"MFI per {refs['mfi']}; CMF per {refs['cmf']}; OBV per {refs['obv_slope']}"
            ],
            "risks": ["OHLCV flow is an indication only"],
            "evidence_refs": [refs["mfi"], refs["cmf"], refs["obv_slope"]],
            "available_indicators": list(candidate["flow"]["available_indicators"]),
            "missing_indicators": list(candidate["flow"]["missing_indicators"]),
        }

    return handler


def decision_handler_factory(proposals=None, with_reason=True):
    """proposals: optional {ticker: (status, reason)} override map."""

    def handler(request):
        candidate = request.envelope["candidates"][0]
        facts = candidate["facts"]
        refs = candidate["evidence_refs"]
        python_status = request.envelope["policy"]["python_status"]
        conflicts = request.envelope["conflicts"]
        status, reason = (proposals or {}).get(
            facts["ticker"], (python_status, "")
        )
        if status == python_status and conflicts and not reason:
            reason = f"conflict {conflicts[0]['rule_id']} acknowledged"
        return {
            "ticker": facts["ticker"],
            "proposed_status": status,
            "confidence_band": "MEDIUM",
            "status_change_reason": reason if status != python_status else "",
            "reasons": [
                f"python derives {python_status} per {refs.get('ema20', refs.get('setup', ''))}"
            ],
            "concerns": [
                f"conflict {c['rule_id']} per "
                + (c["evidence_refs"][-1] if c["evidence_refs"]
                   else refs.get("ema20", refs.get("setup", "")))
                for c in conflicts
            ]
            or [f"no conflicts cited per {refs.get('rsi', refs.get('mfi', refs.get('setup', '')))}"],
            "evidence_refs": [
                refs.get("ema20", refs.get("setup", "")),
                refs.get("price", refs.get("mfi", "")),
            ],
        }

    return handler


def full_transport(env, *, market_regime="BULLISH", decision_proposals=None,
                   dead_decision_for=(), dead_flow_for=()):
    handlers = {"MarketAgent": market_handler_factory(env, market_regime)}
    for c in env["candidates"]:
        ticker = c["facts"]["ticker"]
        handlers[f"TechnicalAgent:{ticker}"] = technical_handler_factory(c)
        handlers[f"FlowAgent:{ticker}"] = flow_handler_factory(
            c, "DISTRIBUTION" if ticker.startswith("CUAN") else "ACCUMULATION"
        )
        if ticker in dead_decision_for:
            def dead(request, _t=ticker):
                raise RuntimeError(f"decision provider down for {_t}")
            handlers[f"DecisionAgent:{ticker}"] = dead
        else:
            handlers[f"DecisionAgent:{ticker}"] = decision_handler_factory(
                decision_proposals
            )
        if ticker in dead_flow_for:
            def dead_flow(request, _t=ticker):
                raise RuntimeError(f"flow provider down for {_t}")
            handlers[f"FlowAgent:{ticker}"] = dead_flow
    return FakeTransport(handlers)


@pytest.fixture(scope="module")
def snapshot():
    return build_snapshot(market_frame(), default_frames(), CONFIG)


@pytest.fixture(scope="module")
def envelope(snapshot):
    return snapshot_to_agent_envelope(snapshot)


# ---------------------------------------------------------------------------
# Conflict rules (deterministic, versioned)
# ---------------------------------------------------------------------------


def test_conflict_rules_version_is_pinned():
    assert CONFLICT_RULES_VERSION == "CONFLICT_RULES_1"


def test_r1_technical_bullish_vs_flow_distribution_is_material(envelope, snapshot):
    cuan = next(c for c in envelope["candidates"] if c["facts"]["ticker"] == "CUAN.JK")
    facts = snapshot.candidate_by_ticker("CUAN.JK") if hasattr(snapshot, "candidate_by_ticker") else None
    facts = facts or next(f for f in snapshot.candidates if f.ticker == "CUAN.JK")
    flow_facts = next(f for f in snapshot.flows if f.ticker == "CUAN.JK")
    conflicts = detect_conflicts(
        "CUAN.JK",
        facts=facts,
        flow_facts=flow_facts,
        technical=type("T", (), {"trend": "BULLISH"})(),
        flow=type("F", (), {"flow": "DISTRIBUTION"})(),
        market=None,
    )
    assert [c.rule_id for c in conflicts] == ["R1"]
    assert conflicts[0].severity == SEVERITY_MATERIAL
    assert conflicts[0].status_effect == "CAP_AT_WAIT"
    assert conflicts[0].payload()["rules_version"] == CONFLICT_RULES_VERSION


def test_r3_bearish_market_vs_bullish_candidate_is_hard(envelope, snapshot):
    ticker = "BKSL.JK"
    facts = next(f for f in snapshot.candidates if f.ticker == ticker)
    flow_facts = next(f for f in snapshot.flows if f.ticker == ticker)
    conflicts = detect_conflicts(
        ticker,
        facts=facts,
        flow_facts=flow_facts,
        technical=type("T", (), {"trend": "BULLISH"})(),
        flow=type("F", (), {"flow": "ACCUMULATION"})(),
        market=type("M", (), {"regime": "BEARISH"})(),
    )
    assert any(c.rule_id == "R3" and c.severity == SEVERITY_HARD for c in conflicts)


def test_r2_bullish_claim_with_missing_core_facts_is_hard(envelope, snapshot):
    ticker = "BKSL.JK"
    facts = next(f for f in snapshot.candidates if f.ticker == ticker)
    flow_facts = next(f for f in snapshot.flows if f.ticker == ticker)
    broken = type("T", (), {"trend": "BULLISH"})()
    conflicts = detect_conflicts(
        ticker,
        facts=type("F", (), {"ema20": None, "ema50": None, "price": None})(),
        flow_facts=flow_facts,
        technical=broken,
        flow=None,
        market=None,
    )
    assert any(c.rule_id == "R2" and c.severity == SEVERITY_HARD for c in conflicts)


def test_r4_accumulation_without_directional_evidence_is_minor(snapshot):
    ticker = "BKSL.JK"
    facts = next(f for f in snapshot.candidates if f.ticker == ticker)
    flow_facts = type(
        "FF", (), {"available_indicators": (), "payload": lambda self: {}, "mfi": None, "cmf": None}
    )()
    conflicts = detect_conflicts(
        ticker,
        facts=facts,
        flow_facts=flow_facts,
        technical=None,
        flow=type("F", (), {"flow": "ACCUMULATION"})(),
        market=None,
    )
    assert any(c.rule_id == "R4" and c.severity == SEVERITY_MINOR for c in conflicts)


def test_benign_low_confidence_difference_triggers_no_conflict(snapshot):
    """A benign divergence (no rule matches) must not trigger a challenge."""
    ticker = "BKSL.JK"
    facts = next(f for f in snapshot.candidates if f.ticker == ticker)
    flow_facts = next(f for f in snapshot.flows if f.ticker == ticker)
    conflicts = detect_conflicts(
        ticker,
        facts=facts,
        flow_facts=flow_facts,
        technical=type("T", (), {"trend": "NEUTRAL"})(),
        flow=type("F", (), {"flow": "ACCUMULATION"})(),
        market=type("M", (), {"regime": "BULLISH"})(),
    )
    assert conflicts == ()


def test_merge_severity_orders_hard_over_material_over_minor():
    def conflict(severity):
        return Conflict(rule_id="X", severity=severity, conflict_type="T",
                        ticker="A", description="d")
    assert merge_severity(()) is None
    assert merge_severity((conflict(SEVERITY_MINOR), conflict(SEVERITY_HARD))) == SEVERITY_HARD
    assert merge_severity((conflict(SEVERITY_MINOR), conflict(SEVERITY_MATERIAL))) == SEVERITY_MATERIAL


# ---------------------------------------------------------------------------
# Merge: veto, justified downgrades, failed-turn floor
# ---------------------------------------------------------------------------


def _outcome(ticker, status, score=0.6):
    from cacingnaga.policy import PolicyOutcome
    from cacingnaga.risk import RiskPlanResult

    return PolicyOutcome(
        ticker=ticker,
        final_status=status,
        agent_proposed_status=status,
        score=score,
        components={"technical": 0.5},
        hard_gate_failures=() if status == "READY" else ("gate: x",),
        reasons=("python reasons",),
        risks=(),
        risk_plan=RiskPlanResult(levels=None, valid=False, reject_reasons=("n/a",), rules_applied=()),
    )


def _decision(status, reason="because evidence says so"):
    from cacingnaga.contracts import DecisionInterpretation

    return DecisionInterpretation(
        ticker="X.JK",
        proposed_status=status,
        confidence_band="MEDIUM",
        status_change_reason=reason if status != "READY" else "",
        reasons=("r",),
        evidence_refs=("TechnicalFacts:0123456789abcdef:ema20",),
    )


def test_merge_vetoes_ready_upgrade_over_gated_out_candidate():
    record = merge_decision(_outcome("X.JK", "WAIT"), _decision("READY"), ())
    assert record.final_status == "WAIT"
    assert record.proposal_vetoed and not record.proposal_accepted
    assert "own the floor" in record.veto_reason


def test_merge_vetoes_wait_upgrade_over_reject():
    record = merge_decision(_outcome("X.JK", "REJECT"), _decision("WAIT"), ())
    assert record.final_status == "REJECT" and record.proposal_vetoed


def test_merge_accepts_justified_downgrade():
    record = merge_decision(_outcome("X.JK", "READY"), _decision("WAIT"), ())
    assert record.final_status == "WAIT" and record.proposal_accepted
    record = merge_decision(_outcome("X.JK", "WAIT"), _decision("REJECT"), ())
    assert record.final_status == "REJECT" and record.proposal_accepted


def test_merge_ignores_unjustified_downgrade():
    record = merge_decision(_outcome("X.JK", "READY"), _decision("WAIT", reason=""), ())
    assert record.final_status == "READY"
    assert "ignored" in record.merge_notes[-1]


def test_merge_failure_floor_keeps_python_status():
    record = merge_decision(_outcome("X.JK", "WAIT"), None, ())
    assert record.final_status == "WAIT"
    assert record.agent_proposed_status == "WAIT"    # recorded, not fabricated
    assert "deterministic status stands" in record.merge_notes[-1]


def test_merge_material_conflict_caps_ready_at_wait():
    conflict = Conflict(rule_id="R1", severity=SEVERITY_MATERIAL,
                        conflict_type="TECHNICAL_VS_FLOW", ticker="X.JK",
                        description="d", status_effect="CAP_AT_WAIT")
    record = merge_decision(_outcome("X.JK", "READY"), _decision("READY"), (conflict,))
    assert record.final_status == "WAIT"
    assert any("caps READY at WAIT" in n for n in record.merge_notes)


def test_merge_hard_conflict_blocks_ready():
    conflict = Conflict(rule_id="R3", severity=SEVERITY_HARD,
                        conflict_type="MARKET_VS_CANDIDATE", ticker="X.JK",
                        description="d", status_effect="BLOCK_READY")
    record = merge_decision(_outcome("X.JK", "READY"), _decision("READY"), (conflict,))
    assert record.final_status == "WAIT"


def test_merge_minor_conflict_never_changes_status():
    conflict = Conflict(rule_id="R4", severity=SEVERITY_MINOR,
                        conflict_type="STRENGTH_VS_EVIDENCE", ticker="X.JK",
                        description="d", status_effect="NONE")
    record = merge_decision(_outcome("X.JK", "READY"), _decision("READY"), (conflict,))
    assert record.final_status == "READY"


# ---------------------------------------------------------------------------
# AnalysisService end-to-end
# ---------------------------------------------------------------------------


def _service(tmp_path, snapshot, env, **transport_kwargs):
    store = AuditStore(tmp_path / "p4.sqlite3")
    transport = full_transport(env, **transport_kwargs)
    return AnalysisService(CONFIG, store, transport), store, transport


def test_full_run_completes_and_persists_without_telegram(tmp_path, snapshot, envelope):
    service, store, _ = _service(tmp_path, snapshot, envelope)
    result = service.run_full_analysis(snapshot, run_id="run-e2e")
    run = result.run_result
    assert run.run_status == "COMPLETE"
    run.validate()                                   # Section 6.10 invariants
    assert len(run.recommendations) <= 3
    assert [d.rank for d in run.recommendations] == list(range(1, len(run.recommendations) + 1))
    assert store.get_run("run-e2e").run_status == "COMPLETE"
    # Persisted: decisions + agent outputs + synthesis, no Telegram anywhere.
    assert store.get_decisions("run-e2e")
    agents_seen = {o["agent_name"] for o in store.get_agent_outputs("run-e2e")}
    assert "MarketAgent" in agents_seen and "TechnicalAgent" in agents_seen
    assert "FlowAgent" in agents_seen and "DecisionAgent" in agents_seen
    assert "AnalysisService" in agents_seen          # synthesis record
    # CUAN produced its material conflict record.
    assert any(c.rule_id == "R1" and c.ticker == "CUAN.JK" for c in result.conflicts)


def test_no_qualified_candidates_returns_no_trade(tmp_path, snapshot, envelope):
    # Bearish market hard-conflicts every bullish candidate; nothing qualifies.
    service, _, _ = _service(tmp_path, snapshot, envelope, market_regime="BEARISH")
    result = service.run_full_analysis(snapshot, run_id="run-notrade")
    run = result.run_result
    assert run.run_status == "COMPLETE"
    assert run.decision_outcome == "NO_TRADE"
    assert run.recommendations == ()
    run.validate()


def test_decision_agent_outage_degrades_to_partial_not_no_trade(tmp_path, snapshot, envelope):
    service, store, _ = _service(
        tmp_path, snapshot, envelope, dead_decision_for=("BKSL.JK",)
    )
    result = service.run_full_analysis(snapshot, run_id="run-outage")
    run = result.run_result
    assert run.run_status == "PARTIAL"
    assert run.decision_outcome == "NOT_EVALUATED"   # never NO_TRADE (§4 item 7)
    assert run.recommendations == ()
    assert store.get_run("run-outage").run_status == "PARTIAL"
    # The failure is persisted as a record, not a gap.
    outputs = store.get_agent_outputs("run-outage")
    failed = [o for o in outputs if o["agent_name"] == "DecisionAgent"
              and not o["validation_ok"]]
    assert failed and "RuntimeError" in failed[0]["validation_error"]


def test_flow_agent_failure_degrades_to_partial(tmp_path, snapshot, envelope):
    service, _, _ = _service(tmp_path, snapshot, envelope, dead_flow_for=("ERTX.JK",))
    result = service.run_full_analysis(snapshot, run_id="run-flowout")
    assert result.run_result.run_status == "PARTIAL"
    assert result.run_result.decision_outcome == "NOT_EVALUATED"


def test_replay_is_deterministic_with_zero_transport_calls(tmp_path, snapshot, envelope):
    service, store, transport = _service(tmp_path, snapshot, envelope)
    first = service.run_full_analysis(snapshot, run_id="run-replay")
    calls_after_first = len(transport.calls)
    assert calls_after_first > 0

    # Same snapshot/config, different trigger/run id: the store dedups, the
    # service replays from the stored synthesis with zero provider calls.
    replay_service, _, replay_transport = _service(tmp_path, snapshot, envelope)
    second = replay_service.run_full_analysis(
        snapshot, run_id="run-replay-2", trigger="schedule"
    )
    assert second.replayed is True
    assert len(replay_transport.calls) == 0
    a, b = first.payload(), second.payload()
    assert a["run_result"] == b["run_result"]
    assert a["decision_records"] == b["decision_records"]
    assert a["conflicts"] == b["conflicts"]


def test_degraded_replay_stays_degraded(tmp_path, snapshot, envelope):
    service, _, _ = _service(tmp_path, snapshot, envelope, dead_flow_for=("ERTX.JK",))
    first = service.run_full_analysis(snapshot, run_id="run-deg")
    assert first.run_result.run_status == "PARTIAL"
    again = service.run_full_analysis(snapshot, run_id="run-deg-2")
    assert again.replayed and again.run_result.run_status == "PARTIAL"
    assert again.run_result.decision_outcome == "NOT_EVALUATED"


def test_snapshot_integrity_checked_before_any_agent_call(tmp_path, snapshot, envelope):
    import dataclasses

    service, _, transport = _service(tmp_path, snapshot, envelope)
    tampered = dataclasses.replace(
        snapshot, analysis_date="2030-01-01"
    )  # payload no longer matches the pinned hash? (analysis_date IS in payload)
    # Build a truly tampered snapshot: swap the hash.
    from cacingnaga.canonical import canonical_hash

    tampered = dataclasses.replace(snapshot, data_snapshot_hash="0" * 64)
    with pytest.raises(ContractViolation):
        service.run_full_analysis(tampered, run_id="run-tamper")
    assert len(transport.calls) == 0                 # no agent call happened


def test_decision_agent_cannot_upgrade_python_status(tmp_path, snapshot, envelope):
    # Propose READY for everyone; Python's gates still own the outcome.
    service, _, _ = _service(
        tmp_path, snapshot, envelope,
        decision_proposals={t: ("READY", "") for t in
                            ("BKSL.JK", "ERTX.JK", "CUAN.JK", "DEWA.JK")},
    )
    result = service.run_full_analysis(snapshot, run_id="run-veto")
    run = result.run_result
    vetoed = [r for r in result.decision_records if r.proposal_vetoed]
    assert vetoed, "gated-out READY proposals must be vetoed"
    for record in vetoed:
        outcome_row = next(
            d for d in (*run.recommendations, *run.wait, *run.rejected)
            if d.ticker == record.ticker
        )
        # The veto keeps Python's own derived status, whatever it was.
        assert record.final_status == record.python_status
        assert record.final_status != "READY"
        assert outcome_row.final_status == record.final_status
        assert outcome_row.agent_proposed_status == "READY"   # recorded
        assert outcome_row.rank is None                       # not promoted


def test_agent_proposed_status_is_recorded_not_decisive(tmp_path, snapshot, envelope):
    proposals = {t: ("REJECT", "flow suggests caution") for t in
                 ("BKSL.JK", "ERTX.JK", "CUAN.JK", "DEWA.JK")}
    service, _, _ = _service(tmp_path, snapshot, envelope, decision_proposals=proposals)
    result = service.run_full_analysis(snapshot, run_id="run-proposal")
    for record in result.decision_records:
        assert record.agent_proposed_status == "REJECT"
    # Python's READY candidates keep their status (justified downgrade only).
    statuses = {d.ticker: d.final_status for d in
                (*result.run_result.recommendations, *result.run_result.wait,
                 *result.run_result.rejected)}
    favor = {"READY": 2, "WAIT": 1, "REJECT": 0}
    for record in result.decision_records:
        row_status = statuses[record.ticker]
        # Record and stored row agree; proposal recorded verbatim.
        assert row_status == record.final_status
        # Never more favorable than Python's derivation (no upgrades), and
        # every accepted downgrade was justified (reason present in proposals).
        assert favor[row_status] <= favor[record.python_status]
        if record.proposal_vetoed:
            assert row_status == record.python_status


def test_market_interpretation_flows_into_run_result(tmp_path, snapshot, envelope):
    service, _, _ = _service(tmp_path, snapshot, envelope, market_regime="BEARISH")
    result = service.run_full_analysis(snapshot, run_id="run-mkt")
    assert result.run_result.market.regime == "BEARISH"


def test_service_rejects_new_tickers_in_decision_packet(tmp_path, snapshot, envelope):
    """A decision response mutating the ticker cannot pass validation."""
    def hostile(request):
        candidate = request.envelope["candidates"][0]
        return {
            "ticker": "OTHER.JK",
            "proposed_status": "READY",
            "confidence_band": "LOW",
            "status_change_reason": "",
            "reasons": ["r"],
            "concerns": ["c"],
            "evidence_refs": ["TechnicalFacts:0123456789abcdef:ema20"],
        }

    handlers = {"MarketAgent": market_handler_factory(envelope)}
    for c in envelope["candidates"]:
        t = c["facts"]["ticker"]
        handlers[f"TechnicalAgent:{t}"] = technical_handler_factory(c)
        handlers[f"FlowAgent:{t}"] = flow_handler_factory(c)
        handlers[f"DecisionAgent:{t}"] = hostile
    store = AuditStore(tmp_path / "p4h.sqlite3")
    service = AnalysisService(CONFIG, store, FakeTransport(handlers))
    result = service.run_full_analysis(snapshot, run_id="run-hostile")
    assert result.run_result.run_status == "PARTIAL"   # bounded failure, isolated
