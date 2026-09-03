import sys
import math
import argparse
import json
import hashlib
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent if HERE.parent.name == "tests" else HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import financial_screener as fs


BACKTEST_CONFIG = {
    # V1 time-stop (hari bursa, data daily): 10 hari / 8 minggu (=40) / 6 bulan (=126).
    "daily_swing": {"entry_window": 3, "max_hold": 10},
    "weekly_position": {"entry_window": 5, "max_hold": 40},
    "monthly_long_term": {"entry_window": 10, "max_hold": 126},
}

# Alias legacy -> kanonis (CLI lama tetap jalan, pipeline selalu kanonis).
BACKTEST_ALIASES = {
    "1hari": "daily_swing",
    "1minggu": "weekly_position",
    "1bulan": "monthly_long_term",
}


def normalize_backtest_mode(mode: str) -> str:
    m = str(mode or "").strip()
    if m in BACKTEST_CONFIG:
        return m
    if m in BACKTEST_ALIASES:
        return BACKTEST_ALIASES[m]
    try:
        return fs.normalize_timeframe(m)
    except Exception:
        return m


def get_backtest_cfg(mode: str) -> dict:
    canon = normalize_backtest_mode(mode)
    if canon in BACKTEST_CONFIG:
        return BACKTEST_CONFIG[canon]
    # Fallback tunggal ke TIMEFRAME_CONFIG screener (entry_window/max_hold_days V1).
    tf = fs.get_timeframe_config(canon)
    return {
        "entry_window": tf.get("entry_window"),
        "max_hold": tf.get("max_hold_days", tf.get("max_holding_bars")),
    }

# Centralized error codes for consistency with plan
ERROR_CODES = {
    "ERR_INPUT": "Invalid input combination",
    "ERR_TICKER_NOT_FOUND": "Ticker not found in universe or no data",
    "ERR_MARKET_CONTEXT": "Failed to calculate market context/regime",
    "ERR_INSUFFICIENT_DATA": "Insufficient data for analysis",
}


def to_date(value):
    return pd.Timestamp(value).normalize() if value else None


def normalize_df(df):
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    out = out.sort_index()
    return out[~out.index.duplicated(keep="last")]


def extract_all_frames(data_massal, pool):
    result = {}
    for ticker in pool:
        df = fs.ekstrak_ticker_frame(data_massal, ticker)
        if not df.empty:
            result[ticker] = normalize_df(df)
    return result


def get_signal_dates(ihsg_daily, mode_tren, start, end, step):
    ihsg_daily = normalize_df(ihsg_daily).dropna(subset=["Close"])
    daily_index = pd.DatetimeIndex(ihsg_daily.index)
    mode_tren = normalize_backtest_mode(mode_tren)

    if mode_tren == "daily_swing":
        min_rows = fs.get_timeframe_config(mode_tren)["min_rows"]
        dates = list(daily_index[max(0, min_rows - 1):])
    else:
        tf = fs.siapkan_data_untuk_timeframe(ihsg_daily, mode_tren)
        labels = tf.index[fs.get_timeframe_config(mode_tren)["min_rows"] - 1:]
        dates = []
        for label in labels:
            pos = daily_index.searchsorted(pd.Timestamp(label), side="right") - 1
            if pos >= 0:
                d = pd.Timestamp(daily_index[pos]).normalize()
                if not dates or d != dates[-1]:
                    dates.append(d)

    start = to_date(start)
    end = to_date(end)

    if start is not None:
        dates = [d for d in dates if d >= start]
    if end is not None:
        dates = [d for d in dates if d <= end]

    return dates[::max(1, step)]


