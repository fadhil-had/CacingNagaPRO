"""Final IDX screener for daily, weekly, and monthly watchlists.

The shared module owns data loading and generic candidate construction. This
public entrypoint freezes the rules that passed the latest internal historical
checks. Daily is deliberately a D+10 watchlist, not an automatic trade system.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


_RANKING_PATH = Path(__file__).with_name("screener_ranking.py")
_SPEC = importlib.util.spec_from_file_location("screener_ranking_base", _RANKING_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load ranking core: {_RANKING_PATH}")
_RANKING = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RANKING
_SPEC.loader.exec_module(_RANKING)

for _name in dir(_RANKING):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_RANKING, _name)


VARIANT_ID = "FINAL_DAILY_D10_WEEKLY_MONTHLY"
STATUS_DAILY_WATCHLIST = "Watchlist D+10"
DAILY_SCORE_MIN = 80.0
DAILY_PRICE_MIN = 200.0
DAILY_AVG_TURNOVER20_MIN = 10_000_000_000.0
DAILY_MEDIAN_TURNOVER20_MIN = 5_000_000_000.0
DAILY_ATR_PCT_MIN = 0.015
DAILY_ATR_PCT_MAX = 0.040
DAILY_TAKE_PROFIT_OPTIONS = (0.03, 0.05, 0.10)
DAILY_STOP_LOSS_OPTIONS = (0.02, 0.05)

RANKING_MODELS = {
    "daily_swing": {
        "ret60": 1 / 3,
        "atr_pct": 1 / 3,
        "near_sma20": 1 / 3,
    },
    "weekly_position": {
        "rs_13": 0.20,
        "rs_26": 0.40,
        "high_52": 0.20,
        "risk_adjusted_26": 0.20,
    },
    "monthly_long_term": dict(_RANKING.RANKING_MODELS["monthly_long_term"]),
}
BACKTEST_HOLD_GRID = {
    "daily_swing": [(10, 10)],
    "weekly_position": [(8, 40)],
    "monthly_long_term": [(6, 126)],
}

_BASE_TIMEFRAME_METRICS = _RANKING._timeframe_metrics
_BASE_ANALYZE = _RANKING.analisa_saham_confluence
_BASE_FINALIZE = _RANKING.finalisasi_score_dan_status
_BASE_MARKET_REGIME = _RANKING.analisa_market_regime
_BASE_RANKING_CANDIDATES = _RANKING.ranking_candidates
_BASE_REPORT = _RANKING.deterministic_report
_BASE_MAIN = _RANKING.main


def _daily_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    turnover20: float,
) -> dict:
    """Frozen D+10 ranking factors; benchmark is diagnostic only."""
    del benchmark, turnover20
    close = pd.to_numeric(frame["Close"], errors="coerce")
    high = pd.to_numeric(frame["High"], errors="coerce")
    low = pd.to_numeric(frame["Low"], errors="coerce")
    prior_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prior_close).abs(), (low - prior_close).abs()], axis=1,
    ).max(axis=1)
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    ret60 = close.pct_change(60)
    atr_pct = true_range.rolling(14).mean() / close
    near_sma20 = -(close / sma20 - 1).abs()
    current_close = _RANKING._finite(close.iloc[-1])
    values = {
        "ret60": _RANKING._finite(ret60.iloc[-1]),
        "atr_pct": _RANKING._finite(atr_pct.iloc[-1]),
        "near_sma20": _RANKING._finite(near_sma20.iloc[-1]),
    }
    data_valid = bool(all(np.isfinite(value) for value in values.values()))
    trend_valid = bool(
        data_valid
        and current_close > _RANKING._finite(sma50.iloc[-1])
        and _RANKING._finite(sma50.iloc[-1])
        > _RANKING._finite(sma50.shift(5).iloc[-1])
    )
    return {
        "components": values,
        "primary_rs": values["ret60"],
        "stock_return": values["ret60"],
        "momentum_valid": trend_valid,
        "setup_valid": data_valid,
        "trigger_active": False,
        "trigger_reference": current_close,
        "setup_name": "Daily D+10 Cross-sectional Watchlist",
        "trend_valid": trend_valid,
    }


def _weekly_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    turnover20: float,
) -> dict:
    base = _BASE_TIMEFRAME_METRICS(frame, benchmark, "weekly_position", turnover20)
    current, previous = frame.iloc[-1], frame.iloc[-2]
    close = float(current["Close"])
    atr = _RANKING._finite(current["ATR"])
    extension_atr = (
        (close - _RANKING._finite(current["EMA20"])) / atr if atr > 0 else np.inf
    )
    rsi = _RANKING._finite(current["RSI"])
    setup_valid = bool(
        base["momentum_valid"]
        and base["components"]["high_52"] >= 0.80
        and extension_atr <= 2.0
        and 50 <= rsi <= 72
    )
    active = bool(
        setup_valid
        and close > _RANKING._finite(current["EMA10"], close)
        and close > _RANKING._finite(previous["Close"], close)
    )
    return {
        **base,
        "setup_valid": setup_valid,
        "trigger_active": active,
        "trigger_reference": (
            close if active else _RANKING._finite(current["Prev_High4"], close)
        ),
        "setup_name": (
            "Weekly Momentum Rebalance" if active
            else "Weekly Momentum - Wait Recovery" if setup_valid
            else "No Valid Weekly Momentum"
        ),
    }


def _timeframe_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    mode: str,
    turnover20: float,
) -> dict:
    if mode == "daily_swing":
        return _daily_metrics(frame, benchmark, turnover20)
    if mode == "weekly_position":
        return _weekly_metrics(frame, benchmark, turnover20)
    if mode == "monthly_long_term":
        return _BASE_TIMEFRAME_METRICS(frame, benchmark, mode, turnover20)
    raise ValueError(f"Timeframe tidak didukung: {mode}")


def _daily_prior_turnover(frame: pd.DataFrame) -> tuple[float, float]:
    turnover = pd.to_numeric(frame["Close"], errors="coerce") * pd.to_numeric(
        frame["Volume"], errors="coerce",
    )
    prior20 = turnover.shift(1).tail(20).dropna()
    if len(prior20) < 20:
        return np.nan, np.nan
    return float(prior20.mean()), float(prior20.median())


def _daily_price_options(
    reference_price: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return user-selectable TP/SL prices, rounded to the IDX tick size."""
    take_profit = tuple(
        (percent, round_idx_price(reference_price * (1 + percent), "up"))
        for percent in DAILY_TAKE_PROFIT_OPTIONS
    )
    stop_loss = tuple(
        (percent, round_idx_price(reference_price * (1 - percent), "down"))
        for percent in DAILY_STOP_LOSS_OPTIONS
    )
    return take_profit, stop_loss


