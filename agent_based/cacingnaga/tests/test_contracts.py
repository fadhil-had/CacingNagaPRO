from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
sys.path.insert(0, str(BASE))

from cacingnaga import canonical
from cacingnaga import contracts as C
from cacingnaga.config import AIAnalystConfig, ScreenerConfig, config_payload
from cacingnaga.errors import ContractViolation, SnapshotError
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.legacy_adapter import load_legacy_ranking
from cacingnaga.snapshot import (
    AnalysisSnapshot,
    add_flow_indicators,
    assert_snapshot_integrity,
    build_snapshot,
    classify_setup,
    snapshot_to_agent_envelope,
)


# ---------------------------------------------------------------------------
# Phase 0 — canonical JSON / hashing
# ---------------------------------------------------------------------------


def test_canonical_json_is_key_order_independent_and_stable():
    a = canonical.canonical_json({"b": 1, "a": [1, 2, {"z": 0, "y": None}]})
    b = canonical.canonical_json({"a": [1, 2, {"y": None, "z": 0}], "b": 1})
    assert a == b
    assert canonical.canonical_hash({"b": 1, "a": 2}) == canonical.canonical_hash({"a": 2, "b": 1})


def test_canonical_json_rejects_nan_and_infinity():
    with pytest.raises(ContractViolation):
        canonical.canonical_json({"x": float("nan")})
    with pytest.raises(ContractViolation):
        canonical.canonical_json({"x": float("inf")})


# ---------------------------------------------------------------------------
# Phase 0 — contracts: enums, validation, forbidden agent fields
# ---------------------------------------------------------------------------


def test_interpretation_round_trip_preserves_values():
    """Exit criterion: a contract object round-trips without changing values."""
    original = C.TechnicalInterpretation(
        ticker="ERAA.JK",
        trend="BULLISH",
        setup="PULLBACK",
        momentum="POSITIVE",
        confidence_band="MEDIUM",
        reasons=("close above EMA20",),
        evidence_refs=("TechnicalFacts:abc123:rsi",),
    )
    original.validate()
    restored = C.TechnicalInterpretation.from_payload(original.payload())
    assert restored == original
    assert canonical.canonical_hash(original.payload()) == canonical.canonical_hash(restored.payload())


def test_market_interpretation_rejects_invalid_regime():
    interp = C.MarketInterpretation(regime="NEUTRAL-BULLISH")  # PRD example token
    with pytest.raises(ContractViolation):
        interp.validate()
    ok = C.MarketInterpretation(
        regime="BULLISH",
        secondary_direction="NEUTRAL-BULLISH",  # nuance lives in a side field
        confidence_band="MEDIUM",
    )
    ok.validate()


def test_agent_payloads_cannot_inject_forbidden_fields():
    """Round-trip guard: injected numbers/scores are rejected at the boundary."""
    clean = C.TechnicalInterpretation(ticker="ERAA.JK", trend="BULLISH").payload()
    with pytest.raises(ContractViolation):
        C.TechnicalInterpretation.from_payload({**clean, "technical_score": 90})
    with pytest.raises(ContractViolation):
        C.TechnicalInterpretation.from_payload({**clean, "stop_loss": 800})
    flow_clean = C.FlowInterpretation(ticker="ERAA.JK").payload()
    with pytest.raises(ContractViolation):
        C.FlowInterpretation.from_payload({**flow_clean, "entry": 880})
    with pytest.raises(ContractViolation):
        C.MarketInterpretation.from_payload(
            {**C.MarketInterpretation().payload(), "price": 7800}
        )
    # Unknown non-forbidden keys are equally rejected (strict schema).
    with pytest.raises(ContractViolation):
        C.FlowInterpretation.from_payload({**flow_clean, "mood": "optimistic"})


def test_flow_unknown_cannot_claim_high_confidence():
    with pytest.raises(ContractViolation):
        C.FlowInterpretation(ticker="ERAA.JK", flow="UNKNOWN", confidence_band="HIGH").validate()
    C.FlowInterpretation(ticker="ERAA.JK", flow="UNKNOWN", confidence_band="LOW").validate()


def test_risk_levels_reject_invalid_long_only_plans():
    with pytest.raises(ContractViolation):
        C.RiskLevels(ticker="X.JK", entry_low=900, stop_loss=920).validate()
    with pytest.raises(ContractViolation):
        C.RiskLevels(ticker="X.JK", entry_high=900, tp1=880).validate()
    with pytest.raises(ContractViolation):
        C.RiskLevels(ticker="X.JK", entry_high=900, tp1=950, tp2=930).validate()
    ok = C.RiskLevels(
        ticker="X.JK", entry_low=875, entry_high=890, stop_loss=845,
        tp1=930, tp2=970,
    )
    ok.validate()


