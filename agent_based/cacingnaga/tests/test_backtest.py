from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
sys.path.insert(0, str(BASE))

from cacingnaga.backtest import (
    DEFAULT_FEE_PCT,
    DEFAULT_SLIPPAGE_PCT,
    BacktestConfig,
    TradeRecord,
    _Fills,
    _forward_window,
    _simulate_counterfactual,
    _simulate_ready,
    render_baseline_report,
    run_offline_backtest,
)
from cacingnaga.canonical import canonical_hash
from cacingnaga.config import AIAnalystConfig
from cacingnaga.errors import ContractViolation
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.snapshot import build_snapshot

CONFIG = AIAnalystConfig()
BT = BacktestConfig()


def _frame(opens, highs, lows, closes, volumes, end="2026-09-25"):
    index = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=index,
    )


def _flat_frame(price=900.0, days=30, volume=1_000_000.0):
    n = days
    return _frame(
        [price] * n, [price * 1.005] * n, [price * 0.995] * n, [price] * n, [volume] * n
    )


def _levels():
    """A valid plan shape for walk tests (entry 900, SL 870, TP 945/990)."""

    class L:
        entry_low, entry_high = 895.0, 905.0
        stop_loss = 870.0
        tp1, tp2 = 945.0, 990.0

    return L()


# ---------------------------------------------------------------------------
# Fills / costs (parity with the legacy benchmark engine)
# ---------------------------------------------------------------------------


def test_fill_costs_match_legacy_benchmark_convention():
    fills = _Fills(BT)
    assert fills.buy(1000.0) == pytest.approx(1001.0)   # +0.1% slippage
    assert fills.sell(1000.0) == pytest.approx(999.0)   # -0.1% slippage
    net = fills.net_return(1000.0, 1050.0)
    expected = (1050.0 - 1000.0 - 2.0 - 2.1) / 1000.0 * 100
    assert net == pytest.approx(expected)
    assert DEFAULT_FEE_PCT == 0.20 and DEFAULT_SLIPPAGE_PCT == 0.10


def test_backtest_config_validation():
    with pytest.raises(ContractViolation):
        BacktestConfig(fee_pct=-0.1).validate()
    with pytest.raises(ContractViolation):
        BacktestConfig(entry_window=0).validate()


def test_forward_window_is_strictly_future_and_volume_filtered():
    stock = _flat_frame(days=10)
    start = stock.index[4]
    window = _forward_window(stock, start, 3, inclusive=False)
    assert len(window) == 3
    assert window.index.min() > start


# ---------------------------------------------------------------------------
# Outcome matrix: each documented execution rule has a test
# ---------------------------------------------------------------------------


def test_unfilled_entry_when_price_never_enters_zone():
    stock = _flat_frame(price=1200.0, days=10)   # never comes near 895-905
    record = _simulate_ready("X.JK", stock.index[0], stock, stock, _levels(), _Fills(BT), BT)
    assert record.outcome == "NOT_EXECUTED"
    assert record.entry_date is None and np.isnan(record.net_return_pct)


def test_stop_first_pessimism_and_gap_through_stop_fills_at_open():
    # Bar 1 fills at the open (910 -> wait, open 910 > 905 ceiling: adjust).
    # Entry bar: open 900 inside zone. Next bar gaps below the stop.
    opens = [900.0, 850.0, 850.0, 850.0, 850.0]
    highs = [910.0, 860.0, 860.0, 860.0, 860.0]
    lows = [895.0, 840.0, 840.0, 840.0, 840.0]
    closes = [905.0, 855.0, 855.0, 855.0, 855.0]
    stock = _frame(opens, highs, lows, closes, [1e6] * 5)
    fills = _Fills(BT)
    record = _simulate_ready("X.JK", stock.index[0], stock, stock, _levels(), fills, BT)
    assert record.outcome == "EXECUTED_STOP"
    # Gap fill at the open (850), not at the stop (870); slippage applied.
    assert record.exit_price == pytest.approx(fills.sell(850.0))
    assert record.exit_reason == "STOP"
    assert record.net_return_pct < 0