def _format_daily_price_options(
    options: tuple[tuple[float, float], ...],
    sign: str,
) -> str:
    return "<br>".join(
        f"{sign}{percent:.0%}: {format_rupiah(price)}"
        for percent, price in options
    )


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
    """Apply frozen daily eligibility without changing weekly/monthly."""
    mode = normalize_timeframe(mode_tren)
    if mode != "daily_swing":
        return _BASE_ANALYZE(
            ticker_code,
            df_saham,
            ihsg_tf,
            mode,
            min_turnover,
            min_price,
            prepared_tf=prepared_tf,
            liquidity=liquidity,
        )

    try:
        frame = prepared_tf
        if frame is None:
            if df_saham is None:
                raise ValueError("Data saham diperlukan bila indikator belum disiapkan")
            frame = siapkan_data_untuk_timeframe(df_saham, mode)
        fallback_liquidity = liquidity or _daily_prior_turnover(frame)
        candidate = _BASE_ANALYZE(
            ticker_code,
            None,
            ihsg_tf,
            mode,
            min_turnover,
            min_price,
            prepared_tf=frame,
            liquidity=fallback_liquidity,
        )
        if candidate.get("error"):
            return candidate

        metrics = _daily_metrics(frame, ihsg_tf, np.nan)
        close = _RANKING._finite(frame.iloc[-1]["Close"])
        support = _RANKING._finite(frame.iloc[-1].get("Support"), close)
        resistance = _RANKING._finite(frame.iloc[-1].get("Resistance"), close)
        take_profit_prices, stop_loss_prices = _daily_price_options(close)
        avg_turnover20, median_turnover20 = _daily_prior_turnover(frame)
        atr_pct = metrics["components"]["atr_pct"]
        # Daily thresholds are frozen. CLI thresholds remain configurable for
        # weekly/monthly but must not silently change the validated D+10 model.
        price_floor = DAILY_PRICE_MIN
        avg_turnover_floor = DAILY_AVG_TURNOVER20_MIN
        price_valid = bool(close >= price_floor)
        average_liquidity_valid = bool(avg_turnover20 >= avg_turnover_floor)
        median_liquidity_valid = bool(
            median_turnover20 >= DAILY_MEDIAN_TURNOVER20_MIN
        )
        volatility_valid = bool(DAILY_ATR_PCT_MIN <= atr_pct <= DAILY_ATR_PCT_MAX)
        data_valid = bool(metrics["setup_valid"])
        u0_pass = bool(
            data_valid
            and price_valid
            and average_liquidity_valid
            and median_liquidity_valid
            and volatility_valid
        )
        trend_valid = bool(metrics["trend_valid"])
        hard_pass = bool(u0_pass and trend_valid)

        fail_reasons = []
        if not data_valid:
            fail_reasons.append("data indikator minimum")
        if not price_valid:
            fail_reasons.append("harga minimum")
        if not average_liquidity_valid:
            fail_reasons.append("rata-rata turnover 20D")
        if not median_liquidity_valid:
            fail_reasons.append("median turnover 20D")
        if not volatility_valid:
            fail_reasons.append("ATR%")
        if u0_pass and not trend_valid:
            fail_reasons.append("SMA50 belum naik")

        conditions = {
            "price": price_valid,
            "average_liquidity": average_liquidity_valid,
            "median_liquidity": median_liquidity_valid,
            "volatility": volatility_valid,
            "rising_sma50": trend_valid,
        }
        candidate.update({
            "timeframe": mode,
            "recommendation_type": "WATCHLIST_ONLY",
            "holding_horizon": "maksimal 10 sesi",
            "confidence": "setara dalam Top 3",
            "take_profit_options": DAILY_TAKE_PROFIT_OPTIONS,
            "stop_loss_options": DAILY_STOP_LOSS_OPTIONS,
            "take_profit_price_options": take_profit_prices,
            "stop_loss_price_options": stop_loss_prices,
            "support_level": support,
            "resistance_level": resistance,
            "entry_level": np.nan,
            "planned_entry": np.nan,
            "entry_type": "not_validated",
            "trigger_price": np.nan,
            "stop_level": np.nan,
            "target_price": np.nan,
            "risk_per_share": np.nan,
            "risk_reward": np.nan,
            "avg_turnover20_prev": avg_turnover20,
            "median_turnover20_prev": median_turnover20,
            "turnover20": median_turnover20,
            "min_turnover": avg_turnover_floor,
            "min_price": price_floor,
            "rs_excess": metrics["primary_rs"],
            "stock_return": metrics["stock_return"],
            "rs_trend_up": trend_valid,
            "atr_pct": atr_pct,
            "setup_name": metrics["setup_name"],
            "breakout_ok": False,
            "pullback_ok": False,
            "daily_u0_pass": u0_pass,
            "hard_pass": hard_pass,
            "hard_fail_reasons": fail_reasons,
            "conditions": conditions,
            "v4_components": metrics["components"],
            "v4_setup_valid": data_valid,
            "v4_trigger_active": False,
            "quality_score": 0.0,
            "status": "Pending",
        })
        return candidate
    except Exception as exc:
        return {"ticker": ticker_code, "error": True, "alasan": str(exc)}