def breadth_at_date(frames, cutoff):
    above50 = 0
    above200 = 0
    valid50 = 0
    valid200 = 0
    returns20 = []

    for df in frames.values():
        hist = df.loc[:cutoff].dropna(subset=["Close"])
        if len(hist) < 60:
            continue

        close = hist["Close"]
        ema50 = close.ewm(span=50, adjust=False).mean()
        valid50 += 1
        if close.iloc[-1] > ema50.iloc[-1]:
            above50 += 1

        if len(close) >= 220:
            ema200 = close.ewm(span=200, adjust=False).mean()
            valid200 += 1
            if close.iloc[-1] > ema200.iloc[-1]:
                above200 += 1

        if len(close) >= 21 and close.iloc[-21] > 0:
            returns20.append(close.iloc[-1] / close.iloc[-21] - 1)

    return {
        "breadth50": above50 / valid50 if valid50 else np.nan,
        "breadth200": above200 / valid200 if valid200 else np.nan,
        "median_return20": float(np.median(returns20)) if returns20 else np.nan,
        "jumlah_saham_breadth": valid50,
    }


def apply_entry_slippage(price, bps):
    return float(price) * (1 + bps / 10000)


def apply_exit_slippage(price, bps):
    return float(price) * (1 - bps / 10000)


def calc_r(entry, exit_price, stop, buy_fee_pct, sell_fee_pct):
    risk = entry - stop
    if risk <= 0:
        return np.nan, np.nan

    gross_r = (exit_price - entry) / risk
    entry_cost = entry * (1 + buy_fee_pct / 100)
    exit_value = exit_price * (1 - sell_fee_pct / 100)
    net_r = (exit_value - entry_cost) / risk
    return float(gross_r), float(net_r)


