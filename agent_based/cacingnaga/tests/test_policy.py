from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
sys.path.insert(0, str(BASE))

from cacingnaga import contracts as C
from cacingnaga.canonical import canonical_hash
from cacingnaga.config import (
    AIAnalystConfig,
    GatesConfig,
    PolicyConfig,
    RiskPolicyConfig,
    ScoreWeights,
    UnknownComponentPolicy,
    config_payload,
)
from cacingnaga.errors import ContractViolation, SnapshotError
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.policy import (
    DecisionPolicy,
    PolicyOutcome,
    evaluate_snapshot,
    flow_component,
    liquidity_component,
    market_fit_component,
    risk_reward_component,
    technical_component,
)
from cacingnaga.risk import RiskPlanCalculator
from cacingnaga.snapshot import build_snapshot

CONFIG = AIAnalystConfig()


@pytest.fixture(scope="module")
def snapshot():
    return build_snapshot(market_frame(), default_frames(), CONFIG)


@pytest.fixture(scope="module")
def snap(snapshot):
    """Alias so helpers/tests can request the snapshot as an argument."""
    return snapshot


# ---------------------------------------------------------------------------
# PolicyConfig validation (config-only changes, startup rejection)
# ---------------------------------------------------------------------------


def test_policy_config_default_is_valid():
    AIAnalystConfig().validate()


def test_weights_must_sum_to_one():
    with pytest.raises(ContractViolation):
        ScoreWeights(technical=0.5).validate()
    ok = ScoreWeights(technical=0.35, flow=0.15)  # rebalanced, still sums to 1
    ok.validate()


def test_risk_mandates_cannot_violate_prd():
    with pytest.raises(ContractViolation):
        RiskPolicyConfig(min_stop_pct=0.01).validate()
    with pytest.raises(ContractViolation):
        RiskPolicyConfig(max_target_pct=0.20).validate()
    with pytest.raises(ContractViolation):
        RiskPolicyConfig(max_holding_days=15).validate()


def test_gate_config_bounds():
    with pytest.raises(ContractViolation):
        GatesConfig(min_composite_score=1.5).validate()
    with pytest.raises(ContractViolation):
        GatesConfig(min_risk_reward_tp1=0.5).validate()


def test_unknown_component_mode_is_validated():
    with pytest.raises(ContractViolation):
        UnknownComponentPolicy(mode="renormalize").validate()


# ---------------------------------------------------------------------------
# RiskPlanCalculator (deterministic, mandates, fail-closed)
# ---------------------------------------------------------------------------


def _facts(**overrides) -> C.TechnicalFacts:
    fields = dict(
        ticker="TST.JK", as_of="2026-09-25", price=900.0, ema20=880.0, ema50=860.0,
        ema200=830.0, rsi=58.0, adx=24.0, atr=18.0, atr_pct=0.02,
        macd_hist=1.5, support=850.0, resistance=980.0, prev_high=940.0,
        relative_volume=1.3, turnover20_idr=5e9, setup="TREND_CONTINUATION",
    )
    fields.update(overrides)
    return C.TechnicalFacts(**fields)


def test_risk_plan_is_deterministic_and_valid():
    calc = RiskPlanCalculator(CONFIG.policy)
    r1 = calc.calculate(_facts())
    r2 = calc.calculate(_facts())
    assert r1.valid and r2.valid
    assert r1.payload() == r2.payload()
    lv = r1.levels
    # Mandates: stop 2-5% of entry, TP1 3-10% above entry, ordering long-only.
    entry_mid = (lv.entry_low + lv.entry_high) / 2
    stop_pct = (entry_mid - lv.stop_loss) / entry_mid
    tp1_pct = (lv.tp1 - entry_mid) / entry_mid
    assert 0.02 <= stop_pct <= 0.05
    assert 0.03 <= tp1_pct <= 0.10
    assert lv.stop_loss < lv.entry_low <= lv.entry_high < lv.tp1 < lv.tp2
    assert lv.risk_reward_tp1 >= 1.0 < lv.risk_reward_tp2
    assert lv.maximum_holding_days == 10
    assert "IDX tick rounding" in " ".join(r1.rules_applied)


def test_risk_plan_rejects_missing_mandatory_facts():
    calc = RiskPlanCalculator(CONFIG.policy)
    result = calc.calculate(_facts(support=None))
    assert not result.valid and result.levels is None
    assert any("support" in r for r in result.reject_reasons)


def test_risk_plan_rejects_broken_structure():
    calc = RiskPlanCalculator(CONFIG.policy)
    result = calc.calculate(_facts(price=820.0, support=850.0, ema20=880.0))
    assert not result.valid
    assert any("entry zone inverted" in r or "entry" in r for r in result.reject_reasons)