def _average_percentile(values: list[float], value: float) -> float:
    valid = np.asarray([item for item in values if np.isfinite(item)], dtype=float)
    if not np.isfinite(value) or not len(valid):
        return 0.0
    lower = float(np.sum(valid < value))
    equal = float(np.sum(valid == value))
    return (lower + (equal + 1.0) / 2.0) / len(valid) * 100.0


def finalisasi_score_dan_status(
    candidates: list[dict],
    mode_tren: str,
    market_regime: dict,
) -> list[dict]:
    mode = normalize_timeframe(mode_tren)
    if mode != "daily_swing":
        # IHSG is context for position risk, not a veto on an individual stock.
        # A bearish index can still contain valid relative-strength leaders.
        # Keep the original regime untouched for CSV/report diagnostics, while
        # preventing the shared finalizer from turning the whole universe into
        # ``Skip - Trend`` solely because of the market-wide condition.
        ranking_regime = {**market_regime, "market_trend_ok": True}
        return _BASE_FINALIZE(candidates, mode, ranking_regime)

    # Historical validation ranks U0 before applying the rising-SMA50 gate.
    u0 = [candidate for candidate in candidates if candidate.get("daily_u0_pass")]
    values = {
        name: [
            candidate.get("v4_components", {}).get(name, np.nan)
            for candidate in u0
        ]
        for name in RANKING_MODELS[mode]
    }
    for candidate in candidates:
        components = candidate.get("v4_components", {})
        percentiles = {
            name: _average_percentile(values[name], components.get(name, np.nan))
            for name in RANKING_MODELS[mode]
        }
        score = float(np.mean(list(percentiles.values())))
        candidate["rank_percentiles"] = percentiles
        candidate["quality_score"] = score
        candidate["rs_percentile"] = percentiles["ret60"]
        candidate["selected_top3"] = False

        if not candidate.get("daily_u0_pass"):
            candidate["status"] = tentukan_status_skip(
                candidate.get("hard_fail_reasons", []),
            )
        elif not candidate.get("hard_pass"):
            candidate["status"] = STATUS_SKIP_TREND
        elif score < DAILY_SCORE_MIN:
            candidate["status"] = STATUS_SKIP_SETUP
        else:
            candidate["status"] = STATUS_DAILY_WATCHLIST
        candidate["kondisi_detail"] = ", ".join(
            f"{'✅' if value else '❌'} {name}"
            for name, value in candidate.get("conditions", {}).items()
        )

    eligible = sorted(
        (
            candidate
            for candidate in candidates
            if candidate["status"] == STATUS_DAILY_WATCHLIST
        ),
        key=lambda item: (-item["quality_score"], item["ticker"]),
    )
    for rank, candidate in enumerate(eligible, start=1):
        candidate["universe_rank"] = rank
    return candidates


