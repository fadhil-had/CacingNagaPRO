"""V4 research screener with independent models for three holding horizons.

V4 keeps the proven data-loading and execution contract from the baseline, but
separates three concepts that V1 mixed into one confluence score:

* eligibility: liquidity, price, trend, and volatility requirements;
* ranking: cross-sectional percentile score with timeframe-specific factors;
* trigger: whether an eligible ranked candidate is actionable now.

The module exports the same public hooks used by ``backtest_screener_v2.py``.
It is a research candidate and does not replace ``financial_screener.py``.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd


_BASE_PATH = Path(__file__).with_name("financial_screener.py")
_SPEC = importlib.util.spec_from_file_location("financial_screener_v4_base", _BASE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load baseline screener: {_BASE_PATH}")
_BASE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BASE
_SPEC.loader.exec_module(_BASE)

for _name in dir(_BASE):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_BASE, _name)


VARIANT_ID = "V4_TIMEFRAME_SPECIFIC_PERCENTILE_RANK"
GLOBAL_RANKING = True
STATUS_SKIP_SETUP = "Skip - Setup"

RANKING_MODELS = {
    "daily_swing": {
        "rs_63": 0.60,
        "trend_quality_20": 0.25,
        "turnover_20": 0.15,
    },
    "weekly_position": {
        "rs_13": 0.40,
        "rs_26": 0.25,
        "high_52": 0.20,
        "risk_adjusted_26": 0.15,
    },
    "monthly_long_term": {
        "rs_12_1": 0.50,
        "high_52": 0.30,
        "risk_adjusted_6": 0.20,
    },
}


def _finite(value: object, default: float = np.nan) -> float:
    return safe_float(value, default)


def _period_return(frame: pd.DataFrame, lookback: int, end_offset: int = 0) -> float:
    close = frame["Close"].dropna()
    end_index = len(close) - 1 - end_offset
    start_index = end_index - lookback
    if start_index < 0:
        return np.nan
    start, end = float(close.iloc[start_index]), float(close.iloc[end_index])
    return end / start - 1.0 if start > 0 else np.nan


def _excess_return(
    stock: pd.DataFrame,
    benchmark: pd.DataFrame,
    lookback: int,
    end_offset: int = 0,
) -> tuple[float, float]:
    aligned = pd.concat(
        [stock["Close"].rename("stock"), benchmark["Close"].rename("benchmark")],
        axis=1,
        join="inner",
    ).dropna()
    stock_return = _period_return(aligned.rename(columns={"stock": "Close"}), lookback, end_offset)
    benchmark_return = _period_return(
        aligned.rename(columns={"benchmark": "Close"}), lookback, end_offset,
    )
    if not np.isfinite(stock_return) or not np.isfinite(benchmark_return):
        return np.nan, stock_return
    return stock_return - benchmark_return, stock_return


def _risk_adjusted_return(frame: pd.DataFrame, lookback: int) -> float:
    total_return = _period_return(frame, lookback)
    volatility = frame["Close"].pct_change().tail(lookback).std()
    if not np.isfinite(total_return) or not np.isfinite(volatility) or volatility <= 0:
        return np.nan
    return float(total_return / volatility)


def tambah_indikator(tf: pd.DataFrame, mode_tren: str) -> pd.DataFrame:
    """Add only the V4 features that are reusable across historical snapshots."""
    mode = normalize_timeframe(mode_tren)
    df = _BASE.tambah_indikator(tf, mode)
    close = df["Close"]
    df["EMA10"] = close.ewm(span=10, adjust=False).mean()
    df["RSI5"] = _BASE.hitung_rsi(close, 5)
    mean20 = close.rolling(20).mean()
    std20 = close.rolling(20).std().replace(0, np.nan)
    df["Z20"] = (close - mean20) / std20

    log_close = np.log(close.where(close > 0))
    x = pd.Series(np.arange(len(df), dtype=float), index=df.index)
    slope20 = log_close.rolling(20).cov(x) / x.rolling(20).var()
    return_vol20 = close.pct_change().rolling(20).std().replace(0, np.nan)
    df["Trend_Quality20"] = slope20 / return_vol20
    df["Prev_High4"] = df["High"].rolling(4).max().shift(1)
    high_window = {
        "daily_swing": 252,
        "weekly_position": 52,
        "monthly_long_term": 12,
    }[mode]
    df["High52_Position"] = close / close.rolling(high_window).max()
    return df


def siapkan_data_untuk_timeframe(df_saham: pd.DataFrame, mode_tren: str) -> pd.DataFrame:
    """Prepare causal V4 features and preserve the daily 200-day trend."""
    mode = normalize_timeframe(mode_tren)
    daily = _BASE.buang_daily_candle_belum_selesai(df_saham)
    tf = _BASE.resample_timeframe(daily, mode)
    prepared = tambah_indikator(tf, mode)

    if mode == "monthly_long_term":
        daily_close = daily["Close"].dropna()
        daily_trend = pd.DataFrame({
            "Daily_SMA200": daily_close.rolling(200).mean(),
            "Daily_SMA200_21Ago": daily_close.rolling(200).mean().shift(21),
        })
        monthly_trend = daily_trend.resample("ME").last()
        prepared = prepared.join(monthly_trend, how="left")
    return prepared


def analisa_market_regime(
    ihsg_daily: pd.DataFrame,
    mode_tren: str,
    breadth: dict,
) -> dict:
    """Keep the baseline regime diagnostics and add the explicit V4 gate."""
    mode = normalize_timeframe(mode_tren)
    regime = _BASE.analisa_market_regime(ihsg_daily, mode, breadth)
    frame = siapkan_data_untuk_timeframe(ihsg_daily, mode)
    current = frame.iloc[-1]
    if mode == "daily_swing":
        market_trend_ok = current["Close"] > current["EMA200"]
    elif mode == "weekly_position":
        market_trend_ok = current["Close"] > current["EMA20"]
    else:
        market_trend_ok = (
            current["Close"] > current["Daily_SMA200"]
            and current["Daily_SMA200"] > current["Daily_SMA200_21Ago"]
        )
    regime["market_trend_ok"] = bool(market_trend_ok)
    return regime


def _timeframe_metrics(
    frame: pd.DataFrame,
    ihsg_tf: pd.DataFrame,
    mode: str,
    turnover20: float,
) -> dict:
    current, previous = frame.iloc[-1], frame.iloc[-2]
    close = float(current["Close"])

    if mode == "daily_swing":
        rs63, stock_return = _excess_return(frame, ihsg_tf, 63)
        pullback_depth = _finite(frame["Z20"].tail(5).min())
        recent_rsi5 = _finite(frame["RSI5"].tail(5).min())
        setup_valid = (
            np.isfinite(rs63) and rs63 > 0
            and -1.75 <= pullback_depth <= -0.25
            and recent_rsi5 < 45
        )
        ema9_reclaim = previous["Close"] <= previous["EMA9"] and close > current["EMA9"]
        trigger_active = bool(close > previous["High"] or ema9_reclaim)
        return {
            "components": {
                "rs_63": rs63,
                "trend_quality_20": _finite(current["Trend_Quality20"]),
                "turnover_20": turnover20,
            },
            "primary_rs": rs63,
            "stock_return": stock_return,
            "momentum_valid": bool(np.isfinite(rs63) and rs63 > 0),
            "setup_valid": bool(setup_valid),
            "trigger_active": trigger_active,
            "trigger_reference": float(previous["High"]),
            "setup_name": (
                "Pullback Recovery" if setup_valid and trigger_active
                else "Pullback - Wait Recovery" if setup_valid
                else "No Valid Daily Pullback"
            ),
        }

    if mode == "weekly_position":
        rs13, stock_return = _excess_return(frame, ihsg_tf, 13)
        rs26, _ = _excess_return(frame, ihsg_tf, 26)
        high52 = _finite(current["High52_Position"])
        atr = _finite(current["ATR"])
        extension_atr = (close - current["EMA20"]) / atr if atr > 0 else np.inf
        setup_valid = bool(rs13 > 0 and rs26 > 0 and high52 >= 0.80 and extension_atr <= 2.0)
        trigger_reference = _finite(current["Prev_High4"], close)
        trigger_active = bool(close > current["EMA10"])
        trigger_active = trigger_active and close >= trigger_reference
        return {
            "components": {
                "rs_13": rs13,
                "rs_26": rs26,
                "high_52": high52,
                "risk_adjusted_26": _risk_adjusted_return(frame, 26),
            },
            "primary_rs": rs13,
            "stock_return": stock_return,
            "momentum_valid": bool(np.isfinite(rs13) and np.isfinite(rs26) and rs13 > 0 and rs26 > 0),
            "setup_valid": setup_valid,
            "trigger_active": bool(trigger_active),
            "trigger_reference": trigger_reference,
            "setup_name": (
                "Weekly 4-Week Breakout" if setup_valid and trigger_active
                else "Weekly Momentum - Wait Breakout" if setup_valid
                else "No Valid Weekly Momentum"
            ),
        }

    rs12_1, stock_return = _excess_return(frame, ihsg_tf, 11, end_offset=1)
    high52 = _finite(current["High52_Position"])
    momentum12_1 = _period_return(frame, 11, end_offset=1)
    setup_valid = bool(rs12_1 > 0 and momentum12_1 > 0 and high52 >= 0.75)
    return {
        "components": {
            "rs_12_1": rs12_1,
            "high_52": high52,
            "risk_adjusted_6": _risk_adjusted_return(frame, 6),
        },
        "primary_rs": rs12_1,
        "stock_return": stock_return,
        "momentum_valid": bool(np.isfinite(rs12_1) and rs12_1 > 0 and momentum12_1 > 0),
        "setup_valid": setup_valid,
        "trigger_active": True,
        "trigger_reference": close,
        "setup_name": "Monthly Momentum Rebalance",
    }


# Small research variants can replace this hook without copying the complete
# V4 data-loading, reporting, and candidate-construction implementation.
TIMEFRAME_METRICS = _timeframe_metrics


def analisa_saham_confluence(
    ticker_code: str,
    df_saham: pd.DataFrame | None,
    ihsg_tf: pd.DataFrame,
    mode_tren: str,
    min_turnover: float,
    min_price: float,
    *,
    prepared_tf: pd.DataFrame | None = None,
    liquidity: tuple[float, float] | None = None,
) -> dict:
    """Build a compatible candidate while replacing V1 eligibility and setup."""
    try:
        mode = normalize_timeframe(mode_tren)
        frame = prepared_tf
        if frame is None:
            if df_saham is None:
                raise ValueError("Data saham diperlukan bila indikator belum disiapkan")
            frame = siapkan_data_untuk_timeframe(df_saham, mode)
        if liquidity is None:
            if df_saham is None:
                raise ValueError("Data saham diperlukan bila likuiditas belum disiapkan")
            liquidity = _BASE.hitung_daily_liquidity(df_saham)

        candidate = _BASE.analisa_saham_confluence(
            ticker_code,
            None,
            ihsg_tf,
            mode,
            min_turnover,
            min_price,
            prepared_tf=frame,
            liquidity=liquidity,
        )
        if candidate.get("error"):
            return candidate

        current = frame.iloc[-1]
        close = float(current["Close"])
        atr = _finite(current["ATR"], close * 0.02)
        atr_pct = _finite(current["ATR_Pct"])
        cfg = get_timeframe_config(mode)
        turnover20, turnover60 = liquidity
        metrics = TIMEFRAME_METRICS(frame, ihsg_tf, mode, turnover20)

        if mode == "daily_swing":
            trend_valid = close > current["EMA50"] > current["EMA200"]
        elif mode == "weekly_position":
            trend_valid = close > current["EMA20"] > current["EMA50"]
        else:
            trend_valid = (
                close > current["Daily_SMA200"]
                and current["Daily_SMA200"] > current["Daily_SMA200_21Ago"]
            )

        liquidity_valid = bool(np.isfinite(turnover20) and turnover20 >= min_turnover)
        price_valid = bool(close >= min_price)
        volatility_valid = bool(cfg["min_atr_pct"] <= atr_pct <= cfg["max_atr_pct"])
        hard_pass = bool(trend_valid and liquidity_valid and price_valid and volatility_valid)

        fail_reasons = []
        if not trend_valid:
            fail_reasons.append("struktur tren V4")
        if not liquidity_valid:
            fail_reasons.append("likuiditas rupiah")
        if not price_valid:
            fail_reasons.append("harga minimum")
        if not volatility_valid:
            fail_reasons.append("ATR%")

        tick = fraksi_harga_idx(close)
        active = bool(metrics["trigger_active"])
        trigger = round_idx_price(
            close if active else max(close, metrics["trigger_reference"] + tick),
            "up",
        )
        entry = round_idx_price(close if active else trigger, "up")
        support = _finite(current.get("Support"), close - atr)
        structure_stop = min(_finite(current["EMA20"]), support) - 0.25 * atr
        atr_stop = entry - cfg["stop_atr"] * atr
        stop = round_idx_price(max(structure_stop, atr_stop), "down")
        if stop >= entry:
            stop = round_idx_price(entry - cfg["stop_atr"] * atr, "down")
        risk_per_share = max(entry - stop, tick)
        target = round_idx_price(entry + cfg["target_rr"] * risk_per_share, "up")

        conditions = {
            "trend": bool(trend_valid),
            "relative_strength": bool(metrics["momentum_valid"]),
            "momentum": bool(metrics["momentum_valid"]),
            "macd": False,
            "volume": liquidity_valid,
            "setup": bool(metrics["setup_valid"]),
            "volatility": volatility_valid,
        }
        candidate.update({
            "entry_level": entry,
            "planned_entry": entry,
            "entry_type": "active" if active else "planned",
            "trigger_price": trigger,
            "stop_level": stop,
            "target_price": target,
            "risk_per_share": risk_per_share,
            "risk_reward": (target - entry) / risk_per_share,
            "turnover20": turnover20,
            "turnover60": turnover60,
            "rs_excess": metrics["primary_rs"],
            "stock_return": metrics["stock_return"],
            "rs_trend_up": bool(metrics["momentum_valid"]),
            "setup_name": metrics["setup_name"],
            "breakout_ok": active,
            "pullback_ok": bool(mode == "daily_swing" and metrics["setup_valid"]),
            "hard_pass": hard_pass,
            "hard_fail_reasons": fail_reasons,
            "conditions": conditions,
            "v4_components": metrics["components"],
            "v4_setup_valid": bool(metrics["setup_valid"]),
            "v4_trigger_active": active,
            "quality_score": 0.0,
            "status": "Pending",
        })
        return candidate
    except Exception as exc:
        return {"ticker": ticker_code, "error": True, "alasan": str(exc)}


def _percentile(values: list[float], value: float) -> float:
    valid = np.asarray([item for item in values if np.isfinite(item)], dtype=float)
    if not np.isfinite(value) or not len(valid):
        return 0.0
    return float(np.mean(valid <= value) * 100.0)


def finalisasi_score_dan_status(
    candidates: list[dict],
    mode_tren: str,
    market_regime: dict,
) -> list[dict]:
    """Rank valid candidates cross-sectionally, then assign trigger status."""
    mode = normalize_timeframe(mode_tren)
    weights = RANKING_MODELS[mode]
    market_ok = bool(market_regime.get("market_trend_ok", False))
    rankable = [
        candidate for candidate in candidates
        if candidate.get("hard_pass") and candidate.get("v4_setup_valid") and market_ok
    ]
    component_values = {
        name: [candidate["v4_components"].get(name, np.nan) for candidate in rankable]
        for name in weights
    }

    for candidate in candidates:
        percentiles = {
            name: _percentile(component_values[name], candidate["v4_components"].get(name, np.nan))
            for name in weights
        }
        candidate["rank_percentiles"] = percentiles
        candidate["quality_score"] = float(sum(weights[name] * percentiles[name] for name in weights))
        primary_name = next(iter(weights))
        candidate["rs_percentile"] = percentiles[primary_name]

        if not market_ok:
            candidate["status"] = STATUS_SKIP_TREND
        elif not candidate.get("hard_pass"):
            candidate["status"] = tentukan_status_skip(candidate.get("hard_fail_reasons", []))
        elif not candidate.get("v4_setup_valid"):
            candidate["status"] = STATUS_SKIP_SETUP
        elif candidate.get("v4_trigger_active"):
            candidate["status"] = STATUS_READY
        else:
            candidate["status"] = STATUS_WAIT
        candidate["kondisi_detail"] = ", ".join(
            f"{'✅' if value else '❌'} {name}"
            for name, value in candidate["conditions"].items()
        )

    ranked = sorted(
        rankable,
        key=lambda item: (
            item["quality_score"], item["rs_percentile"], item.get("turnover20", 0),
        ),
        reverse=True,
    )
    for rank, candidate in enumerate(ranked, start=1):
        candidate["universe_rank"] = rank
    return candidates


def ranking_candidates(candidates: list[dict], limit: int = 3):
    valid = [candidate for candidate in candidates if candidate.get("status") in {STATUS_READY, STATUS_WAIT}]
    ranked = sorted(
        valid,
        key=lambda item: (
            item.get("quality_score", 0),
            item.get("rs_percentile", 0),
            item.get("turnover20", 0),
        ),
        reverse=True,
    )
    selected = ranked[:limit]
    return selected, selected[0]["status"] if selected else STATUS_WAIT


def _screen_timeframe(args: argparse.Namespace, pool: list[str], stocks: dict, ihsg: pd.DataFrame, mode: str):
    breadth = hitung_market_breadth_from_frames(stocks, pool)
    ihsg_tf = siapkan_data_untuk_timeframe(ihsg, mode)
    regime = analisa_market_regime(ihsg, mode, breadth)

    def analyze(ticker: str):
        frame = stocks.get(ticker)
        if frame is None or frame.empty:
            return None
        result = analisa_saham_confluence(
            ticker, frame, ihsg_tf, mode, args.min_turnover, args.min_price,
        )
        return None if result.get("error") else result

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        candidates = [item for item in executor.map(analyze, pool) if item is not None]
    finalisasi_score_dan_status(candidates, mode, regime)
    return candidates, regime


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V4 timeframe-specific IDX research screener")
    parser.add_argument(
        "--timeframe", "--trend", dest="timeframe",
        choices=["daily", "weekly", "monthly", *CANONICAL_TIMEFRAMES, "all"],
        default="daily",
    )
    parser.add_argument("--ticker", default="", help="Optional ticker; ranking still uses the full universe")
    parser.add_argument("--excel", default="resource/daftar-saham.xlsx")
    parser.add_argument("--period", choices=["10y", "max"], default="10y")
    parser.add_argument("--min-turnover", type=float, default=1_000_000_000)
    parser.add_argument("--min-price", type=float, default=100)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output-dir", default="output/screener_v4")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.top < 1 or args.batch_size < 1 or args.workers < 1:
        raise ValueError("top, batch-size, dan workers harus positif")
    if args.timeframe == "all" and not args.ticker:
        raise ValueError("--timeframe all hanya untuk pemeriksaan satu ticker")

    pool = ambil_semua_ticker_dari_excel(args.excel)
    ihsg = download_ihsg(args.period)
    stocks = download_saham_batch(pool, args.period, args.batch_size)
    pool = [ticker for ticker in pool if ticker in stocks]
    if ihsg.empty or not pool:
        raise RuntimeError("Data pasar tidak tersedia")

    requested = normalisasi_ticker(args.ticker) if args.ticker else ""
    modes = CANONICAL_TIMEFRAMES if args.timeframe == "all" else [normalize_timeframe(args.timeframe)]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for mode in modes:
        candidates, regime = _screen_timeframe(args, pool, stocks, ihsg, mode)
        if requested:
            shown = [candidate for candidate in candidates if candidate["ticker"] == requested]
        else:
            shown, _ = ranking_candidates(candidates, args.top)
        simpan_csv(candidates, str(output_dir / f"{mode}.csv"), mode)
        if not shown:
            print(f"{mode}: tidak ada kandidat valid")
            continue
        print("\n" + deterministic_report(shown, mode, regime))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