def simulate_trade(
    daily,
    signal_date,
    trigger,
    stop,
    target,
    entry_window,
    max_hold,
    same_bar_policy,
    buy_fee,
    sell_fee,
    slippage_bps,
):
    daily = normalize_df(daily).dropna(subset=["Open", "High", "Low", "Close"])
    future = daily[daily.index.normalize() > pd.Timestamp(signal_date).normalize()]

    if future.empty:
        return {
            "triggered": False,
            "outcome": "NO_FUTURE_DATA",
            "resolved_date": str(pd.Timestamp(signal_date).date()),
        }

    entry_scan = future.iloc[:entry_window]

    for dt, bar in entry_scan.iterrows():
        o = fs.safe_float(bar["Open"])
        h = fs.safe_float(bar["High"])
        l = fs.safe_float(bar["Low"])

        if not all(np.isfinite([o, h, l])):
            continue

        if o <= stop:
            return {
                "triggered": False,
                "outcome": "CANCELLED_INVALIDATION",
                "resolved_date": str(pd.Timestamp(dt).date()),
            }

        if o >= target:
            return {
                "triggered": False,
                "outcome": "EXPIRED_GAP_TARGET",
                "resolved_date": str(pd.Timestamp(dt).date()),
            }

        if h < trigger:
            if l <= stop:
                return {
                    "triggered": False,
                    "outcome": "CANCELLED_INVALIDATION",
                    "resolved_date": str(pd.Timestamp(dt).date()),
                }
            continue

        entry = apply_entry_slippage(max(o, trigger), slippage_bps)
        entry_date = pd.Timestamp(dt)

        if entry >= target:
            return {
                "triggered": False,
                "outcome": "EXPIRED_BAD_FILL",
                "resolved_date": str(entry_date.date()),
            }

        hit_stop = l <= stop
        hit_target = h >= target

        if hit_stop and hit_target:
            if same_bar_policy == "target":
                exit_price = apply_exit_slippage(target, slippage_bps)
                outcome = "TARGET_SAME_BAR"
            else:
                exit_price = apply_exit_slippage(stop, slippage_bps)
                outcome = "STOP_SAME_BAR"

            gross_r, net_r = calc_r(entry, exit_price, stop, buy_fee, sell_fee)
            return {
                "triggered": True,
                "outcome": outcome,
                "entry_date": str(entry_date.date()),
                "entry_price": entry,
                "exit_date": str(entry_date.date()),
                "exit_price": exit_price,
                "hold_sessions": 1,
                "gross_r": gross_r,
                "net_r": net_r,
                "resolved_date": str(entry_date.date()),
            }

        if hit_stop:
            exit_price = apply_exit_slippage(stop, slippage_bps)
            gross_r, net_r = calc_r(entry, exit_price, stop, buy_fee, sell_fee)
            return {
                "triggered": True,
                "outcome": "STOP_ENTRY_BAR",
                "entry_date": str(entry_date.date()),
                "entry_price": entry,
                "exit_date": str(entry_date.date()),
                "exit_price": exit_price,
                "hold_sessions": 1,
                "gross_r": gross_r,
                "net_r": net_r,
                "resolved_date": str(entry_date.date()),
            }

        if hit_target:
            exit_price = apply_exit_slippage(target, slippage_bps)
            gross_r, net_r = calc_r(entry, exit_price, stop, buy_fee, sell_fee)
            return {
                "triggered": True,
                "outcome": "TARGET_ENTRY_BAR",
                "entry_date": str(entry_date.date()),
                "entry_price": entry,
                "exit_date": str(entry_date.date()),
                "exit_price": exit_price,
                "hold_sessions": 1,
                "gross_r": gross_r,
                "net_r": net_r,
                "resolved_date": str(entry_date.date()),
            }

        entry_index = daily.index.get_loc(dt)
        holding = daily.iloc[entry_index + 1: entry_index + max_hold]
        exit_price = None
        exit_date = None
        outcome = None
        hold_sessions = 1

        for hold_sessions, (exit_dt, exit_bar) in enumerate(holding.iterrows(), start=2):
            o2 = fs.safe_float(exit_bar["Open"])
            h2 = fs.safe_float(exit_bar["High"])
            l2 = fs.safe_float(exit_bar["Low"])

            if not all(np.isfinite([o2, h2, l2])):
                continue

            if o2 <= stop:
                exit_price = apply_exit_slippage(o2, slippage_bps)
                exit_date = pd.Timestamp(exit_dt)
                outcome = "STOP_GAP"
                break

            if o2 >= target:
                exit_price = apply_exit_slippage(target, slippage_bps)
                exit_date = pd.Timestamp(exit_dt)
                outcome = "TARGET"
                break

            hit_stop = l2 <= stop
            hit_target = h2 >= target

            if hit_stop and hit_target:
                if same_bar_policy == "target":
                    exit_price = apply_exit_slippage(target, slippage_bps)
                    outcome = "TARGET_SAME_BAR"
                else:
                    exit_price = apply_exit_slippage(stop, slippage_bps)
                    outcome = "STOP_SAME_BAR"
                exit_date = pd.Timestamp(exit_dt)
                break

            if hit_stop:
                exit_price = apply_exit_slippage(stop, slippage_bps)
                exit_date = pd.Timestamp(exit_dt)
                outcome = "STOP"
                break

            if hit_target:
                exit_price = apply_exit_slippage(target, slippage_bps)
                exit_date = pd.Timestamp(exit_dt)
                outcome = "TARGET"
                break

        if exit_price is None:
            if holding.empty:
                exit_date = entry_date
                exit_price = fs.safe_float(bar["Close"])
                hold_sessions = 1
            else:
                exit_date = pd.Timestamp(holding.index[-1])
                exit_price = fs.safe_float(holding["Close"].iloc[-1])
                hold_sessions = len(holding) + 1

            exit_price = apply_exit_slippage(exit_price, slippage_bps)
            outcome = "TIME_EXIT"

        gross_r, net_r = calc_r(entry, exit_price, stop, buy_fee, sell_fee)

        return {
            "triggered": True,
            "outcome": outcome,
            "entry_date": str(entry_date.date()),
            "entry_price": entry,
            "exit_date": str(exit_date.date()),
            "exit_price": exit_price,
            "hold_sessions": hold_sessions,
            "gross_r": gross_r,
            "net_r": net_r,
            "resolved_date": str(exit_date.date()),
        }

    resolved = entry_scan.index[-1] if len(entry_scan) else future.index[0]
    return {
        "triggered": False,
        "outcome": "EXPIRED",
        "resolved_date": str(pd.Timestamp(resolved).date()),
    }