def analisa_market_regime(
    ihsg_daily: pd.DataFrame,
    mode_tren: str,
    breadth: dict,
) -> dict:
    regime = _BASE_MARKET_REGIME(ihsg_daily, mode_tren, breadth)
    mode = normalize_timeframe(mode_tren)
    if mode in {"daily_swing", "weekly_position", "monthly_long_term"}:
        regime["filter_role"] = "diagnostic_only"
    return regime


def ranking_candidates(candidates: list[dict], limit: int = 3):
    mode = next((item.get("timeframe") for item in candidates), "")
    if mode == "daily_swing":
        ranked = sorted(
            (
                item
                for item in candidates
                if item.get("status") == STATUS_DAILY_WATCHLIST
            ),
            key=lambda item: (-item.get("quality_score", 0.0), item["ticker"]),
        )[:limit]
        for item in ranked:
            item["selected_top3"] = True
        # Alphabetical display avoids implying that Rank 1 was better calibrated.
        return sorted(ranked, key=lambda item: item["ticker"]), STATUS_DAILY_WATCHLIST

    if mode == "weekly_position":
        # Prefer actionable entries, but retain a ranked watchlist when no
        # weekly breakout is active yet.
        ready = [item for item in candidates if item.get("status") == STATUS_READY]
        eligible = ready or [
            item for item in candidates if item.get("status") == STATUS_WAIT
        ]
    else:
        eligible = candidates
    return _BASE_RANKING_CANDIDATES(eligible, limit)