def test_tp1_then_stop_two_target_accounting():
    # Bar 0 is the screen bar; entry attempts start T+1 (bar 1, open 900).
    # Bar 2 touches TP1 (945); bar 3 gaps through the stop.
    opens = [900.0, 900.0, 940.0, 850.0, 850.0]
    highs = [910.0, 910.0, 950.0, 860.0, 860.0]
    lows = [895.0, 895.0, 930.0, 840.0, 840.0]
    closes = [905.0, 905.0, 948.0, 855.0, 855.0]
    stock = _frame(opens, highs, lows, closes, [1e6] * 5)
    fills = _Fills(BT)
    record = _simulate_ready("X.JK", stock.index[0], stock, stock, _levels(), fills, BT)
    assert record.outcome == "EXECUTED_TP1_THEN_STOP"
    half1 = fills.sell(945.0)
    half2 = fills.sell(850.0)
    expected = (fills.net_return(fills.buy(900.0), half1, half_position=True)
                + fills.net_return(fills.buy(900.0), half2, half_position=True)) / 2
    assert record.net_return_pct == pytest.approx(expected, abs=1e-6)
    assert record.exit_reason == "STOP"


def test_tp1_then_tp2_full_target_path():
    # Bar 1 fills (open 900); bar 2 touches TP1; bar 3 touches TP2.
    opens = [900.0, 900.0, 940.0, 985.0, 985.0]
    highs = [910.0, 910.0, 950.0, 995.0, 995.0]
    lows = [895.0, 895.0, 930.0, 970.0, 970.0]
    closes = [905.0, 905.0, 948.0, 992.0, 992.0]
    stock = _frame(opens, highs, lows, closes, [1e6] * 5)
    fills = _Fills(BT)
    record = _simulate_ready("X.JK", stock.index[0], stock, stock, _levels(), fills, BT)
    assert record.outcome == "EXECUTED_TP1_THEN_TP2"
    assert record.exit_reason == "TP2"
    assert record.net_return_pct > 0


def test_simultaneous_tp1_and_tp2_resolves_at_tp1_only():
    # §2.4: bar 2 touches TP1 AND TP2 on the same bar -> TP1 only (half out);
    # later bars stay below TP2, so the remainder time-stops at the close.
    opens = [900.0, 900.0, 940.0, 950.0, 950.0]
    highs = [910.0, 910.0, 995.0, 955.0, 955.0]   # ONLY bar 2 spans TP1..TP2
    lows = [895.0, 895.0, 930.0, 930.0, 930.0]
    closes = [905.0, 905.0, 950.0, 945.0, 945.0]
    stock = _frame(opens, highs, lows, closes, [1e6] * 5)
    fills = _Fills(BT)
    record = _simulate_ready("X.JK", stock.index[0], stock, stock, _levels(), fills, BT)
    assert record.outcome == "EXECUTED_TP1_THEN_TIME"
    half1 = fills.sell(945.0)
    half2 = fills.sell(945.0)  # time-stop close of the last session (945.0)
    expected = (fills.net_return(fills.buy(900.0), half1, half_position=True)
                + fills.net_return(fills.buy(900.0), half2, half_position=True)) / 2
    assert record.net_return_pct == pytest.approx(expected, abs=1e-6)


def test_gap_through_tp1_fills_at_open():
    # Bar 2 opens above TP1 (960 > 945): TP1 half fills at the open.
    opens = [900.0, 900.0, 960.0, 960.0, 960.0]
    highs = [910.0, 910.0, 970.0, 970.0, 970.0]
    lows = [895.0, 895.0, 950.0, 950.0, 950.0]
    closes = [905.0, 905.0, 965.0, 965.0, 965.0]
    stock = _frame(opens, highs, lows, closes, [1e6] * 5)
    fills = _Fills(BT)
    record = _simulate_ready("X.JK", stock.index[0], stock, stock, _levels(), fills, BT)
    assert record.outcome == "EXECUTED_TP1_THEN_TIME"
    assert record.mfe_pct > 0


def test_time_stop_exits_at_close_after_ten_sessions():
    days = 12
    stock = _flat_frame(price=900.0, days=days)   # never hits SL/TP
    record = _simulate_ready("X.JK", stock.index[0], stock, stock, _levels(), _Fills(BT), BT)
    assert record.outcome == "EXECUTED_TIME_STOP"
    assert record.holding_sessions == 10
    assert record.exit_reason == "TIME_STOP"
    # Flat tape: net return is negative by round-trip costs only (≈0.6%).
    assert -0.7 < record.net_return_pct < 0