def metrics(df):
    base = {
        "signals": len(df),
        "triggered": 0,
        "entry_rate": np.nan,
        "win_rate": np.nan,
        "target_rate": np.nan,
        "expectancy_r": np.nan,
        "avg_win_r": np.nan,
        "avg_loss_r": np.nan,
        "profit_factor": np.nan,
        "max_drawdown_r": np.nan,
        "avg_hold": np.nan,
    }

    if df.empty:
        return base

    trades = df[df["triggered"] == True].copy()
    base["triggered"] = len(trades)
    base["entry_rate"] = len(trades) / len(df)

    if trades.empty:
        return base

    r = pd.to_numeric(trades["net_r"], errors="coerce")
    valid = trades.assign(_r=r).dropna(subset=["_r"])
    if valid.empty:
        return base

    rv = valid["_r"]
    wins = rv[rv > 0]
    losses = rv[rv <= 0]

    ordered = valid.sort_values(["exit_date", "signal_date"])
    equity = ordered["_r"].cumsum()
    drawdown = equity - equity.cummax()

    base.update({
        "win_rate": float((rv > 0).mean()),
        "target_rate": float(valid["outcome"].astype(str).str.startswith("TARGET").mean()),
        "expectancy_r": float(rv.mean()),
        "avg_win_r": float(wins.mean()) if len(wins) else np.nan,
        "avg_loss_r": float(losses.mean()) if len(losses) else np.nan,
        "profit_factor": float(wins.sum() / abs(losses.sum()))
            if len(losses) and losses.sum() != 0 else np.inf,
        "max_drawdown_r": float(drawdown.min()) if len(drawdown) else np.nan,
        "avg_hold": float(pd.to_numeric(valid["hold_sessions"], errors="coerce").mean()),
    })
    return base


def grouped_metrics(df, column):
    rows = []
    if df.empty or column not in df.columns:
        return pd.DataFrame()

    for key, group in df.groupby(column, observed=True, dropna=False):
        rows.append({column: str(key), **metrics(group)})
    return pd.DataFrame(rows)


def add_buckets(df):
    if df.empty:
        return df

    out = df.copy()
    out["setup_base"] = np.where(
        out["setup"].astype(str).str.startswith("Breakout"),
        "Breakout",
        np.where(
            out["setup"].astype(str).str.contains("Pullback"),
            "Pullback",
            "Other",
        ),
    )

    out["score_bucket"] = pd.cut(
        out["quality_score"],
        [-np.inf, 69.99, 74.99, 79.99, 84.99, np.inf],
        labels=["<70", "70-74", "75-79", "80-84", "85+"],
    )

    out["rs_bucket"] = pd.cut(
        out["rs_percentile"],
        [-np.inf, 59.99, 69.99, 79.99, 89.99, np.inf],
        labels=["<60", "60-69", "70-79", "80-89", "90+"],
    )
    return out


def fmt_pct(v):
    return "-" if not np.isfinite(v) else f"{v:.1%}"


def fmt_num(v, digits=2):
    return "-" if not np.isfinite(v) else f"{v:.{digits}f}"


def make_report(mode, df, args):
    m = metrics(df)
    canon = normalize_backtest_mode(mode)
    rep_cfg = get_backtest_cfg(canon)
    return f"""# IDX Backtest — {canon.upper()}

- Signals: **{m['signals']}**
- Triggered trades: **{m['triggered']}**
- Entry rate: **{fmt_pct(m['entry_rate'])}**
- Win rate: **{fmt_pct(m['win_rate'])}**
- Target hit rate: **{fmt_pct(m['target_rate'])}**
- Expectancy: **{fmt_num(m['expectancy_r'])}R/trade**
- Average win: **{fmt_num(m['avg_win_r'])}R**
- Average loss: **{fmt_num(m['avg_loss_r'])}R**
- Profit factor: **{fmt_num(m['profit_factor'])}**
- Trade-sequence max drawdown: **{fmt_num(m['max_drawdown_r'])}R**
- Average hold: **{fmt_num(m['avg_hold'], 1)} sessions**

## Assumptions

- Source of strategy rules: `scripts.financial_screener`
- Top picks per signal date: {args.top}
- Entry window: {rep_cfg['entry_window']} sessions
- Max hold (time-stop): {rep_cfg['max_hold']} sessions (V1: 10 hari / 40 hari / 126 hari)
- Same-bar policy: {args.same_bar_policy}
- Buy fee: {args.buy_fee:.3f}%
- Sell fee: {args.sell_fee:.3f}%
- Slippage: {args.slippage_bps:.1f} bps per side
- Same ticker overlap: {"allowed" if args.allow_overlap_same_ticker else "blocked until resolved"}

## Notes

- Universe berasal dari Excel yang sama dengan screener.
- Data historis Yahoo dari universe hari ini dapat memiliki survivorship bias.
- Max drawdown di atas adalah trade-sequence drawdown, bukan portfolio equity drawdown dengan alokasi modal simultan.
"""