def deterministic_report(
    top_picks: list[dict],
    mode_tren: str,
    market_regime: dict,
) -> str:
    mode = normalize_timeframe(mode_tren)
    if mode != "daily_swing":
        return _BASE_REPORT(top_picks, mode, market_regime)

    lines = [
        "### IDX Daily Swing Watchlist — maksimal D+10",
        "",
        (
            f"**Kondisi IHSG (diagnostik, bukan filter):** "
            f"{market_regime.get('regime', '-')} | "
            f"IHSG {format_rupiah(market_regime.get('close', np.nan))} | "
            f"Breadth > EMA50 {market_regime.get('breadth50', np.nan):.1%}"
        ),
        "",
        "| Ticker | Score | Close | Ret60 Pctl | ATR Pctl | Dekat SMA20 Pctl | "
        "TP dari Close | SL dari Close | Support 20D | Resistance 20D | Horizon |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | :--- | :--- | ---: | ---: | :--- |",
    ]
    for candidate in top_picks:
        ranks = candidate.get("rank_percentiles", {})
        take_profit_prices = candidate.get("take_profit_price_options")
        stop_loss_prices = candidate.get("stop_loss_price_options")
        if not take_profit_prices or not stop_loss_prices:
            take_profit_prices, stop_loss_prices = _daily_price_options(
                _RANKING._finite(candidate.get("harga_terakhir")),
            )
        lines.append(
            f"| {candidate['ticker'].replace('.JK', '')} | "
            f"{candidate['quality_score']:.1f}/100 | "
            f"{format_rupiah(candidate['harga_terakhir'])} | "
            f"{ranks.get('ret60', 0):.0f}% | "
            f"{ranks.get('atr_pct', 0):.0f}% | "
            f"{ranks.get('near_sma20', 0):.0f}% | "
            f"{_format_daily_price_options(take_profit_prices, '+')} | "
            f"{_format_daily_price_options(stop_loss_prices, '-')} | "
            f"{format_rupiah(candidate.get('support_level', np.nan))} | "
            f"{format_rupiah(candidate.get('resistance_level', np.nan))} | "
            "maksimal 10 sesi |"
        )
    lines.extend([
        "",
        "*Ketiga saham adalah satu set Top 3 dengan tingkat keyakinan setara; "
        "urutan tabel bukan ranking keyakinan.*",
        "*TP dan SL dihitung dari harga penutupan sebagai referensi, lalu dibulatkan "
        "ke fraksi harga BEI. User menentukan sendiri kombinasi yang dipakai; "
        "ini bukan instruksi atau level eksekusi tervalidasi.*",
        "*Support dan resistance adalah low/high rolling 20 sesi, bukan kepastian "
        "harga akan berbalik atau menembus level tersebut.*",
        "*Backtest tidak menjamin hasil berikutnya.*",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = normalize_timeframe(args.timeframe)
    if mode not in BACKTEST_HOLD_GRID:
        raise ValueError("Timeframe tidak didukung")
    if not 1 <= args.top <= 3:
        raise ValueError("--top harus antara 1 dan 3")
    return _BASE_MAIN(argv)


# Shared functions resolve these collaborators in their own module.
_RANKING.TIMEFRAME_METRICS = _timeframe_metrics
_RANKING.RANKING_MODELS = RANKING_MODELS
_RANKING.VARIANT_ID = VARIANT_ID
_RANKING.analisa_saham_confluence = analisa_saham_confluence
_RANKING.finalisasi_score_dan_status = finalisasi_score_dan_status
_RANKING.analisa_market_regime = analisa_market_regime
_RANKING.ranking_candidates = ranking_candidates
_RANKING.deterministic_report = deterministic_report


if __name__ == "__main__":
    raise SystemExit(main())