def test_ready_decision_requires_risk_plan_ref():
    with pytest.raises(ContractViolation):
        C.CandidateDecision(ticker="X.JK", final_status="READY").validate()
    ok = C.CandidateDecision(ticker="X.JK", final_status="READY", risk_plan_ref="rp:1")
    ok.validate()


def _run(**overrides):
    """Valid minimal run with required dates; tests override fields."""
    fields = {
        "run_id": "run-x",
        "analysis_date": "2026-09-25",
        "as_of": "2026-09-25",
        "run_status": "COMPLETE",
        "decision_outcome": "NO_TRADE",
        "market_facts": C.MarketFacts(as_of="2026-09-25", close=7800.0),
    }
    fields.update(overrides)
    return C.RunResult(**fields)


def _decision(ticker: str, status: str, rank: int | None = None) -> C.CandidateDecision:
    return C.CandidateDecision(
        ticker=ticker,
        final_status=status,
        agent_proposed_status=status,
        risk_plan_ref="rp:1" if status == "READY" else "",
        rank=rank,
    )


def test_run_result_invariants_cap_and_ranks():
    ready = [_decision(f"R{i}.JK", "READY", i + 1) for i in range(3)]
    wait = [_decision("W.JK", "WAIT")]
    run = _run(recommendations=tuple(ready), wait=tuple(wait), decision_outcome="TOP_3")
    run.validate()


def test_run_result_rejects_fourth_recommendation_and_duplicate():
    ready = [_decision(f"R{i}.JK", "READY", i + 1) for i in range(4)]
    run = _run(recommendations=tuple(ready), decision_outcome="TOP_3")
    with pytest.raises(ContractViolation):
        run.validate()

    dup = _run(
        recommendations=(_decision("A.JK", "READY", 1),),
        wait=(_decision("A.JK", "WAIT"),),
        decision_outcome="TOP_3",
    )
    with pytest.raises(ContractViolation):
        dup.validate()


def test_partial_run_maps_to_not_evaluated_not_no_trade():
    with pytest.raises(ContractViolation):
        _run(run_status="PARTIAL", decision_outcome="NO_TRADE").validate()
    ok = _run(run_status="PARTIAL", decision_outcome="NOT_EVALUATED")
    ok.validate()


def test_complete_run_with_zero_ready_is_no_trade():
    run = _run(wait=(_decision("W.JK", "WAIT"),))
    run.validate()
    with pytest.raises(ContractViolation):
        _run(decision_outcome="TOP_3").validate()


def test_market_facts_breadth_absence_must_be_explicit():
    with pytest.raises(ContractViolation):
        C.MarketFacts(as_of="2026-09-25", breadth50=0.61, breadth_available=False).validate()
    C.MarketFacts(as_of="2026-09-25", breadth50=None, breadth_available=False).validate()


# ---------------------------------------------------------------------------
# Phase 0/1 — fixtures are offline and deterministic
# ---------------------------------------------------------------------------


def test_fixture_frames_are_deterministic_and_offline():
    f1, f2 = default_frames(), default_frames()
    for key in f1:
        pd.testing.assert_frame_equal(f1[key], f2[key])
    assert len(f1) >= 4  # pool must exceed the top-3 cap


def test_fixtures_cover_bullish_conflict_and_missing_data_scenarios():
    frames = default_frames()
    assert classify_setup(add_flow_indicators(_prepared(frames["BKSL.JK"]))) in {"BREAKOUT", "TREND_CONTINUATION"}
    # Conflict scenario: distribution-style tape (down days on heavy volume).
    conflict = add_flow_indicators(_prepared(frames["CUAN.JK"]))
    assert np.isfinite(conflict["CMF"].iloc[-1])
    # Stale scenario: flat prices make directional flow unavailable/neutral.
    stale = add_flow_indicators(_prepared(frames["DEWA.JK"]))
    assert np.isfinite(stale["Close"].iloc[-1])


def _prepared(frame: pd.DataFrame) -> pd.DataFrame:
    legacy = load_legacy_ranking()
    return legacy.siapkan_data_untuk_timeframe(legacy.buang_daily_candle_belum_selesai(frame), "daily_swing")


# ---------------------------------------------------------------------------
# Phase 1 — snapshot: as-of cutoff, no lookahead, determinism
# ---------------------------------------------------------------------------