def run_backtest(mode, pool, frames, data_massal, ihsg_daily, args):
    mode = normalize_backtest_mode(mode)
    dates = get_signal_dates(
        ihsg_daily,
        mode,
        args.start,
        args.end,
        args.signal_step,
    )

    cfg = get_backtest_cfg(mode)
    busy_until = {}
    rows = []

    print(f"\n🧪 {mode}: {len(dates)} signal dates")

    for i, cutoff in enumerate(dates, start=1):
        ihsg_slice = ihsg_daily.loc[:cutoff]
        if ihsg_slice.empty:
            continue

        data_slice = data_massal.loc[:cutoff]
        breadth = breadth_at_date(frames, cutoff)

        try:
            ihsg_tf = fs.siapkan_data_untuk_timeframe(ihsg_slice, mode)
            regime = fs.analisa_market_regime(ihsg_slice, mode, breadth)
        except Exception as e:
            print(f"⚠️ {mode} {cutoff}: gagal menghitung market regime - {e}")
            continue

        candidates = []

        for ticker in pool:
            full_df = frames.get(ticker)
            if full_df is None or full_df.empty:
                continue

            stock_slice = full_df.loc[:cutoff]
            if stock_slice.empty:
                continue

            try:
                res = fs.analisa_saham_confluence(
                    ticker,
                    stock_slice,
                    ihsg_tf,
                    mode,
                    args.min_turnover,
                    args.min_price,
                )

                if not res.get("error"):
                    candidates.append(res)
            except Exception as e:
                # Continue processing other stocks if one fails
                continue

        if not candidates:
            continue

        try:
            candidates = fs.finalisasi_score_dan_status(candidates, mode, regime)
        except Exception as e:
            print(f"⚠️ {mode} {cutoff}: gagal finalisasi score - {e}")
            continue

        ready_all = [c for c in candidates if fs.STATUS_ALIAS.get(c.get("status"), c.get("status")) == fs.STATUS_READY]
        if not ready_all:
            continue

        ranked, pick_type = fs.ranking_candidates(candidates, max(len(candidates), args.top))
        if pick_type != fs.STATUS_READY:
            continue

        selected = []
        for c in ranked:
            if fs.STATUS_ALIAS.get(c.get("status"), c.get("status")) != fs.STATUS_READY:
                continue

            if not args.allow_overlap_same_ticker:
                until = busy_until.get(c["ticker"])
                if until is not None and cutoff <= until:
                    continue

            selected.append(c)
            if len(selected) >= args.top:
                break

        for rank, c in enumerate(selected, start=1):
            try:
                sim = simulate_trade(
                    frames[c["ticker"]],
                    cutoff,
                    c["entry_level"],
                    c["stop_level"],
                    c["target_price"],
                    cfg["entry_window"],
                    cfg["max_hold"],
                    args.same_bar_policy,
                    args.buy_fee,
                    args.sell_fee,
                    args.slippage_bps,
                )

                rows.append({
                    "timeframe": mode,
                    "signal_date": str(cutoff.date()),
                    "ticker": c["ticker"],
                    "rank": rank,
                    "regime": regime["regime"],
                    "regime_score": regime["score"],
                    "quality_score": c["quality_score"],
                    "rs_percentile": c["rs_percentile"],
                    "rs_excess": c["rs_excess"],
                    "rsi": c["rsi"],
                    "vol_ratio": c["vol_ratio"],
                    "vol_z": c["vol_z"],
                    "atr_pct": c["atr_pct"],
                    "setup": c["setup_name"],
                    "signal_close": c["harga_terakhir"],
                    "entry_trigger": c["entry_level"],
                    "planned_stop": c["stop_level"],
                    "planned_target": c["target_price"],
                    "planned_rr": c["risk_reward"],
                    "turnover20": c["turnover20"],
                    **sim,
                })

                if not args.allow_overlap_same_ticker:
                    resolved = to_date(sim.get("resolved_date"))
                    if resolved is not None:
                        busy_until[c["ticker"]] = resolved
            except Exception as e:
                print(f"⚠️ {mode} {cutoff} {c['ticker']}: simulasi trade gagal - {e}")
                continue

        if i % max(1, args.progress_every) == 0 or i == len(dates):
            print(
                f"   {i}/{len(dates)} | "
                f"signals={len(rows)} | "
                f"regime={regime['regime']}"
            )

    return pd.DataFrame(rows)


