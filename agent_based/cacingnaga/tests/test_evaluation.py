"""Phase 7 — backtest integration and forward evaluation tests (plan §7 Phase 7).

Exit criteria under test:
- a completed run can be reconstructed from storage without rerunning an LLM
  (this pipeline is deterministic; the evaluation report is byte-reproducible),
- READY/WAIT/REJECT outcomes are computed from documented, testable rules,
- metrics and ablations reproduce from stored recommendations over aligned
  dates/costs,
- the evaluation basket's drawdown assumptions are explicit and introduce no
  production portfolio management,
- no AI improvement is claimed unless the frozen sample/horizon/promotion
  criterion is met.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd
import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
sys.path.insert(0, str(BASE))

from cacingnaga.backtest import (
    BacktestConfig,
    TradeRecord,
    _Fills,
    _simulate_counterfactual,
    _simulate_ready,
)
from cacingnaga.canonical import canonical_hash, canonical_json
from cacingnaga.config import AIAnalystConfig
from cacingnaga.errors import ContractViolation
from cacingnaga.evaluation import (
    ABLATION_VARIANTS,
    EvalCriteria,
    SignalRecord,
    ablation_config,
    ablation_weights,
    buy_and_hold_baseline,
    confidence_band_stability,
    counterfactual_trigger_return,
    equal_weight_basket_drawdown,
    ihsg_baseline,
    losses_avoided_by_reject,
    overtrading_vs_screener,
    promotion_decision,
    ready_trades,
    reject_diagnostics,
    render_evaluation_report,
    run_ablation,
    run_ablation_suite,
    run_evaluation,
    seeded_random_baseline,
    signal_records_from_run,
    status_rates,
    wait_confirmation_rate,
    wait_counterfactuals,
)
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.policy import evaluate_snapshot
from cacingnaga.snapshot import build_snapshot

CONFIG = AIAnalystConfig()
BT = BacktestConfig()


def _flat_frame(price=900.0, days=15, volume=1_000_000.0):
    index = pd.bdate_range(end="2026-09-25", periods=days)
    return pd.DataFrame(
        {"Open": [price] * days, "High": [price * 1.005] * days,
         "Low": [price * 0.995] * days, "Close": [price] * days,
         "Volume": [volume] * days},
        index=index,
    )


class _L:
    entry_low, entry_high = 895.0, 905.0
    stop_loss = 870.0
    tp1, tp2 = 945.0, 990.0


# ---------------------------------------------------------------------------
# Item 1: accepted signal status is configurable, REJECT never
# ---------------------------------------------------------------------------


def test_accepted_status_is_configurable_and_reject_is_illegal():
    assert BacktestConfig().accepted_status == "READY"
    BacktestConfig(accepted_status="WAIT").validate()
    with pytest.raises(ContractViolation):
        BacktestConfig(accepted_status="REJECT").validate()


def test_accepted_wait_creates_trades_labeled_wait():
    stock = _flat_frame(days=12)
    fills = _Fills(BT)
    record = _simulate_ready(
        "W.JK", stock.index[0], stock, stock, _L(), fills, BT,
        status="WAIT", confidence_band="MEDIUM",
    )
    assert record.status == "WAIT"
    assert record.outcome == "EXECUTED_TIME_STOP"
    assert record.confidence_band == "MEDIUM"
    assert record.actual_result in ("WIN", "LOSS", "FLAT")


# ---------------------------------------------------------------------------
# Items 5–6: status-bucket outcome rules stay distinct
# ---------------------------------------------------------------------------


def _outcome(ticker, status):
    class O:
        pass

    o = O()
    o.ticker = ticker
    o.final_status = status
    o.score = 0.7
    return o


def test_status_buckets_are_mutually_exclusive():
    outcomes = [_outcome("A.JK", "READY"), _outcome("B.JK", "WAIT"),
                _outcome("C.JK", "REJECT")]
    assert [o.ticker for o in ready_trades(outcomes)] == ["A.JK"]
    assert [o.ticker for o in wait_counterfactuals(outcomes)] == ["B.JK"]
    assert [o.ticker for o in reject_diagnostics(outcomes)] == ["C.JK"]


def test_wait_counterfactual_fills_at_reference_and_stays_labeled_not_taken():
    stock = _flat_frame(days=15)
    record = _simulate_counterfactual(
        "W.JK", stock.index[0], stock, stock, "WAIT", _Fills(BT), BT,
        entry_ref=898.0,
    )
    assert record.outcome == "NOT_TAKEN"          # NOT an executed outcome
    assert record.entry_price is not None          # trigger-fill counterfactual
    assert not record.outcome.startswith("EXECUTED")


def test_counterfactual_trigger_return_is_labeled_and_nan_safe():
    stock = _flat_frame(days=15)
    value = counterfactual_trigger_return(
        stock, stock.index[0], 898.0, evaluation_days=10, fills=_Fills(BT)
    )
    assert math.isfinite(value)
    unknown = counterfactual_trigger_return(
        stock, stock.index[0], 0.0, evaluation_days=10, fills=_Fills(BT)
    )
    assert math.isnan(unknown)                     # unknown, not zero


def test_unfilled_entry_is_distinct_from_reject_and_executed():
    stock = _flat_frame(price=1200.0, days=10)     # never enters the zone
    record = _simulate_ready("U.JK", stock.index[0], stock, stock, _L(), _Fills(BT), BT)
    assert record.outcome == "NOT_EXECUTED"
    assert record.outcome != "DIAGNOSTIC_REJECT"
    assert not record.outcome.startswith("EXECUTED")
    assert record.actual_result is None


# ---------------------------------------------------------------------------
# Item 2: provenance-complete signal records
# ---------------------------------------------------------------------------


def test_signal_records_carry_full_provenance_and_round_trip():
    snapshot = build_snapshot(market_frame(), default_frames(), CONFIG,
                              as_of="2026-08-20")
    run, outcomes = evaluate_snapshot(snapshot, CONFIG)
    records = signal_records_from_run(
        run, outcomes, run_id="run-sig", config=CONFIG, config_hash="cfg-hash",
        agents_consulted=("MarketAgent", "TechnicalAgent", "FlowAgent"),
        prompt_hash="p-hash", model_ref="offline-deterministic",
    )
    assert records
    for record in records:
        assert record.policy_ref == "AI_TEAM_DAILY_V1_POLICY_1"
        assert record.config_hash == "cfg-hash"
        assert record.prompt_hash == "p-hash"
        assert "MarketAgent" in record.agent_refs
        payload = record.payload()
        rebuilt = canonical_json(payload)          # must be JSON-safe
        assert "SIGNAL_RECORD_1" in rebuilt
    ready = [r for r in records if r.status == "READY"]
    for record in ready:
        assert record.components                   # policy components attached


# ---------------------------------------------------------------------------
# Item 8: PRD metrics
# ---------------------------------------------------------------------------


def test_status_rates_from_replayed_runs():
    runs = [
        {"ready_count": 1, "wait_count": 2, "reject_count": 1},
        {"ready_count": 0, "wait_count": 1, "reject_count": 2},
    ]
    rates = status_rates(runs)
    assert rates["ready"] == 1 and rates["wait"] == 3 and rates["reject"] == 3
    assert rates["ready_rate"] == pytest.approx(1 / 7)
    assert rates["ready_rate"] + rates["wait_rate"] + rates["reject_rate"] == pytest.approx(1)


def _trade(net, outcome="EXECUTED_TIME_STOP", status="READY", band="MEDIUM",
           exit_date="2026-09-01", ticker="X.JK"):
    record = TradeRecord(
        screen_date="2026-08-20", ticker=ticker, status=status,
        outcome=outcome, exit_date=exit_date, confidence_band=band,
    )
    record.net_return_pct = net
    return record


def test_wait_confirmation_rate_counts_positive_counterfactuals():
    trades = [
        _trade(0.0, outcome="NOT_TAKEN", status="WAIT"),
        _trade(5.0, outcome="NOT_TAKEN", status="WAIT"),
        _trade(-1.0, outcome="NOT_TAKEN", status="WAIT"),
        _trade(2.0, outcome="EXECUTED_TIME_STOP"),          # not a WAIT CF
    ]
    stats = wait_confirmation_rate(trades)
    assert stats["n"] == 3
    assert stats["confirmation_rate"] == pytest.approx(1 / 3)


def test_losses_avoided_by_reject_sum_negative_diagnostics():
    trades = [
        _trade(-3.0, outcome="DIAGNOSTIC_REJECT", status="REJECT"),
        _trade(4.0, outcome="DIAGNOSTIC_REJECT", status="REJECT"),
        _trade(-2.0, outcome="DIAGNOSTIC_REJECT", status="REJECT", ticker="Y.JK"),
    ]
    stats = losses_avoided_by_reject(trades)
    assert stats["n"] == 3
    assert stats["losses_avoided_pct"] == pytest.approx(5.0)


def test_confidence_band_stability_groups_by_band():
    trades = [
        _trade(5.0, band="HIGH"), _trade(-1.0, band="HIGH"),
        _trade(1.0, band="LOW"),
    ]
    stats = confidence_band_stability(trades)
    assert stats["HIGH"]["n"] == 2
    assert stats["HIGH"]["win_rate"] == pytest.approx(0.5)
    assert stats["LOW"]["win_rate"] == 1.0


def test_overtrading_ratio_against_screener():
    stats = overtrading_vs_screener(
        [{"ready_count": 2}], {"2026-08-20": ["A.JK", "B.JK"]}
    )
    assert stats["ai_signals"] == 2 and stats["screener_signals"] == 2
    assert stats["ratio"] == 1.0


# ---------------------------------------------------------------------------
# Item 10: equal-weight basket drawdown (evaluation construct only)
# ---------------------------------------------------------------------------


def test_basket_drawdown_known_sequence():
    trades = [
        _trade(10.0, exit_date="2026-09-01"),
        _trade(-20.0, exit_date="2026-09-02"),
        _trade(5.0, exit_date="2026-09-03"),
    ]
    stats = equal_weight_basket_drawdown(trades)
    assert stats["n"] == 3
    assert stats["max_drawdown_pct"] == pytest.approx(-20.0)
    assert "not production" in stats["construction"]


def test_basket_drawdown_ignores_non_executed():
    trades = [_trade(9.0, outcome="NOT_TAKEN", status="WAIT")]
    stats = equal_weight_basket_drawdown(trades)
    assert stats["n"] == 0 and stats["max_drawdown_pct"] is None


# ---------------------------------------------------------------------------
# Item 9: date-matched ablations over the same pool
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def screen_dates():
    index = pd.to_datetime(market_frame().index)
    return [d.date().isoformat() for d in index[260:268:2]]


def test_ablation_weights_sum_to_one_and_unknown_raises():
    for variant in ABLATION_VARIANTS:
        weights = ablation_weights(variant)
        assert sum(weights.values()) == pytest.approx(1.0)
    with pytest.raises(ContractViolation):
        ablation_weights("super_ai")


def test_ablation_arms_are_date_matched_and_deterministic(screen_dates):
    suite = run_ablation_suite(market_frame(), default_frames(), CONFIG, BT, screen_dates)
    assert suite["variants"] == ABLATION_VARIANTS
    assert len(suite["arms"]) == 5
    for arm in suite["arms"]:
        assert arm["screen_dates"] == screen_dates      # same dates, same pool
    again = run_ablation_suite(market_frame(), default_frames(), CONFIG, BT, screen_dates)
    assert canonical_hash(suite) == canonical_hash(again)


def test_single_ablation_arm_matches_suite_arm(screen_dates):
    arm = run_ablation(market_frame(), default_frames(), CONFIG, BT,
                       screen_dates, variant="technical_flow")
    suite = run_ablation_suite(market_frame(), default_frames(), CONFIG, BT, screen_dates)
    matched = next(a for a in suite["arms"] if a["variant"] == "technical_flow")
    assert canonical_hash(arm) == canonical_hash(matched)


def test_ablation_config_changes_only_weights():
    base = AIAnalystConfig()
    variant_cfg = ablation_config(base, "technical_only")
    assert variant_cfg.policy.weights.technical == 1.0
    assert variant_cfg.policy.gates == base.policy.gates     # gates unchanged
    assert variant_cfg.screener == base.screener


# ---------------------------------------------------------------------------
# Item 11: deterministic baselines
# ---------------------------------------------------------------------------


def test_ihsg_and_buy_hold_baselines_deterministic(screen_dates):
    ihsg = market_frame()
    first = ihsg_baseline(ihsg, screen_dates, horizon=10)
    second = ihsg_baseline(ihsg, screen_dates, horizon=10)
    assert first == second and first["n"] == len(screen_dates)
    bah = buy_and_hold_baseline(ihsg, screen_dates=screen_dates)
    assert bah["fee_free"] is True and bah["average_return_pct"] is not None


def test_seeded_random_baseline_is_seed_deterministic():
    pool = {"2026-08-20": ["B.JK", "A.JK", "C.JK", "D.JK"]}
    first = seeded_random_baseline(pool, seed=7)
    second = seeded_random_baseline(pool, seed=7)
    other = seeded_random_baseline(pool, seed=8)
    assert first["picks"] == second["picks"]
    assert first["picks"] != other["picks"] or first["seed"] == other["seed"]


def test_current_screener_baseline_runs_frozen_legacy_point_in_time(screen_dates):
    from cacingnaga.evaluation import current_screener_baseline

    baseline = current_screener_baseline(
        market_frame(), default_frames(), screen_dates
    )
    assert baseline["name"] == "current_screener"
    assert set(baseline["picks"]) == set(screen_dates)
    assert baseline["status_labels"]["ready"] == "Ready to Enter"
    for picks in baseline["picks"].values():
        assert len(picks) <= 3


# ---------------------------------------------------------------------------
# Item 12: frozen criteria + promotion gate
# ---------------------------------------------------------------------------


def test_criteria_must_be_frozen_before_promotion():
    criteria = EvalCriteria()
    with pytest.raises(ContractViolation):
        criteria.assert_frozen()
    frozen = EvalCriteria(frozen=True)
    frozen.assert_frozen()
    # A missing full_decision arm is a structural error…
    with pytest.raises(ContractViolation):
        promotion_decision(frozen, {}, baseline_metric=1.0)
    # …while an arm without data is reported, not raised.
    empty = promotion_decision(frozen, {"full_decision": {}}, baseline_metric=1.0)
    assert empty["promoted"] is False and "insufficient data" in empty["reason"]


def test_promotion_requires_sample_and_edge():
    frozen = EvalCriteria(frozen=True, promotion_min_samples=30, promotion_min_edge_pct=0.5)
    small = promotion_decision(
        frozen, {"full_decision": {"executed_trades": 5, "average_net_return_pct": 9.0}},
        baseline_metric=1.0,
    )
    assert small["promoted"] is False and "below frozen minimum" in small["reason"]
    weak_edge = promotion_decision(
        frozen,
        {"full_decision": {"executed_trades": 50, "average_net_return_pct": 1.2}},
        baseline_metric=1.0,
    )
    assert weak_edge["promoted"] is False and "below frozen minimum" in weak_edge["reason"]
    strong = promotion_decision(
        frozen,
        {"full_decision": {"executed_trades": 50, "average_net_return_pct": 2.0}},
        baseline_metric=1.0,
    )
    assert strong["promoted"] is True


def test_promotion_never_promotes_on_unknown_metrics():
    frozen = EvalCriteria(frozen=True)
    result = promotion_decision(
        frozen, {"full_decision": {"executed_trades": 100, "average_net_return_pct": None}},
        baseline_metric=None,
    )
    assert result["promoted"] is False and "insufficient data" in result["reason"]


# ---------------------------------------------------------------------------
# Item 14: reproducible run_evaluation + machine-readable report
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def evaluation():
    return run_evaluation(
        market_frame(), default_frames(), CONFIG, BT, screen_dates=None,
        criteria=EvalCriteria(frozen=True),
    )


def test_evaluation_report_is_reproducible_byte_identical(evaluation):
    again = run_evaluation(
        market_frame(), default_frames(), CONFIG, BT, screen_dates=None,
        criteria=EvalCriteria(frozen=True),
    )
    assert canonical_hash(evaluation) == canonical_hash(again)


def test_evaluation_report_shape_and_disclaimer(evaluation):
    assert evaluation["schema"] == "EVALUATION_REPORT_1"
    assert evaluation["pipeline_version"] == "AI_TEAM_DAILY_V1"
    assert evaluation["criteria"]["frozen"] is True
    assert "do not guarantee future performance" in evaluation["disclaimer"]
    assert evaluation["promotion"]["promoted"] is False     # synthetic data
    assert evaluation["promotion"]["reason"]
    # All five ablation arms + every baseline present.
    assert len(evaluation["ablations"]["arms"]) == 5
    for name in ("ihsg", "buy_and_hold", "seeded_random", "current_screener",
                 "deterministic_policy"):
        assert name in evaluation["baselines"]


def test_evaluation_markdown_renders_deterministically(evaluation):
    markdown = render_evaluation_report(evaluation)
    assert markdown == render_evaluation_report(evaluation)
    assert "NOT PROMOTED" in markdown
    assert "Evaluation Report" in markdown


def test_evaluation_accepted_status_flows_through(evaluation):
    assert evaluation["backtest"]["accepted_status"] == "READY"
    wait_eval = run_evaluation(
        market_frame(), default_frames(), CONFIG,
        BacktestConfig(accepted_status="WAIT"), screen_dates=None,
        criteria=EvalCriteria(frozen=True),
    )
    assert wait_eval["backtest"]["accepted_status"] == "WAIT"