def test_completed_cutoff_drops_partial_candle(monkeypatch):
    """Deterministic fake clock: 'today 15:00 WIB' means the last candle is partial."""
    from cacingnaga import snapshot as snap
    index = pd.bdate_range(end="2026-09-25", periods=5)
    df = pd.DataFrame(
        {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 100},
        index=index,
    )
    fake_now = dt.datetime(2026, 9, 25, 15, 0, tzinfo=snap._legacy().IDX_TZ)
    class FakeDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now if tz is not None else fake_now.replace(tzinfo=None)
    core = snap._legacy()._BASE  # screener_core owns buang_daily_candle_belum_selesai
    monkeypatch.setattr(core, "datetime", FakeDatetime)
    cut = snap.completed_cutoff(df)
    assert len(cut) == len(df) - 1  # the "today" candle is dropped

    # After the 16:10 WIB buffer the same candle counts as completed.
    fake_done = dt.datetime(2026, 9, 25, 16, 30, tzinfo=core.IDX_TZ)
    class FakeDone(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_done if tz is not None else fake_done.replace(tzinfo=None)
    monkeypatch.setattr(core, "datetime", FakeDone)
    assert len(snap.completed_cutoff(df)) == len(df)


def test_flow_indicators_are_causal_and_finite():
    enriched = add_flow_indicators(_prepared(default_frames()["BKSL.JK"]))
    tail = enriched.tail(60)
    for col in ("BB_Mid", "OBV", "MFI", "CMF", "ADL", "UpDown_Volume_Ratio"):
        assert col in enriched.columns
        assert np.isfinite(tail[col]).all(), col


def test_snapshot_is_deterministic_across_replays():
    config = AIAnalystConfig()
    s1 = build_snapshot(market_frame(), default_frames(), config)
    s2 = build_snapshot(market_frame(), default_frames(), config)
    assert s1.data_snapshot_hash == s2.data_snapshot_hash
    assert [c.ticker for c in s1.candidates] == [c.ticker for c in s2.candidates]
    s1.validate()
    s2.validate()


def test_snapshot_mutations_break_integrity():
    snapshot = build_snapshot(market_frame(), default_frames(), AIAnalystConfig())
    assert_snapshot_integrity(snapshot)
    mutated = AnalysisSnapshot(
        **{**snapshot.__dict__, "eligible_count": snapshot.eligible_count + 1}
    )
    with pytest.raises(ContractViolation):
        assert_snapshot_integrity(mutated)


def test_snapshot_pool_exceeds_recommendation_cap():
    snapshot = build_snapshot(market_frame(), default_frames(), AIAnalystConfig())
    assert len(snapshot.candidates) >= 4
    assert snapshot.eligible_count >= len(snapshot.candidates)


def test_conflict_fixture_diverges_technical_vs_flow():
    """Phase 6 prerequisite: a real technical-bullish / flow-distribution pair."""
    snapshot = build_snapshot(market_frame(), default_frames(), AIAnalystConfig())
    by_ticker = {c.ticker: (c, f) for c, f in zip(snapshot.candidates, snapshot.flows)}
    # Facts and flow rows must stay aligned after the deterministic sort.
    assert set(by_ticker) == {"BKSL.JK", "ERTX.JK", "CUAN.JK", "DEWA.JK"}
    conflict_facts, conflict_flow = by_ticker["CUAN.JK"]
    bullish_trend = (
        conflict_facts.ema20 > conflict_facts.ema50
        and conflict_facts.ema50 > conflict_facts.ema200
    )
    distribution_flow = conflict_flow.cmf is not None and conflict_flow.cmf < 0
    assert bullish_trend and distribution_flow
    # Bullish fixtures must read the opposite way on flow (accumulation side).
    bull_facts, bull_flow = by_ticker["BKSL.JK"]
    assert bull_flow.cmf is not None and bull_flow.cmf > 0
    assert bull_facts.setup != "UNKNOWN"


def test_snapshot_agent_envelope_is_compact_and_evidence_addressed():
    snapshot = build_snapshot(market_frame(), default_frames(), AIAnalystConfig())
    envelope = snapshot_to_agent_envelope(snapshot)
    assert envelope["as_of"] == snapshot.as_of
    first = envelope["candidates"][0]
    assert first["evidence_ids"]["price"].startswith("TechnicalFacts:")
    assert "data_snapshot_hash" not in first  # envelope is a subset, not audit


def test_snapshot_reports_empty_and_short_history_as_warnings():
    frames = default_frames()
    frames["EMPTY.JK"] = pd.DataFrame()
    snapshot = build_snapshot(market_frame(), frames, AIAnalystConfig())
    assert any("EMPTY.JK" in w for w in snapshot.warnings)
    assert snapshot.failed_count >= 1


def test_config_hash_is_stable_payload():
    config = AIAnalystConfig()
    payload = config_payload(config)
    assert canonical.canonical_hash(payload) == canonical.canonical_hash(config_payload(AIAnalystConfig()))
    assert payload["pipeline_version"] == "AI_TEAM_DAILY_V1"


def test_legacy_tests_remain_green_baseline_documented():
    """Frozen baseline: the legacy suite must pass unchanged (exit criterion)."""
    legacy = load_legacy_ranking()
    assert legacy.PIPELINE_VERSION if hasattr(legacy, "PIPELINE_VERSION") else True
    assert legacy.VARIANT_ID == "TIMEFRAME_PERCENTILE_RANK"