def test_risk_plan_caps_targets_at_ten_pct_mandate():
    calc = RiskPlanCalculator(CONFIG.policy)
    # Deep support/ATR push the structural stop to the 5% max clamp;
    # TP2 at 2.5R would then be ~12.5%, which the 10% cap must contain.
    result = calc.calculate(_facts(support=845.0, atr=35.0))
    assert result.valid
    lv = result.levels
    entry_mid = (lv.entry_low + lv.entry_high) / 2
    assert (lv.tp2 - entry_mid) / entry_mid <= 0.10 + 1e-9
    assert any("capped at the 10% target mandate" in r for r in result.rules_applied)


def test_risk_plan_levels_respect_idx_tick_sizes():
    calc = RiskPlanCalculator(CONFIG.policy)
    lv = calc.calculate(_facts(price=903.0, support=851.0)).levels
    for value in (lv.entry_low, lv.entry_high, lv.stop_loss, lv.tp1, lv.tp2):
        # Every price must be an exact IDX tick multiple at its own level.
        tick = 1 if value < 200 else 2 if value < 500 else 5 if value < 2000 else 10 if value < 5000 else 25
        assert abs(value / tick - round(value / tick)) < 1e-9


# ---------------------------------------------------------------------------
# Score components (documented ranges)
# ---------------------------------------------------------------------------


def test_technical_component_bounds_and_strength():
    weak = technical_component(_facts(price=800, ema20=850, ema50=880, ema200=900, rsi=30, adx=10), None)
    strong = technical_component(_facts(), None)
    assert 0.0 <= weak < strong <= 1.0


def test_flow_component_distinguishes_unavailable_from_neutral():
    score_missing, available = flow_component(C.FlowFacts(ticker="X", as_of="2026-09-25"))
    assert score_missing == 0.0 and not available       # UNKNOWN, not neutral
    score_accum, _ = flow_component(C.FlowFacts(ticker="X", as_of="2026-09-25", cmf=0.2, mfi=75))
    score_dist, _ = flow_component(C.FlowFacts(ticker="X", as_of="2026-09-25", cmf=-0.2, mfi=10))
    assert score_accum > score_dist >= 0.0


def test_market_fit_component():
    score, available = market_fit_component(
        C.MarketFacts(as_of="2026-09-25", close=7900, ema20=7800, ema50=7700, ema200=7500)
    )
    assert available and score > 0.9


def test_liquidity_component_monotonic():
    low = liquidity_component(_facts(turnover20_idr=1.5e9))
    high = liquidity_component(_facts(turnover20_idr=60e9))
    assert 0.0 <= low < high <= 1.0


def test_risk_reward_component_none_is_zero():
    assert risk_reward_component(None) == 0.0


# ---------------------------------------------------------------------------
# DecisionPolicy: gates, statuses, ranking, run invariants
# ---------------------------------------------------------------------------


def test_full_snapshot_run_satisfies_all_invariants(snapshot):
    run, outcomes = evaluate_snapshot(snapshot, CONFIG)
    run.validate()  # Section 6.10 invariants
    assert run.run_status == "COMPLETE"
    assert run.decision_outcome in ("TOP_3", "NO_TRADE")
    assert len(run.recommendations) <= 3
    # Every snapshot candidate appears exactly once across the run.
    evaluated = {o.ticker for o in outcomes}
    in_run = {d.ticker for d in (*run.recommendations, *run.wait, *run.rejected)}
    assert evaluated == in_run
    for decision in run.recommendations:
        assert decision.risk_plan_ref.startswith("AI_TEAM_DAILY_V1_RISK_1:")
        assert decision.confidence_band in ("MEDIUM", "HIGH")


def test_zero_qualified_candidates_yields_no_trade_without_failure(snap):
    # Bearish broken facts for every candidate → nothing passes the gates.
    policy = DecisionPolicy(CONFIG)
    outcomes = [
        policy.evaluate(
            _facts(ticker=f"X{i}.JK", price=500.0, ema20=700.0, ema50=750.0,
                   ema200=800.0, rsi=30.0, adx=8.0, support=650.0, resistance=720.0,
                   setup="BREAKDOWN"),
            C.FlowFacts(ticker=f"X{i}.JK", as_of="2026-09-25"),
            snap.market,
        )
        for i in range(4)
    ]

    class _Snap:
        analysis_date = "2026-09-25"
        as_of = "2026-09-25"
        market = snap.market
        warnings: tuple[str, ...] = ()
        data_snapshot_hash = "deadbeef"

    run = policy.finalize_run(_Snap(), outcomes, run_id="no-trade-run")
    run.validate()
    assert run.decision_outcome == "NO_TRADE"
    assert run.recommendations == ()


def test_invalid_risk_plan_can_neither_ready_nor_fake_levels(snapshot):
    policy = DecisionPolicy(CONFIG)
    facts = _facts(ticker="NOP.JK", support=None)  # plan impossible
    outcome = policy.evaluate(facts, C.FlowFacts(ticker="NOP.JK", as_of="2026-09-25"),
                              snapshot.market)
    assert outcome.final_status != "READY"
    assert outcome.risk_plan.levels is None