def save_outputs(df, mode, output_dir, args):
    mode = normalize_backtest_mode(mode)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = add_buckets(df)
    df.to_csv(out_dir / f"signals_{mode}.csv", index=False)

    trades = df[df["triggered"] == True].copy() if not df.empty else pd.DataFrame()
    trades.to_csv(out_dir / f"trades_{mode}.csv", index=False)

    grouped_metrics(df, "regime").to_csv(
        out_dir / f"summary_regime_{mode}.csv", index=False
    )
    grouped_metrics(df, "setup_base").to_csv(
        out_dir / f"summary_setup_{mode}.csv", index=False
    )
    grouped_metrics(df, "score_bucket").to_csv(
        out_dir / f"summary_score_{mode}.csv", index=False
    )
    grouped_metrics(df, "rs_bucket").to_csv(
        out_dir / f"summary_rs_{mode}.csv", index=False
    )

    report_path = out_dir / f"BACKTEST_REPORT_{mode}.md"
    report_path.write_text(make_report(mode, df, args), encoding="utf-8")
    
    # Save config snapshot for reproducibility
    config_snapshot = {
        "run_timestamp": datetime.now().isoformat(),
        "mode": mode,
        "params": {
            "start": args.start,
            "end": args.end,
            "period": args.period,
            "top": args.top,
            "signal_step": args.signal_step,
            "min_turnover": args.min_turnover,
            "min_price": args.min_price,
            "buy_fee": args.buy_fee,
            "sell_fee": args.sell_fee,
            "slippage_bps": args.slippage_bps,
            "same_bar_policy": args.same_bar_policy,
            "allow_overlap_same_ticker": args.allow_overlap_same_ticker,
        },
        "timeframe_config": fs.get_timeframe_config(mode),
        "factor_weights": fs.FACTOR_WEIGHTS,
        "backtest_config": get_backtest_cfg(mode),
    }
    
    config_path = out_dir / "config_snapshot.json"
    config_path.write_text(json.dumps(config_snapshot, indent=2, default=str), encoding="utf-8")
    
    return report_path