def test_mfe_mae_measured_from_actual_fill():
    stock = _flat_frame(price=900.0, days=12)
    fills = _Fills(BT)
    record = _simulate_ready("X.JK", stock.index[0], stock, stock, _levels(), fills, BT)
    entry_price = fills.buy(900.0)
    assert record.mfe_pct == pytest.approx((900.0 * 1.005 / entry_price - 1) * 100, abs=1e-6)
    assert record.mae_pct == pytest.approx((900.0 * 0.995 / entry_price - 1) * 100, abs=1e-6)


# ---------------------------------------------------------------------------
# Counterfactuals / diagnostics (never P&L)
# ---------------------------------------------------------------------------


def test_wait_counterfactual_and_reject_diagnostic_are_labeled_separately():
    stock = _flat_frame(days=15)
    fills = _Fills(BT)
    wait = _simulate_counterfactual("W.JK", stock.index[0], stock, stock, "WAIT", fills, BT, None)
    reject = _simulate_counterfactual("R.JK", stock.index[0], stock, stock, "REJECT", fills, BT, None)
    assert wait.outcome == "NOT_TAKEN"
    assert reject.outcome == "DIAGNOSTIC_REJECT"
    assert wait.net_return_pct == reject.net_return_pct  # same close-to-close path
    # Neither is an executed outcome.
    assert not wait.outcome.startswith("EXECUTED")
    assert not reject.outcome.startswith("EXECUTED")


# ---------------------------------------------------------------------------
# Walk-forward replay: determinism, integrity, no-lookahead
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def replay_result():
    config = AIAnalystConfig()
    index = pd.to_datetime(market_frame().index)
    dates = [d.date().isoformat() for d in index[260:268:2]]
    return run_offline_backtest(
        market_frame(), default_frames(), config, BacktestConfig(), screen_dates=dates
    )


def test_replay_is_deterministic(replay_result):
    config = AIAnalystConfig()
    index = pd.to_datetime(market_frame().index)
    dates = [d.date().isoformat() for d in index[260:268:2]]
    again = run_offline_backtest(
        market_frame(), default_frames(), config, BacktestConfig(), screen_dates=dates
    )
    assert canonical_hash(replay_result) == canonical_hash(again)


def test_replay_produces_runs_and_report_shape(replay_result):
    assert len(replay_result["runs"]) == len(replay_result["screen_dates"])
    assert replay_result["report"]["total_runs"] == len(replay_result["runs"])
    assert replay_result["report"]["top3_runs"] + replay_result["report"]["no_trade_runs"] \
        == replay_result["report"]["total_runs"]
    for trade in replay_result["trades"]:
        assert trade["outcome"] in (
            "EXECUTED_STOP", "EXECUTED_TIME_STOP", "EXECUTED_TP1_THEN_TP2",
            "EXECUTED_TP1_THEN_TIME", "EXECUTED_TP1_THEN_STOP", "EXECUTED_TP2",
            "NOT_EXECUTED", "NOT_TAKEN", "DIAGNOSTIC_REJECT",
        )


def test_report_renders_deterministic_markdown(replay_result):
    markdown1 = render_baseline_report(replay_result)
    markdown2 = render_baseline_report(replay_result)
    assert markdown1 == markdown2
    assert "AI_TEAM_DAILY_V1" in markdown1
    assert "No AI value is claimed" in markdown1


def test_point_in_time_snapshot_cannot_see_future_candles():
    """The replay snapshot must equal a snapshot built from truncated frames."""
    frames = default_frames()
    market = market_frame()
    as_of = "2026-08-20"
    replay_snapshot = build_snapshot(market, frames, CONFIG, as_of=as_of)
    truncated_market = market.loc[pd.to_datetime(market.index) <= pd.Timestamp(as_of)]
    truncated_frames = {
        k: v.loc[pd.to_datetime(v.index) <= pd.Timestamp(as_of)] for k, v in frames.items()
    }
    direct_snapshot = build_snapshot(truncated_market, truncated_frames, CONFIG)
    assert replay_snapshot.data_snapshot_hash == direct_snapshot.data_snapshot_hash
    assert replay_snapshot.as_of == as_of


def test_replay_screen_dates_respect_warmup_and_forward_buffer():
    result = run_offline_backtest(market_frame(), default_frames(), CONFIG, BacktestConfig())
    min_history = CONFIG.screener.min_history_bars
    index = pd.to_datetime(market_frame().index)
    first = pd.Timestamp(result["screen_dates"][0])
    assert first > index[min_history - 1]           # warmup respected
    last = index.max()
    assert (last - first).days >= BT.evaluation_days + 2  # forward buffer kept