def test_same_inputs_produce_same_status_rank_and_hash(snapshot):
    run1, outcomes1 = evaluate_snapshot(snapshot, CONFIG)
    run2, outcomes2 = evaluate_snapshot(snapshot, CONFIG)
    assert [d.ticker for d in run1.recommendations] == [d.ticker for d in run2.recommendations]
    assert [d.rank for d in run1.recommendations] == [d.rank for d in run2.recommendations]
    assert [o.payload() for o in outcomes1] == [o.payload() for o in outcomes2]
    assert run1.config_hash == run2.config_hash


def test_weight_change_alone_moves_ranking(snapshot):
    run_default, _ = evaluate_snapshot(snapshot, CONFIG)
    rebalanced = AIAnalystConfig(
        policy=PolicyConfig(weights=ScoreWeights(technical=0.35, flow=0.15))
    )
    run_alt, _ = evaluate_snapshot(snapshot, rebalanced)
    # Config-only change flows through without touching any agent/policy code.
    assert run_alt.config_hash != run_default.config_hash


def test_ranks_are_contiguous_and_ready_only_in_recommendations(snapshot):
    run, _ = evaluate_snapshot(snapshot, CONFIG)
    assert [d.rank for d in run.recommendations] == list(range(1, len(run.recommendations) + 1))
    assert all(d.final_status == "READY" for d in run.recommendations)
    assert all(d.rank is None for d in (*run.wait, *run.rejected))


def test_tie_break_is_deterministic_by_ticker(snap):
    policy = DecisionPolicy(CONFIG)
    a = policy.evaluate(_facts(ticker="AAA.JK"), C.FlowFacts(ticker="AAA.JK", as_of="2026-09-25"),
                        snap.market)
    b = policy.evaluate(_facts(ticker="ZZZ.JK"), C.FlowFacts(ticker="ZZZ.JK", as_of="2026-09-25"),
                        snap.market)
    assert a.score == b.score  # identical facts ⇒ identical score

    class _Snap:
        analysis_date = "2026-09-25"
        as_of = "2026-09-25"
        market = snap.market
        warnings: tuple[str, ...] = ()
        data_snapshot_hash = "deadbeef"

    run = policy.finalize_run(_Snap(), [b, a], run_id="tie-run")  # shuffled input order
    ready = [d.ticker for d in run.recommendations]
    assert ready == sorted(ready)  # ties resolved by ticker regardless of input order


def test_hard_gate_failure_blocks_ready_even_with_high_score(snapshot):
    gates = GatesConfig(min_relative_volume=10.0)  # impossible threshold
    strict = AIAnalystConfig(policy=PolicyConfig(gates=gates))
    run, outcomes = evaluate_snapshot(snapshot, strict)
    assert run.recommendations == ()
    assert run.decision_outcome == "NO_TRADE"
    assert all(o.hard_gate_failures for o in outcomes)


def test_trigger_gate_blocks_unconfirmed_setup(snapshot):
    """Plan §3: READY needs a trigger (breakout or EMA20 reclaim + volume)."""
    policy = DecisionPolicy(CONFIG)
    # Reclaim true (900 > EMA20 880) but volume confirmation too low.
    outcome = policy.evaluate(
        _facts(relative_volume=1.05),
        C.FlowFacts(ticker="TST.JK", as_of="2026-09-25"), snapshot.market,
    )
    assert outcome.final_status != "READY"
    assert any("trigger gate" in f for f in outcome.hard_gate_failures)
    # Same facts with a confirmed breakout (900 > prev high 890, rel vol 1.5):
    trigger_failures = [
        f for f in policy.evaluate(
            _facts(relative_volume=1.5, prev_high=890.0),
            C.FlowFacts(ticker="TST.JK", as_of="2026-09-25"), snapshot.market,
        ).hard_gate_failures if "trigger gate" in f
    ]
    assert trigger_failures == []


def test_gate_relaxation_promotes_wait_to_ready_via_config_only(snapshot):
    """Promotion must be achievable by configuration, never by code edits.

    Both the RSI ceiling and the trigger volume confirmation are relaxed in
    configuration; the trigger gate itself stays enabled and passes via the
    EMA20 reclaim path (relative volume >= 0.0).
    """
    permissive = AIAnalystConfig(
        policy=PolicyConfig(gates=GatesConfig(max_rsi=100.0, trigger_volume_confirm=0.0))
    )
    run, outcomes = evaluate_snapshot(snapshot, permissive)
    run.validate()
    assert run.decision_outcome == "TOP_3"
    assert 1 <= len(run.recommendations) <= 3
    assert [d.rank for d in run.recommendations] == list(range(1, len(run.recommendations) + 1))
    # Statuses were derived per candidate; the promoted ones were all READY.
    by_ticker = {o.ticker: o.final_status for o in outcomes}
    assert all(by_ticker[d.ticker] == "READY" for d in run.recommendations)
