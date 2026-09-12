"""Regression test for the deterministic V2 execution fixtures."""
import copy
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def load_v2_module():
    """Load the runner directly so tests need no package installation."""
    path = Path(__file__).with_name("backtest_screener_v2.py")
    spec = importlib.util.spec_from_file_location("backtest_screener_v2_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_execution_self_test():
    load_v2_module().run_self_test()


def test_cached_snapshots_match_legacy_calculation():
    """Caching causal histories must not change a point-in-time signal."""
    module = load_v2_module()
    runner = module.V1
    screener = runner.load_screener(Path("scripts/financial_screener.py"))
    dates = pd.bdate_range("2017-01-02", periods=1_900)

    def make_frame(seed: int) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        close = 1_000 + np.cumsum(rng.normal(0.35, 12, len(dates)))
        opening = close + rng.normal(0, 3, len(dates))
        high = np.maximum(opening, close) + rng.uniform(2, 15, len(dates))
        low = np.minimum(opening, close) - rng.uniform(2, 15, len(dates))
        return pd.DataFrame({
            "Open": opening, "High": high, "Low": low, "Close": close,
            "Volume": rng.integers(1_000_000, 8_000_000, len(dates)),
        }, index=dates)

    pool = ["AAA.JK", "BBB.JK", "CCC.JK"]
    stocks = {ticker: make_frame(number) for number, ticker in enumerate(pool, start=1)}
    # The legacy runner carries a stock's last completed candle across an IHSG
    # session where that ticker did not trade.  Exercise that exact behaviour.
    stocks["BBB.JK"] = stocks["BBB.JK"].drop(index=dates[::7])
    stocks["CCC.JK"] = stocks["CCC.JK"].drop(index=dates[::11])
    ihsg = make_frame(99)
    config = runner.RunConfig(
        dates[0], dates[-1], pd.Timestamp("2024-01-01"), 1_000_000_000, 50, 3, 30,
    )
    timeframes = ("daily_swing", "weekly_position", "monthly_long_term")
    history = runner.build_snapshot_history(screener, pool, stocks, ihsg, timeframes)

    for timeframe in timeframes:
        evaluation_dates = runner.evaluation_dates(
            screener, ihsg, timeframe, dates[0], dates[-1],
        )
        # Cover the first valid history boundary and a mature snapshot.
        first_valid = screener.get_timeframe_config(timeframe)["min_rows"] - 1
        for screen_date in (evaluation_dates[first_valid], evaluation_dates[-1]):
            legacy = runner.screen_snapshot(
                screener, pool, stocks, ihsg, screen_date, timeframe, config,
            )
            cached = runner.screen_snapshot(
                screener, pool, stocks, ihsg, screen_date, timeframe, config,
                history=history,
            )
            assert cached == legacy


def test_hold_selection_uses_constrained_portfolio_not_trade_average():
    """A high-return trade can be unavailable when its portfolio slot is full."""
    module = load_v2_module()
    dates = pd.date_range("2024-01-02", periods=5, freq="B")
    ihsg = pd.DataFrame({
        "Open": [100.0] * 5, "High": [100.0] * 5, "Low": [100.0] * 5,
        "Close": [100.0] * 5,
    }, index=dates)
    stock = pd.DataFrame({
        "Open": [100.0] * 5, "High": [100.0] * 5, "Low": [100.0] * 5,
        "Close": [100.0] * 5, "Volume": [1_000_000] * 5,
    }, index=dates)

    def trade(signal_id, ticker, bars, entry_date, exit_date, exit_price):
        return {
            "signal_id": signal_id, "timeframe": "daily_swing",
            "max_hold_bars": bars, "max_hold_days": bars,
            "screen_date": dates[0], "entry_date": entry_date,
            "exit_date": exit_date, "entry_price": 100.0,
            "exit_price": exit_price, "status": "Ready to Enter",
            "is_top_pick": True, "trade_status": "COMPLETED",
            "order_status": "TRIGGERED", "status_rank": 1,
            "ticker": ticker,
        }

    # Five-day trades look better individually (10% and 30%), but their second
    # trade is skipped with one slot. Ten-day candidates can both be entered.
    trades = pd.DataFrame([
        trade("five-a", "AAA.JK", 5, dates[0], dates[4], 110.0),
        trade("five-b", "BBB.JK", 5, dates[1], dates[2], 130.0),
        trade("ten-a", "AAA.JK", 10, dates[0], dates[1], 107.0),
        trade("ten-b", "BBB.JK", 10, dates[2], dates[4], 107.0),
    ])
    selected, audit = module.select_holds_from_portfolio(
        trades, {"AAA.JK": stock, "BBB.JK": stock}, ihsg, dates[0], dates[-1],
        initial_capital=1_000_000, max_positions=1,
        cfg=module.ExecutionConfig(0.0, 0.0, "stop"), scope="unit",
        minimum_trades={"daily_swing": 1, "weekly_position": 1, "monthly_long_term": 1},
        minimum_pf=1.0, maximum_drawdown_pct=100.0,
    )
    assert selected["daily_swing"] == (10, 10)
    five = audit[audit.max_hold_bars.eq(5)].iloc[0]
    ten = audit[audit.max_hold_bars.eq(10)].iloc[0]
    assert five.skipped_candidates == 1
    assert ten.selected and not five.selected


def test_portfolio_closes_same_day_exit_without_reusing_intraday_slot():
    """A same-session exit closes today but cannot create fictional capacity."""
    module = load_v2_module()
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    stock = pd.DataFrame({
        "Open": [100.0] * 3, "High": [110.0] * 3, "Low": [90.0] * 3,
        "Close": [100.0] * 3, "Volume": [1_000_000] * 3,
    }, index=dates)

    def candidate(signal_id, ticker, rank, entry_date, exit_date, exit_price):
        return {
            "signal_id": signal_id, "ticker": ticker, "status_rank": rank,
            "entry_date": entry_date, "exit_date": exit_date,
            "entry_price": 100.0, "exit_price": exit_price,
            "exit_reason": "STOP" if exit_price < 100 else "TARGET",
        }

    candidates = pd.DataFrame([
        candidate("same-day", "AAA.JK", 1, dates[0], dates[0], 95.0),
        candidate("blocked-today", "BBB.JK", 2, dates[0], dates[1], 110.0),
        candidate("available-tomorrow", "BBB.JK", 1, dates[1], dates[2], 110.0),
    ])
    closed, curve, skipped = module.simulate_portfolio(
        candidates, {"AAA.JK": stock, "BBB.JK": stock}, dates,
        initial_capital=1_000_000, max_positions=1,
        cfg=module.ExecutionConfig(0.0, 0.0, "stop"),
    )

    assert closed.signal_id.tolist() == ["same-day", "available-tomorrow"]
    assert skipped.signal_id.tolist() == ["blocked-today"]
    assert skipped.skip_reason.tolist() == ["CAPACITY_FULL"]
    assert curve.active_positions.tolist() == [0, 1, 0]
    assert not closed.get("forced_end", pd.Series(False, index=closed.index)).fillna(False).any()


def test_portfolio_stop_risk_sizing_and_position_cap_limit_shares():
    """Optional sizing should enforce both stop risk and allocation limits."""
    module = load_v2_module()
    dates = pd.date_range("2024-01-02", periods=2, freq="B")
    stock = pd.DataFrame({
        "Open": [100.0, 110.0], "High": [100.0, 110.0],
        "Low": [100.0, 110.0], "Close": [100.0, 110.0],
        "Volume": [1_000_000, 1_000_000],
    }, index=dates)
    candidate = pd.DataFrame([{
        "signal_id": "risk-sized", "ticker": "AAA.JK", "status_rank": 1,
        "entry_date": dates[0], "exit_date": dates[1],
        "entry_price": 100.0, "stop_price": 90.0, "exit_price": 110.0,
        "exit_reason": "TARGET",
    }])
    closed, _, _ = module.simulate_portfolio(
        candidate, {"AAA.JK": stock}, dates, initial_capital=1_000_000,
        max_positions=1,
        cfg=module.ExecutionConfig(
            0.0, 0.0, "stop", risk_per_trade_pct=1.0,
            max_position_pct=100.0,
        ),
    )
    assert closed.iloc[0].shares == 1_000
    assert closed.iloc[0].entry_cost == 100_000
    assert closed.iloc[0].pnl == 10_000

    capped, _, _ = module.simulate_portfolio(
        candidate, {"AAA.JK": stock}, dates, initial_capital=1_000_000,
        max_positions=1,
        cfg=module.ExecutionConfig(
            0.0, 0.0, "stop", risk_per_trade_pct=0.0,
            max_position_pct=5.0,
        ),
    )
    assert capped.iloc[0].shares == 500
    assert capped.iloc[0].entry_cost == 50_000


def test_point_in_time_audit_requires_manifest_and_records_coverage(tmp_path):
    module = load_v2_module()
    dates = pd.bdate_range("2024-01-02", periods=5)
    frame = pd.DataFrame({
        "Open": [100.0] * 4, "High": [101.0] * 4, "Low": [99.0] * 4,
        "Close": [100.0] * 4, "Volume": [1_000] * 4,
    }, index=dates.delete(2))
    ihsg = pd.DataFrame({"Close": [100.0] * 5}, index=dates)
    excel = tmp_path / "universe.xlsx"
    excel.write_bytes(b"test universe")

    summary = module.write_point_in_time_audit(
        tmp_path / "without_manifest", ["AAA.JK", "BBB.JK"], {"AAA.JK": frame},
        ihsg, dates[0], dates[3], dates[-1], excel,
    )
    audit = pd.read_csv(tmp_path / "without_manifest" / "universe_audit.csv")
    assert summary["certification"] == "NOT_CERTIFIED_NO_HISTORICAL_MANIFEST"
    assert (tmp_path / "without_manifest" / "point_in_time_universe_template.csv").is_file()
    assert audit.loc[audit.ticker.eq("BBB.JK"), "audit_status"].item() == "NO_MARKET_DATA"
    assert audit.loc[audit.ticker.eq("AAA.JK"), "max_consecutive_missing_sessions"].item() == 1

    manifest = tmp_path / "membership.csv"
    pd.DataFrame({
        "ticker": ["AAA"], "effective_start": ["2024-01-01"],
        "effective_end": ["2024-12-31"],
    }).to_csv(manifest, index=False)
    summary = module.write_point_in_time_audit(
        tmp_path / "with_manifest", ["AAA.JK"], {"AAA.JK": frame},
        ihsg, dates[0], dates[3], dates[-1], excel, manifest,
    )
    assert summary["certification"] == "MANIFEST_PRESENT_MANUAL_SOURCE_REVIEW_REQUIRED"


def test_ipo_proxy_uses_idx_listing_date_and_ohlcv_availability(tmp_path):
    module = load_v2_module()
    dates = pd.bdate_range("2024-01-02", periods=5)
    frame = pd.DataFrame({
        "Open": [100.0] * 5, "High": [101.0] * 5, "Low": [99.0] * 5,
        "Close": [100.0] * 5, "Volume": [1_000] * 5,
    }, index=dates)
    excel = tmp_path / "idx.xlsx"
    pd.DataFrame({
        "Kode": ["AAA", "BBB", "NONE"],
        "Tanggal Pencatatan": ["01 Jan 2020", "04 Jan 2024", "01 Jan 2020"],
    }).to_excel(excel, index=False)
    manifest_path = tmp_path / "ipo_proxy.csv"
    manifest = module.write_ipo_proxy_manifest(
        manifest_path, excel, {"AAA.JK": frame, "BBB.JK": frame}, dates[-1],
    )
    assert manifest.loc[manifest.ticker.eq("AAA.JK"), "effective_start"].item() == "2024-01-02"
    assert manifest.loc[manifest.ticker.eq("BBB.JK"), "effective_start"].item() == "2024-01-04"
    loaded = module.load_membership_manifest(manifest_path)
    assert module.active_manifest_tickers(loaded, ["AAA.JK", "BBB.JK"], dates[1]) == ["AAA.JK"]
    assert module.active_manifest_tickers(loaded, ["AAA.JK", "BBB.JK"], dates[2]) == ["AAA.JK", "BBB.JK"]


def test_v3_rank_ablation_preserves_ready_eligibility():
    runner = load_v2_module().V1
    baseline = runner.load_screener(Path("scripts/financial_screener.py"))
    v3 = runner.load_screener(Path("scripts/financial_screener_v3.py"))
    candidate = {
        "rs_excess": 0.10, "rs_trend_up": True, "rs_percentile": 0.0,
        "hard_pass": True, "hard_fail_reasons": [],
        "conditions": {
            "trend": True, "relative_strength": False, "momentum": True,
            "macd": True, "volume": True, "setup": True, "volatility": True,
        },
    }
    regime = {"regime": "BULLISH"}
    base_result = baseline.finalisasi_score_dan_status([copy.deepcopy(candidate)], "daily_swing", regime)[0]
    v3_result = v3.finalisasi_score_dan_status([copy.deepcopy(candidate)], "daily_swing", regime)[0]
    assert base_result["status"] == v3_result["status"] == "Ready to Enter"
    assert base_result["quality_score"] == 100.0
    assert v3_result["eligibility_score"] == 100.0
    assert v3_result["quality_score"] == 75.0


def test_setup_family_restricts_and_reranks_ready_candidates():
    module = load_v2_module()
    screener = module.V1.load_screener(Path("scripts/financial_screener.py"))

    def candidate(status, setup, score):
        return {
            "status": status, "normalized_status": status, "setup_name": setup,
            "quality_score": score, "rs_percentile": 80.0, "breakout_ok": False,
            "vol_z": 1.0, "turnover20": 2_000_000_000.0,
        }

    candidates = [
        candidate("Ready to Enter", "Bullish Pullback/Reclaim", 95.0),
        candidate("Ready to Enter", "Breakout Confirmed", 70.0),
        candidate("Ready to Enter", "Marubozu Bullish", 80.0),
        candidate("Wait for Trigger", "Bullish Pullback/Reclaim", 60.0),
    ]
    filtered = module.apply_ready_setup_family(screener, candidates, "breakout")
    ready = [item for item in filtered if item["normalized_status"] == "Ready to Enter"]
    wait = [item for item in filtered if item["normalized_status"] == "Wait for Trigger"]
    assert len(ready) == 1 and ready[0]["setup_name"] == "Breakout Confirmed"
    assert ready[0]["status_rank"] == 1
    assert len(wait) == 1


def test_v4_uses_one_global_rank_across_ready_and_wait():
    module = load_v2_module()
    runner = module.V1
    screener = runner.load_screener(Path("scripts/financial_screener_v4.py"))
    candidates = [
        {"ticker": "WAIT.JK", "status": screener.STATUS_WAIT, "quality_score": 90.0,
         "rs_percentile": 90.0, "turnover20": 2_000_000_000.0},
        {"ticker": "READY.JK", "status": screener.STATUS_READY, "quality_score": 80.0,
         "rs_percentile": 80.0, "turnover20": 2_000_000_000.0},
    ]
    ranked = runner.rank_candidates(screener, candidates)
    assert [item["ticker"] for item in ranked] == ["WAIT.JK", "READY.JK"]
    assert [item["status_rank"] for item in ranked] == [1, 2]


def test_v4_timeframes_have_independent_ranking_models():
    runner = load_v2_module().V1
    screener = runner.load_screener(Path("scripts/financial_screener_v4.py"))
    assert set(screener.RANKING_MODELS) == {
        "daily_swing", "weekly_position", "monthly_long_term",
    }
    assert set(screener.RANKING_MODELS["daily_swing"]) == {
        "rs_63", "trend_quality_20", "turnover_20",
    }
    assert set(screener.RANKING_MODELS["weekly_position"]) == {
        "rs_13", "rs_26", "high_52", "risk_adjusted_26",
    }
    assert set(screener.RANKING_MODELS["monthly_long_term"]) == {
        "rs_12_1", "high_52", "risk_adjusted_6",
    }
    for weights in screener.RANKING_MODELS.values():
        assert sum(weights.values()) == 1.0


def test_v4_analyzes_synthetic_history_on_every_timeframe():
    runner = load_v2_module().V1
    screener = runner.load_screener(Path("scripts/financial_screener_v4.py"))
    dates = pd.bdate_range("2015-01-02", periods=2_700)
    steps = np.arange(len(dates), dtype=float)

    def frame(drift: float, wave: float) -> pd.DataFrame:
        close = 1_000 + drift * steps + wave * np.sin(steps / 17)
        opening = close - 2 * np.sin(steps / 5)
        return pd.DataFrame({
            "Open": opening,
            "High": np.maximum(opening, close) + 12,
            "Low": np.minimum(opening, close) - 12,
            "Close": close,
            "Volume": np.full(len(dates), 5_000_000),
        }, index=dates)

    stock = frame(0.35, 22)
    benchmark = frame(0.15, 10)
    breadth = {
        "breadth50": 0.60, "breadth200": 0.55,
        "median_return20": 0.01, "jumlah_saham_breadth": 100,
    }
    for mode, expected_components in screener.RANKING_MODELS.items():
        stock_tf = screener.siapkan_data_untuk_timeframe(stock, mode)
        benchmark_tf = screener.siapkan_data_untuk_timeframe(benchmark, mode)
        candidate = screener.analisa_saham_confluence(
            "TEST.JK", None, benchmark_tf, mode, 1_000_000_000, 100,
            prepared_tf=stock_tf, liquidity=(5_000_000_000, 5_000_000_000),
        )
        assert not candidate.get("error"), candidate.get("alasan")
        assert set(candidate["v4_components"]) == set(expected_components)
        regime = screener.analisa_market_regime(benchmark, mode, breadth)
        result = screener.finalisasi_score_dan_status([candidate], mode, regime)[0]
        assert result["status"] in {
            screener.STATUS_READY, screener.STATUS_WAIT,
            screener.STATUS_SKIP_SETUP, screener.STATUS_SKIP_TREND,
            screener.STATUS_SKIP_EXTENDED, screener.STATUS_SKIP_LIQUIDITY,
        }


def test_v4_precomputed_features_are_point_in_time_equivalent():
    runner = load_v2_module().V1
    screener = runner.load_screener(Path("scripts/financial_screener_v4.py"))
    dates = pd.bdate_range("2015-01-02", periods=2_700)
    steps = np.arange(len(dates), dtype=float)
    close = 1_000 + 0.25 * steps + 18 * np.sin(steps / 19)
    frame = pd.DataFrame({
        "Open": close - 1,
        "High": close + 10,
        "Low": close - 10,
        "Close": close,
        "Volume": np.full(len(dates), 4_000_000),
    }, index=dates)
    snapshot = dates[-40]

    for mode in screener.RANKING_MODELS:
        cached = screener.siapkan_data_untuk_timeframe(frame, mode).loc[:snapshot]
        direct = screener.siapkan_data_untuk_timeframe(frame.loc[:snapshot], mode)
        pd.testing.assert_frame_equal(
            cached.tail(1), direct.tail(1), check_exact=False, rtol=1e-12, atol=1e-12,
        )


def test_v5_changes_daily_weekly_and_keeps_monthly_model():
    runner = load_v2_module().V1
    v4 = runner.load_screener(Path("scripts/financial_screener_v4.py"))
    v5 = runner.load_screener(Path("scripts/financial_screener_v5.py"))
    assert set(v5.RANKING_MODELS["daily_swing"]) == {
        "rs_63", "trend_quality_20", "recovery_strength", "volume_confirmation",
    }
    assert v5.RANKING_MODELS["weekly_position"]["rs_26"] == 0.40
    assert v5.RANKING_MODELS["monthly_long_term"] == v4.RANKING_MODELS["monthly_long_term"]
    assert (10, 50) in v5.BACKTEST_HOLD_GRID["weekly_position"]

    dates = pd.bdate_range("2015-01-02", periods=2_700)
    steps = np.arange(len(dates), dtype=float)
    close = 1_000 + 0.35 * steps + 22 * np.sin(steps / 17)
    frame = pd.DataFrame({
        "Open": close - 2 * np.sin(steps / 5),
        "High": close + 12,
        "Low": close - 12,
        "Close": close,
        "Volume": np.full(len(dates), 5_000_000),
    }, index=dates)
    benchmark = frame.assign(Close=1_000 + 0.15 * steps + 10 * np.sin(steps / 17))
    for mode, components in v5.RANKING_MODELS.items():
        stock_tf = v5.siapkan_data_untuk_timeframe(frame, mode)
        benchmark_tf = v5.siapkan_data_untuk_timeframe(benchmark, mode)
        candidate = v5.analisa_saham_confluence(
            "TEST.JK", None, benchmark_tf, mode, 1_000_000_000, 100,
            prepared_tf=stock_tf, liquidity=(5_000_000_000, 5_000_000_000),
        )
        assert not candidate.get("error"), candidate.get("alasan")
        assert set(candidate["v4_components"]) == set(components)


def test_v6_turns_daily_recovery_into_planned_follow_through():
    runner = load_v2_module().V1
    v6 = runner.load_screener(Path("scripts/financial_screener_v6.py"))
    v6._V5._daily_metrics = lambda frame, benchmark, turnover: {
        "components": {"test": 1.0},
        "primary_rs": 0.1,
        "stock_return": 0.2,
        "momentum_valid": True,
        "setup_valid": True,
        "trigger_active": True,
        "trigger_reference": 99.0,
        "setup_name": "Daily Pullback Recovery Confirmed",
    }
    frame = pd.DataFrame({"High": [101.0], "Low": [95.0], "Close": [100.0]})
    result = v6._daily_metrics(frame, frame, 1_000_000_000)
    assert result["setup_valid"]
    assert not result["trigger_active"]
    assert result["trigger_reference"] == 101.0
    assert result["setup_name"] == "Daily Recovery - Wait Follow-Through"
    assert v6.BACKTEST_HOLD_GRID["daily_swing"] == [(5, 5)]
    assert v6.BACKTEST_HOLD_GRID["weekly_position"] == [(8, 40)]


def test_v6_weekly_rejects_stop_distance_above_safety_limit():
    runner = load_v2_module().V1
    v6 = runner.load_screener(Path("scripts/financial_screener_v6.py"))
    v6._V5_ANALYZE = lambda *args, **kwargs: {
        "planned_entry": 100.0,
        "stop_level": 70.0,
        "hard_pass": True,
        "hard_fail_reasons": [],
    }
    result = v6.analisa_saham_confluence(
        "TEST.JK", None, pd.DataFrame(), "weekly_position",
        1_000_000_000, 100,
    )
    assert result["planned_stop_distance_pct"] == 30.0
    assert not result["v6_weekly_risk_guard"]
    assert not result["hard_pass"]
    assert "jarak stop > 25%" in result["hard_fail_reasons"]


def test_early_invalidation_exits_at_close_after_entry():
    module = load_v2_module()
    dates = pd.date_range("2025-01-02", periods=3, freq="B")
    stock = pd.DataFrame({
        "Open": [100.0, 105.0, 100.0],
        "High": [106.0, 106.0, 101.0],
        "Low": [99.0, 99.0, 98.0],
        "Close": [101.0, 100.0, 99.0],
        "Volume": [1_000_000] * 3,
    }, index=dates)
    ihsg = stock.copy()
    signal = {
        "signal_id": "early-exit", "timeframe": "daily_swing",
        "screen_date": dates[0] - pd.Timedelta(days=1), "ticker": "TEST.JK",
        "status": "Wait for Trigger", "status_rank": 1,
        "entry_type": "planned", "entry_window": 3,
        "trigger_price": 105.0, "stop_price": 95.0, "target_price": 120.0,
        "early_exit_price": 102.0,
    }
    result = module.simulate_signal(
        signal, stock, ihsg, 3, 3,
        module.ExecutionConfig(0.0, 0.0, "stop"),
    )
    assert result["exit_reason"] == "EARLY_INVALIDATION"
    assert result["exit_date"] == dates[0]
    assert result["exit_price"] == 101.0


def test_recommendation_summary_treats_one_screen_as_one_run():
    module = load_v2_module()
    date = pd.Timestamp("2025-01-03")
    rows = []
    for rank, result in ((1, 10.0), (2, -4.0)):
        rows.append({
            "timeframe": "weekly_position", "max_hold_bars": 8,
            "max_hold_days": 40, "sample_split": "SELECTION",
            "status": "Ready to Enter", "screen_date": date,
            "status_rank": rank, "is_top_pick": True,
            "trade_status": "COMPLETED", "order_status": "TRIGGERED",
            "net_return_pct": result, "net_excess_vs_ihsg_pct": result - 1,
            "ihsg_return_pct": 1.0, "exit_reason": "MAX_HOLD",
        })
    summary = module.create_recommendation_summary(pd.DataFrame(rows))
    combined = summary[summary.recommendation_policy.eq("ALL_TOP_PICKS")].iloc[0]
    assert combined.recommendation_runs == 1
    assert combined.average_picks_per_run == 2
    assert combined.mean_basket_return_pct == 3.0
    assert combined.median_basket_return_pct == 3.0
    assert combined.positive_basket_run_rate_pct == 100.0


def test_legacy_analysis_accepts_no_ready_portfolio_candidates(tmp_path):
    module = load_v2_module()
    path = tmp_path / "analysis.md"
    module.write_analysis(
        path,
        pd.DataFrame(),
        {
            "screener_variant_id": "TEST",
            "ready_setup_family": "all",
            "fee_pct": 0.2,
            "slippage_pct": 0.1,
            "universe_membership_applied_per_snapshot": True,
        },
        pd.DataFrame(),
    )
    assert path.is_file()
    assert "Selection summary" in path.read_text()


def test_v7_daily_contraction_requires_confirmed_breakout():
    runner = load_v2_module().V1
    v7 = runner.load_screener(Path("scripts/financial_screener_v7.py"))
    dates = pd.bdate_range("2024-01-02", periods=70)
    close = np.linspace(80.0, 98.0, len(dates))
    close[-11:-1] = np.linspace(99.0, 100.0, 10)
    close[-1] = 102.0
    frame = pd.DataFrame({
        "Open": close - 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": np.full(len(dates), 100.0),
        "Vol_MA20": np.full(len(dates), 100.0),
        "ATR": np.full(len(dates), 2.0),
        "ATR_Pct": np.full(len(dates), 0.03),
        "EMA20": np.linspace(79.0, 99.0, len(dates)),
        "EMA50": np.linspace(77.0, 95.0, len(dates)),
        "RSI": np.full(len(dates), 60.0),
        "Trend_Quality20": np.full(len(dates), 0.10),
        "High52_Position": np.full(len(dates), 0.95),
        "Candle_Range": np.full(len(dates), 2.0),
    }, index=dates)
    frame.loc[dates[-10:-1], "ATR_Pct"] = 0.015
    frame.loc[dates[-1], ["Open", "High", "Low", "Close", "Volume", "Candle_Range"]] = [
        100.0, 103.0, 99.0, 102.0, 150.0, 4.0,
    ]
    benchmark = frame.copy()
    benchmark["Close"] = 80.0

    active = v7._daily_metrics(frame, benchmark, 1_000_000_000)
    assert active["setup_valid"]
    assert active["trigger_active"]
    assert active["setup_name"] == "Daily Contraction Breakout"
    assert set(active["components"]) == set(v7.RANKING_MODELS["daily_swing"])

    waiting_frame = frame.copy()
    waiting_frame.loc[dates[-1], ["Open", "High", "Low", "Close", "Volume", "Candle_Range"]] = [
        99.0, 101.0, 98.0, 100.0, 100.0, 3.0,
    ]
    waiting = v7._daily_metrics(waiting_frame, benchmark, 1_000_000_000)
    assert waiting["setup_valid"]
    assert not waiting["trigger_active"]
    assert waiting["setup_name"] == "Daily Contraction - Wait Breakout"
    assert v7.BACKTEST_HOLD_GRID["daily_swing"] == [(5, 5)]


def test_v8_daily_requires_weekly_trend_and_daily_recovery():
    runner = load_v2_module().V1
    v8 = runner.load_screener(Path("scripts/financial_screener_v8.py"))
    dates = pd.bdate_range("2024-01-02", periods=70)
    close = np.linspace(96.0, 105.0, len(dates))
    close[-6:-1] = [102.0, 101.0, 100.0, 101.0, 102.0]
    close[-1] = 104.0
    frame = pd.DataFrame({
        "Open": close - 0.5,
        "High": close + 0.8,
        "Low": close - 0.8,
        "Close": close,
        "Volume": np.full(len(dates), 100.0),
        "Vol_MA20": np.full(len(dates), 100.0),
        "ATR": np.full(len(dates), 2.0),
        "EMA9": np.linspace(95.0, 102.0, len(dates)),
        "EMA20": np.linspace(94.0, 101.0, len(dates)),
        "EMA50": np.linspace(90.0, 96.0, len(dates)),
        "RSI": np.full(len(dates), 60.0),
        "Candle_Range": np.full(len(dates), 1.6),
        "W_Close": np.full(len(dates), 120.0),
        "W_EMA20": np.full(len(dates), 110.0),
        "W_EMA50": np.full(len(dates), 100.0),
        "W_RSI": np.full(len(dates), 60.0),
        "W_ATR": np.full(len(dates), 8.0),
        "W_Return13": np.full(len(dates), 0.20),
        "W_Return26": np.full(len(dates), 0.35),
        "W_RiskAdjusted26": np.full(len(dates), 3.0),
        "W_High52_Position": np.full(len(dates), 0.95),
        "W_ObservationCount": np.full(len(dates), 80.0),
    }, index=dates)
    frame.loc[dates[-1], ["Open", "High", "Low", "Close", "Candle_Range"]] = [
        102.0, 105.0, 101.5, 104.0, 3.5,
    ]
    benchmark = frame.copy()
    benchmark["W_Return13"] = 0.05
    benchmark["W_Return26"] = 0.10

    active = v8._daily_metrics(frame, benchmark, 1_000_000_000)
    assert active["momentum_valid"]
    assert active["setup_valid"]
    assert active["trigger_active"]
    assert active["setup_name"] == "Weekly Trend - Daily Recovery Confirmed"
    assert set(active["components"]) == set(v8.RANKING_MODELS["daily_swing"])

    broken_weekly = frame.copy()
    broken_weekly["W_EMA20"] = 95.0
    inactive = v8._daily_metrics(broken_weekly, benchmark, 1_000_000_000)
    assert not inactive["momentum_valid"]
    assert not inactive["setup_valid"]
    assert not inactive["trigger_active"]
    assert v8.BACKTEST_HOLD_GRID["daily_swing"] == [(10, 10)]


def test_v8_weekly_context_never_uses_an_unfinished_week():
    runner = load_v2_module().V1
    v8 = runner.load_screener(Path("scripts/financial_screener_v8.py"))
    dates = pd.bdate_range("2022-01-03", periods=320)
    close = pd.Series(np.arange(len(dates), dtype=float) + 100.0, index=dates)
    raw = pd.DataFrame({
        "Open": close - 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": np.full(len(dates), 1_000_000.0),
    }, index=dates)
    prepared = v8.siapkan_data_untuk_timeframe(raw, "daily_swing")
    monday = next(date for date in dates[-40:] if date.weekday() == 0)
    previous_friday = monday - pd.Timedelta(days=3)
    assert prepared.loc[monday, "W_Close"] == raw.loc[previous_friday, "Close"]
    friday = next(date for date in dates[-40:] if date.weekday() == 4)
    assert prepared.loc[friday, "W_Close"] == raw.loc[friday, "Close"]


def test_weekly_paper_tracker_is_top_three_and_idempotent(tmp_path):
    path = Path("scripts/paper_track_weekly.py")
    spec = importlib.util.spec_from_file_location("paper_track_weekly_test", path)
    tracker = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = tracker
    spec.loader.exec_module(tracker)

    source = tmp_path / "weekly_position.csv"
    pd.DataFrame([
        {
            "ticker": f"T{rank}.JK", "date": "2026-09-11",
            "timeframe": "weekly_position", "status": "Ready to Enter",
            "universe_rank": rank, "close": 100 + rank,
            "entry_type": "active",
            "planned_entry": 105 + rank, "trigger_price": 105 + rank,
            "stop": 90 + rank, "target": 130 + rank, "entry_window": 5,
            "max_holding_bars": 8,
            "max_hold_days": 40, "setup": "Weekly Momentum",
            "hard_pass": True,
        }
        for rank in range(1, 5)
    ]).to_csv(source, index=False)
    ledger = tmp_path / "paper" / "picks.csv"
    kwargs = {
        "as_of": "2026-09-12",
        "recorded_at": "2026-09-12T09:00:00+07:00",
    }

    first, created = tracker.capture_snapshot(source, ledger, **kwargs)
    second, created_again = tracker.capture_snapshot(source, ledger, **kwargs)

    assert created
    assert not created_again
    assert first["ticker"].tolist() == ["T1.JK", "T2.JK", "T3.JK"]
    assert len(second) == 3
    assert len(pd.read_csv(ledger)) == 3
    assert len(pd.read_csv(ledger.with_name("runs.csv"))) == 1
    assert first["outcome_status"].eq("PENDING").all()


def test_frozen_weekly_candidate_never_pads_with_wait_setups():
    runner = load_v2_module().V1
    weekly = runner.load_screener(Path("scripts/financial_screener_weekly.py"))
    candidates = [
        {
            "ticker": "READY.JK", "status": weekly.STATUS_READY,
            "quality_score": 70.0, "rs_percentile": 60.0,
            "turnover20": 2_000_000_000,
        },
        {
            "ticker": "WAIT.JK", "status": weekly.STATUS_WAIT,
            "quality_score": 99.0, "rs_percentile": 99.0,
            "turnover20": 9_000_000_000,
        },
    ]

    selected, status = weekly.ranking_candidates(candidates, limit=3)

    assert [candidate["ticker"] for candidate in selected] == ["READY.JK"]
    assert status == weekly.STATUS_READY
    assert weekly.VARIANT_ID == "WEEKLY_V1_READY_ONLY_40D"
    assert weekly.BACKTEST_HOLD_GRID["weekly_position"] == [(8, 40)]
    with np.testing.assert_raises(ValueError):
        weekly.main(["--timeframe", "daily_swing"])


def test_frozen_monthly_candidate_keeps_model_and_126_day_horizon():
    runner = load_v2_module().V1
    monthly = runner.load_screener(Path("scripts/financial_screener_monthly.py"))
    v5 = runner.load_screener(Path("scripts/financial_screener_v5.py"))

    assert monthly.VARIANT_ID == "MONTHLY_V1_MOMENTUM_126D"
    assert monthly.BACKTEST_HOLD_GRID == {"monthly_long_term": [(6, 126)]}
    assert monthly.RANKING_MODELS["monthly_long_term"] == {
        "rs_12_1": 0.50,
        "high_52": 0.30,
        "risk_adjusted_6": 0.20,
    }
    assert (
        monthly.RANKING_MODELS["monthly_long_term"]
        == v5.RANKING_MODELS["monthly_long_term"]
    )
    with np.testing.assert_raises(ValueError):
        monthly.main(["--timeframe", "weekly_position"])


def test_monthly_paper_tracker_records_monthly_source(tmp_path):
    path = Path("scripts/paper_track_monthly.py")
    spec = importlib.util.spec_from_file_location("paper_track_monthly_test", path)
    tracker = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = tracker
    spec.loader.exec_module(tracker)

    source = tmp_path / "monthly_long_term.csv"
    pd.DataFrame([
        {
            "ticker": f"M{rank}.JK", "date": "2026-08-31",
            "timeframe": "monthly_long_term", "status": "Ready to Enter",
            "universe_rank": rank, "close": 100 + rank,
            "entry_type": "active",
            "planned_entry": 105 + rank, "trigger_price": 105 + rank,
            "stop": 80 + rank, "target": 180 + rank, "entry_window": 10,
            "max_holding_bars": 6, "max_hold_days": 126,
            "setup": "Monthly Momentum Rebalance",
            "hard_pass": True,
        }
        for rank in range(1, 4)
    ]).to_csv(source, index=False)
    ledger = tmp_path / "paper_monthly" / "picks.csv"

    picks, created = tracker.capture_snapshot(
        source,
        ledger,
        as_of="2026-09-01",
        recorded_at="2026-09-01T09:00:00+07:00",
    )

    assert created
    assert len(picks) == 3
    assert picks["timeframe"].eq("monthly_long_term").all()
    assert picks["model_id"].eq("MONTHLY_V1_MOMENTUM_126D").all()


def test_paper_resolver_uses_backtest_execution_and_writes_summary(tmp_path):
    tracker_path = Path("scripts/paper_track_weekly.py")
    tracker_spec = importlib.util.spec_from_file_location("resolver_tracker_test", tracker_path)
    tracker = importlib.util.module_from_spec(tracker_spec)
    assert tracker_spec.loader is not None
    sys.modules[tracker_spec.name] = tracker
    tracker_spec.loader.exec_module(tracker)
    resolver_path = Path("scripts/paper_resolve.py")
    resolver_spec = importlib.util.spec_from_file_location("paper_resolver_test", resolver_path)
    resolver = importlib.util.module_from_spec(resolver_spec)
    assert resolver_spec.loader is not None
    sys.modules[resolver_spec.name] = resolver
    resolver_spec.loader.exec_module(resolver)

    screen_date = pd.Timestamp("2026-01-02")
    source = tmp_path / "weekly_position.csv"
    pd.DataFrame([{
        "ticker": "TEST.JK", "date": str(screen_date.date()),
        "timeframe": "weekly_position", "status": "Ready to Enter",
        "universe_rank": 1, "close": 100, "entry_type": "active",
        "planned_entry": 100, "trigger_price": 100, "stop": 80,
        "target": 150, "entry_window": 5, "max_holding_bars": 8,
        "max_hold_days": 40, "setup": "Weekly Momentum", "hard_pass": True,
    }]).to_csv(source, index=False)
    ledger = tmp_path / "paper" / "picks.csv"
    tracker.capture_snapshot(
        source, ledger, as_of="2026-01-03",
        recorded_at="2026-01-03T09:00:00+07:00",
    )
    dates = pd.bdate_range(screen_date, periods=45)
    close = np.linspace(100.0, 120.0, len(dates))
    stock = pd.DataFrame({
        "Open": close, "High": close + 1, "Low": close - 1,
        "Close": close, "Volume": np.full(len(dates), 1_000_000),
    }, index=dates)
    ihsg = stock.copy()
    ihsg[["Open", "High", "Low", "Close"]] = 100.0

    resolved, count = resolver.resolve_pending(
        ledger, ihsg, {"TEST.JK": stock}, cutoff=str(dates[-1].date()),
        resolved_at="2026-03-06T17:00:00+07:00",
    )

    assert count == 1
    assert resolved.iloc[0]["outcome_status"] == "COMPLETED"
    assert float(resolved.iloc[0]["net_return_pct"]) > 0
    assert ledger.with_name("summary.csv").is_file()
    assert "WEEKLY_V1_READY_ONLY_40D" in ledger.with_name("paper_report.md").read_text()


def test_final_entrypoint_routes_only_frozen_timeframes():
    path = Path("scripts/financial_screener_final.py")
    spec = importlib.util.spec_from_file_location("financial_screener_final_test", path)
    final = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = final
    spec.loader.exec_module(final)

    assert final.target_for("weekly_position").name == "financial_screener_weekly.py"
    assert final.target_for("monthly_long_term").name == "financial_screener_monthly.py"
    with np.testing.assert_raises(ValueError):
        final.target_for("daily_swing")
