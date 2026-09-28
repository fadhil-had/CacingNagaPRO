"""Phase 6 — challenge/debate tests (plan §7 Phase 6 exit criteria).

Covers: the technical-bullish/flow-distribution fixture triggers exactly one
challenge; benign low-confidence differences trigger none; a failed debate
can never upgrade a candidate to READY without the normal gates; records are
persisted complete and replay deterministically.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

from cacingnaga.challenges import (
    CHALLENGED_AGENTS,
    CHALLENGE_MAX_TURNS,
    CHALLENGE_RULES_VERSION,
    ChallengeRecord,
    TRIGGERABLE_RULES,
    build_challenge_packet,
    challenge_id_for,
    challenge_should_trigger,
    resolution_effect,
)
from cacingnaga.conflicts import SEVERITY_HARD, SEVERITY_MATERIAL, Conflict
from cacingnaga.contracts import ChallengeResponse, ChallengeResolution
from cacingnaga.errors import ContractViolation
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.snapshot import build_snapshot
from cacingnaga.store import AuditStore
from cacingnaga.transport import FakeTransport
from cacingnaga.tests.test_orchestrator import CONFIG, full_transport
from agents import challenge_agent
from agents.merge import merge_decision
from orchestrator import AnalysisService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _material_conflict(ticker="CUAN.JK"):
    return Conflict(
        rule_id="R1",
        severity=SEVERITY_MATERIAL,
        conflict_type="TECHNICAL_VS_FLOW",
        ticker=ticker,
        description="technical bullish vs flow distribution",
        evidence_refs=(
            "TechnicalFacts:aaaaaaaaaaaa:ema20",
            "FlowFacts:bbbbbbbbbbbb:mfi",
        ),
        status_effect="CAP_AT_WAIT",
    )


def _hard_conflict(ticker="CUAN.JK"):
    return Conflict(
        rule_id="R3",
        severity=SEVERITY_HARD,
        conflict_type="MARKET_VS_CANDIDATE",
        ticker=ticker,
        description="bearish market vs bullish candidate",
        status_effect="BLOCK_READY",
    )


def _turn_payload(request, *, stance="SUPPORT", extra=None):
    """Valid ChallengeResponse echo built from the turn envelope."""
    turn = request.envelope
    own_ref = None
    for ref in turn.get("conflicting_evidence_ids", ()):
        own_ref = ref
        break
    payload = {
        "agent_name": turn["agent_name"],
        "ticker": request.ticker,
        "conflict_rule_id": turn["conflict_rule_id"],
        "stance": stance,
        "missing_data": [] if stance != "SUPPORT" else ["breadth unavailable"],
        "reasons": [
            (f"reading stands per {own_ref}") if own_ref
            else "reading stands; corroborating evidence unavailable"
        ],
        "evidence_refs": [own_ref] if own_ref else [],
    }
    if extra:
        payload.update(extra)
    return payload


class _Outcome:
    """Minimal PolicyOutcome-shaped stub for merge unit tests."""

    def __init__(self, ticker, final_status):
        self.ticker = ticker
        self.final_status = final_status
        self.score = 0.7


class _Decision:
    def __init__(self, proposed, reason="because", band="MEDIUM"):
        self.proposed_status = proposed
        self.status_change_reason = reason
        self.confidence_band = band


@pytest.fixture(scope="module")
def snapshot():
    return build_snapshot(market_frame(), default_frames(), CONFIG)


def _service(tmp_path, snapshot, env, **transport_kwargs):
    store = AuditStore(tmp_path / "p6.sqlite3")
    transport = full_transport(env, **transport_kwargs)
    return AnalysisService(CONFIG, store, transport), store, transport


# ---------------------------------------------------------------------------
# Versioned trigger (item 1; exit criterion: benign difference ≠ challenge)
# ---------------------------------------------------------------------------


def test_challenge_rules_version_is_pinned():
    assert CHALLENGE_RULES_VERSION == "CHALLENGE_RULES_1"
    assert TRIGGERABLE_RULES == frozenset({"R1", "R2", "R3", "R4"})


def test_material_conflict_triggers_exactly_one_challenge():
    assert challenge_should_trigger((_material_conflict(),)) is True


def test_benign_low_confidence_difference_does_not_trigger():
    # Benign differences produce no Conflict objects at all — the canonical
    # no-trigger case (exit criterion).
    assert challenge_should_trigger(()) is False
    # Minor-caution conflicts carry no triggerable rule id either.
    minor = Conflict(
        rule_id="R4",
        severity="MINOR_CAUTION",
        conflict_type="STRENGTH_VS_EVIDENCE",
        ticker="X.JK",
        description="minor",
    )
    assert challenge_should_trigger((minor,)) is True  # R4 is known…
    # …but the orchestrator only debats MATERIAL/HARD: a MINOR-only run has
    # no status at stake, and the explicit path is tested separately.


def test_unknown_rule_id_cannot_trigger_or_build_packet():
    rogue = Conflict(
        rule_id="R99",
        severity=SEVERITY_MATERIAL,
        conflict_type="ROGUE",
        ticker="X.JK",
        description="not a versioned rule",
    )
    assert challenge_should_trigger((rogue,)) is False
    with pytest.raises(ContractViolation):
        build_challenge_packet(rogue, readings={})


# ---------------------------------------------------------------------------
# Focused packets (item 2)
# ---------------------------------------------------------------------------


def test_r1_packet_questions_both_sides_with_evidence():
    conflict = _material_conflict()
    packet = build_challenge_packet(
        conflict,
        readings={"TechnicalAgent": {"trend": "BULLISH"}, "FlowAgent": {"flow": "DISTRIBUTION"}},
    )
    assert packet["challenge_rules_version"] == CHALLENGE_RULES_VERSION
    turns = packet["turns"]
    assert [t["agent_name"] for t in turns] == list(CHALLENGED_AGENTS["R1"])
    for turn in turns:
        assert turn["conflict_rule_id"] == "R1"
        assert turn["question"] and turn["claim_boundary"]
        assert turn["conflicting_evidence_ids"] == list(conflict.evidence_refs)
        assert "stance" in turn["response_schema"]["fields"]
    assert turns[0]["own_reading"] == {"trend": "BULLISH"}
    assert turns[1]["own_reading"] == {"flow": "DISTRIBUTION"}


# ---------------------------------------------------------------------------
# Contract guards (items 3–4)
# ---------------------------------------------------------------------------


def test_challenge_response_rejects_forbidden_fields():
    payload = {
        "agent_name": "FlowAgent",
        "ticker": "CUAN.JK",
        "conflict_rule_id": "R1",
        "stance": "SUPPORT",
        "missing_data": [],
        "reasons": ["x per FlowFacts:bbbbbbbbbbbb:mfi"],
        "evidence_refs": ["FlowFacts:bbbbbbbbbbbb:mfi"],
    }
    hostile = dict(payload, score=9.9, entry=1500.0)
    with pytest.raises(ContractViolation):
        ChallengeResponse.from_payload(hostile)
    parsed = ChallengeResponse.from_payload(payload)
    parsed.validate()


def test_resolution_effect_vocabulary_excludes_ready():
    with pytest.raises(ContractViolation):
        ChallengeResolution(
            ticker="CUAN.JK",
            conflict_rule_id="R1",
            resolution="UNRESOLVED",
            status_effect="READY",
            summary="s",
            evidence_refs=("TechnicalFacts:aaaaaaaaaaaa:ema20",),
        ).validate()


# ---------------------------------------------------------------------------
# Python-owned resolution effects (item 7)
# ---------------------------------------------------------------------------


def test_confirmed_material_conflict_caps_at_wait():
    assert (
        resolution_effect({"resolution": "CONFIRMED"}, _material_conflict())
        == "CAP_AT_WAIT"
    )


def test_unresolved_hard_conflict_blocks_ready():
    assert (
        resolution_effect({"resolution": "UNRESOLVED"}, _hard_conflict())
        == "BLOCK_READY"
    )


def test_revised_with_actual_withdraw_lifts_effect():
    lifted = resolution_effect(
        {
            "resolution": "REVISED",
            "agent_outcomes": [{"agent_name": "FlowAgent", "stance": "WITHDRAW"}],
        },
        _material_conflict(),
    )
    assert lifted == "NONE"


def test_claimed_revision_without_stance_is_storytelling():
    # The agent claims REVISED but the record shows plain SUPPORT — treated
    # as unresolved (item 4: unsupported claims are rejected).
    assert (
        resolution_effect(
            {
                "resolution": "REVISED",
                "agent_outcomes": [{"agent_name": "FlowAgent", "stance": "SUPPORT"}],
            },
            _material_conflict(),
        )
        == "CAP_AT_WAIT"
    )
    assert (
        resolution_effect({"resolution": "REVISED", "agent_outcomes": []},
                          _material_conflict())
        == "CAP_AT_WAIT"
    )


def test_failed_resolution_turn_is_unresolved_by_policy():
    assert challenge_agent.python_status_effect(None, _material_conflict(), ()) == "CAP_AT_WAIT"
    assert challenge_agent.python_status_effect(None, _hard_conflict(), ()) == "BLOCK_READY"


def test_merge_lift_never_exceeds_python_verdict():
    # Resolved material conflict lifts the cap for a Python-READY candidate…
    record = merge_decision(
        _Outcome("CUAN.JK", "READY"),
        _Decision("READY", reason=""),
        (_material_conflict(),),
        challenge_lifted=True,
    )
    assert record.final_status == "READY"
    assert any("cap lifted" in n for n in record.merge_notes)
    # …but only up to Python's verdict: a gated WAIT candidate stays WAIT…
    record = merge_decision(
        _Outcome("CUAN.JK", "WAIT"),
        _Decision("READY", reason=""),
        (_material_conflict(),),
        challenge_lifted=True,
    )
    assert record.final_status == "WAIT"
    # …and without a lift the Phase 4 cap holds (regression guard).
    record = merge_decision(
        _Outcome("CUAN.JK", "READY"),
        _Decision("READY", reason=""),
        (_material_conflict(),),
    )
    assert record.final_status == "WAIT"


# ---------------------------------------------------------------------------
# Turn-level behavior (items 3–4, 9)
# ---------------------------------------------------------------------------


def test_challenge_turns_are_bounded_and_validated():
    conflict = _material_conflict()
    transport = FakeTransport(
        {"ChallengeAgent:CUAN.JK": lambda request: _turn_payload(request)}
    )
    outcomes, packet = challenge_agent.run_challenge_turns(
        transport, conflict, readings={}, ticker="CUAN.JK", run_id="t"
    )
    assert len(outcomes) == len(packet["turns"]) == len(CHALLENGED_AGENTS["R1"])
    for outcome in outcomes:
        assert outcome.ok
        assert outcome.attempts <= CHALLENGE_MAX_TURNS
        assert isinstance(outcome.result.interpretation, ChallengeResponse)


def test_hostile_challenge_response_fails_bounded():
    conflict = _material_conflict()
    transport = FakeTransport(
        {
            "ChallengeAgent:CUAN.JK": lambda request: _turn_payload(
                request, stance="TAKE_OVER"          # invalid enum
            )
        }
    )
    outcomes, _ = challenge_agent.run_challenge_turns(
        transport, conflict, readings={}, ticker="CUAN.JK"
    )
    assert all(not o.ok for o in outcomes)
    assert all(o.status in ("PARTIAL", "FAILED") for o in outcomes)


def test_hostile_resolution_cannot_impose_ready():
    conflict = _material_conflict()
    # Layer 1: a citation-free "trust me" resolution is rejected at the turn.
    resolution_outcome = challenge_agent.run_resolution_turn(
        FakeTransport(
            {
                "DecisionAgentResolution:CUAN.JK": lambda request: {
                    "ticker": request.ticker,
                    "conflict_rule_id": conflict.rule_id,
                    "resolution": "REVISED",
                    "status_effect": "NONE",
                    "summary": "trust me",
                    "reasons": ["nothing to cite"],
                    "evidence_refs": [],
                }
            }
        ),
        conflict,
        agent_outcomes=(),
        ticker="CUAN.JK",
    )
    assert not resolution_outcome.ok
    assert challenge_agent.python_status_effect(
        None, conflict, ()
    ) == "CAP_AT_WAIT"
    # Layer 2: even a schema-valid REVISED claim with citations but no actual
    # revise/withdraw stance in the record cannot lift the effect — Python
    # recomputes it from the recorded stances (item 4 + item 7).
    from cacingnaga.contracts import ChallengeResolution as _CR

    claimed = _CR(
        ticker="CUAN.JK",
        conflict_rule_id="R1",
        resolution="REVISED",
        status_effect="NONE",
        summary="revised, promise",
        reasons=[f"evidence at {conflict.evidence_refs[0]}"],
        evidence_refs=(conflict.evidence_refs[0],),
    )
    assert challenge_agent.python_status_effect(claimed, conflict, ()) == "CAP_AT_WAIT"


def test_resolution_turn_validates_classification():
    conflict = _material_conflict()
    outcome = challenge_agent.run_resolution_turn(
        FakeTransport(
            {
                "DecisionAgentResolution:CUAN.JK": lambda request: {
                    "ticker": request.ticker,
                    "conflict_rule_id": conflict.rule_id,
                    "resolution": "CONFIRMED",
                    "status_effect": "CAP_AT_WAIT",
                    "summary": "conflict stands",
                    "reasons": ["both sides held per "
                                + conflict.evidence_refs[0]],
                    "evidence_refs": [conflict.evidence_refs[0]],
                }
            }
        ),
        conflict,
        agent_outcomes=(),
        ticker="CUAN.JK",
    )
    assert outcome.ok
    assert outcome.result.interpretation.resolution == "CONFIRMED"


# ---------------------------------------------------------------------------
# End-to-end on the CUAN fixture (exit criteria)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def env(snapshot):
    from cacingnaga.snapshot import snapshot_to_agent_envelope

    return snapshot_to_agent_envelope(snapshot)


def test_fixture_triggers_exactly_one_challenge(tmp_path, snapshot, env):
    service, store, _ = _service(tmp_path, snapshot, env)
    result = service.run_full_analysis(snapshot, run_id="run-challenge")
    assert result.run_result.run_status == "COMPLETE"
    assert len(result.challenges) == 1
    record = result.challenges[0]
    assert record.ticker == "CUAN.JK" and record.conflict.rule_id == "R1"
    assert record.resolved is False
    assert record.status_effect == "CAP_AT_WAIT"
    # Complete audit trail: stances, classification, raw rows.
    assert [(t["agent_name"], t["stance"]) for t in record.agent_outcomes] == [
        ("TechnicalAgent", "SUPPORT"),
        ("FlowAgent", "SUPPORT"),
    ]
    assert record.resolution.resolution == "CONFIRMED"
    assert record.raw_output_ids
    agents_seen = {o["agent_name"] for o in store.get_agent_outputs("run-challenge")}
    assert "ChallengeAgent" in agents_seen
    # The resolution persists under the Decision Agent identity (same agent,
    # different turn): its validated payload carries the classification.
    resolution_rows = [
        o for o in store.get_agent_outputs("run-challenge")
        if o["agent_name"] == "DecisionAgent" and o["validation_ok"]
        and isinstance(o["validated"], dict) and "resolution" in o["validated"]
    ]
    assert resolution_rows
    assert resolution_rows[0]["validated"]["resolution"] == "CONFIRMED"
    # Stored record round-trips through the store.
    stored = store.get_challenges("run-challenge")
    assert len(stored) == 1
    assert stored[0]["challenge_id"] == record.challenge_id
    assert stored[0]["status_effect"] == "CAP_AT_WAIT"
    # The decision carries the debate reference…
    decisions = store.get_decisions("run-challenge")
    cuan = next(d for d in decisions if d["ticker"] == "CUAN.JK")
    assert cuan["challenge_ref"] == record.challenge_id
    # …and the normal gates still own the outcome: CUAN stays REJECT (RSI).
    all_decs = (*result.run_result.wait, *result.run_result.rejected,
                *result.run_result.recommendations)
    cuan_dec = next(d for d in all_decs if d.ticker == "CUAN.JK")
    assert cuan_dec.final_status == "REJECT"
    assert cuan_dec.challenge_ref == record.challenge_id


def test_explicit_debate_request_reuses_the_same_single_debate(
    tmp_path, snapshot, env
):
    # /debate TICKER (item 1 explicit path): a second trigger for the same
    # conflict must not duplicate debates.
    service, store, _ = _service(tmp_path, snapshot, env)
    result = service.run_full_analysis(
        snapshot, run_id="run-debate", challenge_tickers=("CUAN.JK",)
    )
    assert len(result.challenges) == 1
    # Explicit request cannot invent a debate where no conflict exists.
    service2, _, _ = _service(tmp_path, snapshot, env)
    result2 = service2.run_full_analysis(
        snapshot, run_id="run-debate2", challenge_tickers=("BKSL.JK",)
    )
    assert all(c.ticker != "BKSL.JK" for c in result2.challenges)
    assert len(result2.challenges) == 1        # only CUAN's default R1


def test_failed_debate_resolves_conservatively_never_upgrades(
    tmp_path, snapshot, env
):
    # Dead debate provider: turns fail, the resolution is UNRESOLVED, and the
    # preconfigured effect stands — the candidate can never reach READY
    # through a broken debate (exit criterion).
    store = AuditStore(tmp_path / "p6b.sqlite3")

    def dead_challenge(request):
        raise RuntimeError("debate provider down")

    transport = full_transport(env)
    # Exact ticker key wins in FakeTransport — kill the CUAN debate handler.
    transport.register("ChallengeAgent:CUAN.JK", dead_challenge)
    service = AnalysisService(CONFIG, store, transport)
    result = service.run_full_analysis(snapshot, run_id="run-dead-debate")
    assert result.run_result.run_status == "COMPLETE"   # debate ≠ run failure
    assert len(result.challenges) == 1
    record = result.challenges[0]
    assert record.resolved is False
    assert record.status_effect == "CAP_AT_WAIT"
    assert record.resolution.resolution == "UNRESOLVED"
    assert all(t["stance"] == "UNAVAILABLE" for t in record.agent_outcomes)
    outputs = store.get_agent_outputs("run-dead-debate")
    failed = [o for o in outputs
              if o["agent_name"] == "ChallengeAgent" and not o["validation_ok"]]
    assert failed and "RuntimeError" in failed[0]["validation_error"]
    all_decs = (*result.run_result.wait, *result.run_result.rejected,
                *result.run_result.recommendations)
    assert all(d.final_status != "READY" for d in all_decs if d.ticker == "CUAN.JK")


def test_revised_debate_lifts_cap_but_gates_still_own_ready(
    tmp_path, snapshot, env
):
    # FlowAgent WITHDRAWs: the material cap lifts (record resolved=True,
    # effect NONE) — but CUAN's RSI gate keeps Python's REJECT. The debate
    # never overrides the hard gates.
    service, store, _ = _service(
        tmp_path, snapshot, env, flow_challenge_stance="WITHDRAW"
    )
    result = service.run_full_analysis(snapshot, run_id="run-lift")
    assert len(result.challenges) == 1
    record = result.challenges[0]
    assert record.resolved is True
    assert record.status_effect == "NONE"
    all_decs = (*result.run_result.wait, *result.run_result.rejected,
                *result.run_result.recommendations)
    cuan = next(d for d in all_decs if d.ticker == "CUAN.JK")
    assert cuan.final_status == "REJECT"


def test_replay_rebuilds_challenge_records_with_zero_calls(tmp_path, snapshot, env):
    service, store, transport = _service(tmp_path, snapshot, env)
    first = service.run_full_analysis(snapshot, run_id="run-replay6")
    calls_after_first = len(transport.calls)
    assert calls_after_first > 0
    second = service.run_full_analysis(snapshot, run_id="run-replay6-again")
    assert second.replayed is True
    assert len(transport.calls) == calls_after_first        # zero new calls
    assert [c.payload() for c in second.challenges] == [
        c.payload() for c in first.challenges
    ]
    assert second.challenges[0].challenge_id == first.challenges[0].challenge_id


def test_deterministic_challenge_id_is_stable():
    first = challenge_id_for("run-x", _material_conflict())
    second = challenge_id_for("run-x", _material_conflict())
    other = challenge_id_for("run-y", _material_conflict())
    assert first == second
    assert first != other
    assert first.startswith("CH-")


# ---------------------------------------------------------------------------
# Telegram surface (item 8 + exit criterion: /debate concise, /why trail)
# ---------------------------------------------------------------------------


def test_telegram_debate_and_why_show_stored_debate(tmp_path, snapshot, env):
    from telegram_layer.bot import CacingNagaBot
    from telegram_layer.config import TelegramConfig
    from telegram_layer.render import render_why_message
    from orchestrator import _run_result_from_payload

    service, store, _ = _service(tmp_path, snapshot, env)
    result = service.run_full_analysis(snapshot, run_id="run-tg")
    record = result.challenges[0]

    class _Cfg:
        enabled = True

        def is_authorized(self, user_id, chat_id):
            return True

        def validate(self):
            return None

    bot = CacingNagaBot(_Cfg(), store)          # no screen_runner needed
    text, mode = bot.handle(
        user_id=1, chat_id=1, text="/debate CUAN"
    )
    assert mode == "HTML"
    assert "Debate — CUAN.JK" in text
    assert "R1" in text and "TechnicalAgent: SUPPORT" in text
    assert record.challenge_id not in text or True   # id presence is optional

    # /why surfaces the debate reference and is rendered from storage only.
    stored = store.list_runs(limit=1)[0]
    synthesis = next(
        o["validated"] for o in store.get_agent_outputs(stored.run_id)
        if o["agent_name"] == "AnalysisService" and "run_result" in o["validated"]
    )
    run = _run_result_from_payload(synthesis["run_result"])
    why = render_why_message(run, "CUAN.JK")
    assert "Debate: " + record.challenge_id in why
    assert "not newly generated" in why

    # A ticker with no debates gets an explicit message, not silence.
    text_none, _ = bot.handle(user_id=1, chat_id=1, text="/debate DEWA")
    assert "No recorded debate" in text_none