def main():
    parser = argparse.ArgumentParser(description="Backtest IDX financial screener")
    parser.add_argument(
        "--trend",
        choices=[
            "daily_swing",
            "weekly_position",
            "monthly_long_term",
            "1hari",
            "1minggu",
            "1bulan",
            "all",
        ],
        default="daily_swing",
        help="Timeframe untuk backtest (all = run 3 timeframe sekaligus; alias legacy 1hari/1minggu/1bulan tetap didukung)",
    )
    parser.add_argument("--start", default="2025-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (optional)")
    parser.add_argument("--period", choices=["5y", "10y", "max"], default="10y", help="Data period for download")
    parser.add_argument("--excel", default="resource/daftar-saham.xlsx", help="Path to stock universe Excel file")
    parser.add_argument("--top", type=int, default=3, help="Number of top picks per signal date")
    parser.add_argument("--signal-step", type=int, default=1, help="Process every Nth signal date")
    parser.add_argument("--min-turnover", type=float, default=1_000_000_000, help="Minimum daily turnover in IDR")
    parser.add_argument("--min-price", type=float, default=100, help="Minimum stock price in IDR")
    parser.add_argument("--buy-fee", type=float, default=0.15, help="Buy fee percentage")
    parser.add_argument("--sell-fee", type=float, default=0.25, help="Sell fee percentage")
    parser.add_argument("--slippage-bps", type=float, default=0.0, help="Slippage in basis points per side")
    parser.add_argument(
        "--same-bar-policy",
        choices=["stop", "target"],
        default="stop",
        help="Policy when stop and target hit on same bar",
    )
    parser.add_argument("--allow-overlap-same-ticker", action="store_true", help="Allow same ticker in overlapping trades")
    parser.add_argument("--progress-every", type=int, default=10, help="Show progress every N signal dates")
    parser.add_argument("--output-dir", default="output/backtest", help="Output directory for results")
    args = parser.parse_args()

    modes = (
        ["daily_swing", "weekly_position", "monthly_long_term"]
        if args.trend == "all"
        else [normalize_backtest_mode(args.trend)]
    )

    if "monthly_long_term" in modes and args.period == "5y":
        args.period = "10y"
        print("ℹ️ Timeframe monthly_long_term butuh data >= 72 bar; period dijadikan 10y.")

    try:
        pool = fs.ambil_semua_ticker_dari_excel(args.excel)
        if not pool:
            raise RuntimeError(f"{ERROR_CODES['ERR_INPUT']}: Universe saham kosong")
    except Exception as e:
        raise RuntimeError(f"{ERROR_CODES['ERR_INPUT']}: Gagal membaca universe saham - {e}")

    tickers = list(dict.fromkeys(pool + [fs.IDX_BENCHMARK]))
    print(
        f"📥 Download {args.period}: "
        f"{len(pool)} saham + IHSG..."
    )

    try:
        data_massal = yf.download(
            tickers,
            period=args.period,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            repair=False,
            progress=False,
            threads=True,
        )
    except Exception as e:
        raise RuntimeError(f"{ERROR_CODES['ERR_MARKET_CONTEXT']}: Download Yahoo Finance gagal - {e}")

    if data_massal is None or data_massal.empty:
        raise RuntimeError(f"{ERROR_CODES['ERR_MARKET_CONTEXT']}: Download Yahoo Finance gagal - data kosong")

    try:
        ihsg_daily = normalize_df(
            fs.ekstrak_ticker_frame(data_massal, fs.IDX_BENCHMARK)
        )
        if ihsg_daily.empty:
            raise RuntimeError(f"{ERROR_CODES['ERR_MARKET_CONTEXT']}: Data IHSG (^JKSE) tidak tersedia")
    except Exception as e:
        raise RuntimeError(f"{ERROR_CODES['ERR_MARKET_CONTEXT']}: Gagal menyiapkan data IHSG - {e}")

    frames = extract_all_frames(data_massal, pool)
    print(f"✅ Data tersedia: {len(frames)}/{len(pool)} saham")

    if len(frames) == 0:
        raise RuntimeError(f"{ERROR_CODES['ERR_INSUFFICIENT_DATA']}: Tidak ada data saham yang tersedia")

    all_results = []

    for mode in modes:
        try:
            result = run_backtest(
                mode,
                pool,
                frames,
                data_massal,
                ihsg_daily,
                args,
            )

            report_path = save_outputs(
                result,
                mode,
                args.output_dir,
                args,
            )

            m = metrics(result)
            print(
                f"\n✅ {mode} | "
                f"Signals={m['signals']} | "
                f"Trades={m['triggered']} | "
                f"Win={fmt_pct(m['win_rate'])} | "
                f"EV={fmt_num(m['expectancy_r'])}R | "
                f"PF={fmt_num(m['profit_factor'])}"
            )
            print(f"📄 {report_path}")

            if not result.empty:
                all_results.append(result)
        except Exception as e:
            print(f"❌ Backtest {mode} gagal: {e}")
            continue

    if len(all_results) > 1:
        combined = pd.concat(all_results, ignore_index=True)
        out_dir = Path(args.output_dir)
        combined.to_csv(out_dir / "signals_all.csv", index=False)
        combined[combined["triggered"] == True].to_csv(
            out_dir / "trades_all.csv", index=False
        )
        print(f"📄 Combined results saved to {out_dir}/signals_all.csv and trades_all.csv")


if __name__ == "__main__":
    main()
