"""Backtest V1 — walk-forward point-in-time untuk screener frozen.

Memakai ulang logika `scripts/financial_screener.py` apa adanya (tidak menduplikasi
rumus). Lihat `BACKTEST_PLAN_V1.md` untuk kontrak/metodologi.

Contoh:
    python tests/backtest_screener_v1.py --trend daily_swing \\
        --start 2022-01-01 --end 2024-12-31 --tickers BBCA,BBRI,TLKM --use-cache
"""
import argparse
import hashlib
import json
import math
import os
import pickle
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.financial_screener import (  # noqa: E402
    FACTOR_WEIGHTS,
    LOT_SIZE,
    STATUS_ALIAS,
    STATUS_READY,
    STATUS_WAIT,
    ambil_semua_ticker_dari_excel,
    analisa_market_regime,
    analisa_saham_confluence,
    download_ihsg,
    download_saham_batch,
    finalisasi_score_dan_status,
    get_max_hold_days,
    get_timeframe_config,
    hitung_market_breadth_from_frames,
    normalize_timeframe,
    ranking_candidates,
    siapkan_data_untuk_timeframe,
)

DEFAULT_STEP = {"daily_swing": 5, "weekly_position": 20, "monthly_long_term": 20}
BUFFER_DAYS = {"daily_swing": 400, "weekly_position": 800, "monthly_long_term": 2500}
# Prefilter cepat (hari bursa) agar tidak memanggil pipeline untuk data jelas kurang.
MIN_DAILY_BARS = {"daily_swing": 260, "weekly_position": 600, "monthly_long_term": 1500}

# Nama faktor V1 (urutan FACTOR_WEIGHTS screener) — dipakai untuk kolom factor_* di
# signals/trades + breakdown. Diambil dari idx_screener_backtest.py buatan user agar
# keputusan V2 (bobot/threshold per faktor) punya dasar data, tanpa mengubah eksekusi.
FACTOR_NAMES = (
    "trend",
    "relative_strength",
    "momentum",
    "macd",
    "volume",
    "setup",
    "volatility",
)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Backtest V1 (baseline, frozen rules)")
    p.add_argument("--trend", default="daily_swing",
                   choices=["daily_swing", "weekly_position", "monthly_long_term",
                            "daily", "weekly", "monthly"])
    p.add_argument("--start", default="2019-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--excel", default="resource/daftar-saham.xlsx")
    p.add_argument("--tickers", default="",
                   help="Daftar kode koma-an, mis. BBCA,BBRI,TLKM(.JK opsional)")
    p.add_argument("--max-tickers", type=int, default=0,
                   help="Batasi N ticker alfabetis (0 = semua). Untuk iterasi cepat.")
    p.add_argument("--step-days", type=int, default=0,
                   help="Override rebalance hari bursa (0 = default per timeframe)")
    p.add_argument("--top", type=int, default=0,
                   help="Top-N Ready per tanggal sinyal via ranking_candidates "
                        "(0 = semua sinyal / signal-level, >0 = portfolio-level top-N).")
    p.add_argument("--allow-overlap-same-ticker", action="store_true",
                   help="Izinkan ticker sama dibuka overlap (default: blokir sampai resolved).")
    p.add_argument("--same-bar-policy", choices=["stop", "target"], default="stop",
                   help="Bila stop+target kena di bar sama (default stop = konservatif).")
    p.add_argument("--notional", type=float, default=10_000_000)
    p.add_argument("--fee-pct", type=float, default=0.2, help="Biaya per sisi (%%)")
    p.add_argument("--slippage-pct", type=float, default=0.1, help="Slippage (%%)")
    p.add_argument("--min-turnover", type=float, default=1_000_000_000)
    p.add_argument("--min-price", type=float, default=100)
    p.add_argument("--include-wait", action="store_true",
                   help="Tradingkan juga Wait for Trigger sebagai varian pembanding")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--output-dir", default="output/backtest")
    p.add_argument("--use-cache", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    # --- Serapan dari tests/idx_screener_backtest.py (analisis V2, eksekusi tetap V1) ---
    p.add_argument("--holdout-start", default="",
                   help="Tanggal mulai holdout YYYY-MM-DD (kosong = tanpa split "
                        "SELECTION/HOLDOUT). Kolom sample_split di signals/trades.")
    p.add_argument("--max-positions", type=int, default=0,
                   help="Batas posisi konkuren utk kurva ekuitas portfolio "
                        "(0 = tanpa batas / signal-level). Filter kronologis by entry_date.")
    p.add_argument("--initial-capital", type=float, default=100_000_000,
                   help="Modal awal utk kurva ekuitas portfolio (default Rp100jt).")
    p.add_argument("--self-test", action="store_true",
                   help="Uji sintetis _exit_on_bar/simulate_trade lalu keluar.")
    return p.parse_args(argv)


def normalisasi_pool_tickers(raw: str):
    out = []
    for t in (raw or "").split(","):
        t = t.strip().upper()
        if not t:
            continue
        out.append(t if t.endswith(".JK") else f"{t}.JK")
    return sorted(set(out))


def strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    try:
        idx = out.index
        if getattr(idx, "tz", None) is not None:
            out.index = idx.tz_convert(None) if hasattr(idx, "tz_convert") else idx.tz_localize(None)
    except Exception:
        pass
    return out
def load_or_download(pool, mode, start, end, batch_size, out_dir, use_cache):
    cache_dir = os.path.join(out_dir, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    pool_key = hashlib.md5(",".join(sorted(pool)).encode()).hexdigest()[:10]
    tag = f"{mode}_{start}_{end}_{pool_key}_{len(pool)}"
    cache_path = os.path.join(cache_dir, f"raw_{tag}.pkl")
    if use_cache and os.path.exists(cache_path):
        print(f"📦 Cache dipakai: {cache_path}")
        with open(cache_path, "rb") as f:
            payload = pickle.load(f)
        return payload["frames"], payload["ihsg"], False
    # Unduh period=max lalu slice: konsisten & buffer cukup utk semua timeframe.
    print(f"📥 Unduh period=max utk {len(pool)} ticker + IHSG ...")
    frames = download_saham_batch(pool, "max", max(1, batch_size))
    ihsg = download_ihsg("max")
    frames = {k: strip_tz(v) for k, v in frames.items()}
    ihsg = strip_tz(ihsg)
    with open(cache_path, "wb") as f:
        pickle.dump({"frames": frames, "ihsg": ihsg}, f)
    print(f"📦 Cache disimpan: {cache_path}")
    return frames, ihsg, True


def build_signal_dates(ihsg, frames, start, end, step_days):
    idx = strip_tz(ihsg).loc[start:end].index.normalize().unique().sort_values()
    if len(idx) == 0:
        # Fallback: gabungan tanggal semua ticker bila IHSG kosong di rentang.
        all_dates = set()
        for df in frames.values():
            try:
                all_dates.update(strip_tz(df).loc[start:end].index.normalize())
            except Exception:
                continue
        idx = pd.DatetimeIndex(sorted(all_dates))
    return [pd.Timestamp(d) for d in idx[:: max(1, step_days)]]


def _calc_r(entry, exit_px, stop, buy_fee, sell_fee):
    risk = entry - stop
    if risk <= 0:
        return float("nan"), float("nan")
    gross_r = (exit_px - entry) / risk
    entry_cost = entry * (1 + buy_fee / 100)
    exit_val = exit_px * (1 - sell_fee / 100)
    net_r = (exit_val - entry_cost) / risk
    return float(gross_r), float(net_r)


def _exit_on_bar(o, h, l, stop, target, slip, same_bar_policy):
    # Gap-open dicek dulu sebelum range High/Low (pelajaran draf lama).
    if o <= stop:
        return o * (1 - slip / 100), "STOP_GAP"
    if o >= target:
        return target * (1 - slip / 100), "TARGET"
    hit_stop = l <= stop
    hit_tgt = h >= target
    if hit_stop and hit_tgt:
        if same_bar_policy == "target":
            return target * (1 - slip / 100), "TARGET_SAME_BAR"
        return stop * (1 - slip / 100), "STOP_SAME_BAR"
    if hit_stop:
        return stop * (1 - slip / 100), "stop"
    if hit_tgt:
        return target * (1 - slip / 100), "target"
    return None, ""


def simulate_trade(sig, full_daily, max_hold_days, fee, slip, same_bar_policy="stop"):
    """Simulasi 1 sinyal pada data daily penuh. Return dict trade / None."""
    t = sig["ticker"]
    df = full_daily.get(t)
    if df is None or df.empty:
        return None
    df = strip_tz(df)
    entry_type = sig.get("entry_type", "planned")
    entry_px_plan = float(sig["entry"])
    trigger = float(sig.get("trigger_price", entry_px_plan))
    stop = float(sig["stop"])
    target = float(sig["target"])
    signal_date = pd.Timestamp(sig["signal_date"])
    window = int(sig.get("entry_window", 3))
    # Bar acuan: posisi daily signal_date dalam index.
    try:
        loc = df.index.searchsorted(signal_date + pd.Timedelta(days=1), side="left")
    except Exception:
        return None
    entry_idx = None
    entry_px = None
    entry_date = None
    if entry_type == "active":
        if loc >= len(df):
            return None
        entry_idx = loc
        entry_date = df.index[entry_idx]
        raw_open = float(df["Open"].iloc[entry_idx])
        if raw_open <= stop:
            return None
        if raw_open >= target:
            return None
        entry_px = raw_open * (1 + slip / 100.0)
    else:
        for j in range(loc, min(loc + window, len(df))):
            o = float(df["Open"].iloc[j])
            h = float(df["High"].iloc[j])
            l = float(df["Low"].iloc[j])
            if o <= stop:
                return None
            if o >= target:
                return None
            if h < trigger:
                if l <= stop:
                    return None
                continue
            entry_idx = j
            entry_date = df.index[j]
            entry_px = max(o, trigger) * (1 + slip / 100.0)
            break
        if entry_idx is None:
            return None
    shares = math.floor(float(sig.get("notional", 10_000_000)) / entry_px / LOT_SIZE) * LOT_SIZE
    if shares <= 0:
        return None
    exit_idx = None
    exit_px = None
    exit_reason = None
    if entry_px >= target:
        return None
    last = min(entry_idx + max_hold_days - 1, len(df) - 1)
    for j in range(entry_idx, last + 1):
        o = float(df["Open"].iloc[j])
        h = float(df["High"].iloc[j])
        l = float(df["Low"].iloc[j])
        px, reason = _exit_on_bar(o, h, l, stop, target,
                                  slip, same_bar_policy)
        if px is not None:
            exit_idx = j
            exit_px = px
            exit_reason = reason
            break
    if exit_idx is None:
        exit_idx = last
        exit_px = float(df["Close"].iloc[last])
        exit_px = exit_px * (1 - slip / 100.0)
        exit_reason = "time-stop"
    cost_in = entry_px * shares * (1 + fee / 100.0)
    proceeds = exit_px * shares * (1 - fee / 100.0)
    pnl_net = proceeds - cost_in
    gross = (exit_px - entry_px) * shares
    ret_net = pnl_net / cost_in if cost_in > 0 else 0.0
    gross_r, net_r = _calc_r(entry_px, exit_px, stop, fee, fee)
    # Passthrough diagnostik V1 (serapan idx_screener_backtest.py): agar trades_*.csv
    # bisa di-breakdown per faktor/setup/regime tanpa mengubah eksekusi jujur.
    out = {"ticker": t, "signal_date": str(pd.Timestamp(signal_date).date()),
           "entry_date": str(pd.Timestamp(entry_date).date()),
           "exit_date": str(pd.Timestamp(df.index[exit_idx]).date()),
           "status": sig.get("status", ""), "setup": sig.get("setup", ""),
           "regime": sig.get("regime", ""), "entry_type": entry_type,
           "entry": round(entry_px, 2), "exit": round(exit_px, 2),
           "stop": stop, "target": target, "shares": shares,
           "hold_days": int(exit_idx - entry_idx),
           "exit_reason": exit_reason, "pnl_gross": round(gross, 2),
           "pnl_net": round(pnl_net, 2), "ret_net_pct": round(ret_net * 100, 3),
           "r_multiple": round(float(net_r), 3) if net_r == net_r else float("nan"),
           "quality_score": sig.get("quality_score", 0)}
    for _k in ("signal_close", "rsi", "atr_pct", "vol_ratio", "vol_z",
               "turnover20", "rs_percentile", "rs_excess", "regime_score",
               "signal_id", "status_rank"):
        if _k in sig:
            out[_k] = sig[_k]
    for _f in FACTOR_NAMES:
        _ck = f"factor_{_f}"
        if _ck in sig:
            out[_ck] = sig[_ck]
    return out


def _signal_diag(c, s, mode, regime_str, regime_score, cfg, args):
    """Kolom diagnostik V1 (serapan idx_screener_backtest.py) — tanpa ubah eksekusi."""
    cond = c.get("conditions", {}) or {}
    diag = {
        "signal_id": f"{mode}|{pd.Timestamp(s).date()}|{c.get('ticker','')}",
        "signal_close": c.get("harga_terakhir"),
        "rsi": c.get("rsi"),
        "atr_pct": c.get("atr_pct"),
        "vol_ratio": c.get("vol_ratio"),
        "vol_z": c.get("vol_z"),
        "turnover20": c.get("turnover20"),
        "rs_percentile": c.get("rs_percentile"),
        "rs_excess": c.get("rs_excess"),
        "regime_score": regime_score,
    }
    for _f in FACTOR_NAMES:
        diag[f"factor_{_f}"] = bool(cond.get(_f, False))
    return diag


def scan_one_date(signal_date, frames, ihsg, pool, mode, min_turnover, min_price):
    """Sinyal point-in-time pada tanggal S.

    Return: (candidates, regime_str, info) dengan info memuat
    ``regime_score`` + ``universe``/``skipped`` agar diagnostik V2
    (serapan idx_screener_backtest.py) tersedia tanpa mengubah eksekusi.
    """
    s = pd.Timestamp(signal_date)
    ihsg_hist = strip_tz(ihsg).loc[:s]
    if ihsg_hist.empty or len(ihsg_hist) < 30:
        return [], "UNKNOWN", {"regime_score": 0}
    sliced = {}
    for t in pool:
        df = frames.get(t)
        if df is None or df.empty:
            continue
        h = strip_tz(df).loc[:s]
        if not h.empty:
            sliced[t] = h
    if not sliced:
        return [], "UNKNOWN", {"regime_score": 0}
    try:
        breadth = hitung_market_breadth_from_frames(sliced, pool)
    except Exception:
        return [], "UNKNOWN", {"regime_score": 0}
    try:
        regime = analisa_market_regime(ihsg_hist, mode, breadth)
    except Exception:
        return [], "UNKNOWN", {"regime_score": 0}
    regime_str = regime.get("regime", "UNKNOWN")
    try:
        regime_score = int(regime.get("score", 0))
    except Exception:
        regime_score = 0
    try:
        ihsg_tf = siapkan_data_untuk_timeframe(ihsg_hist, mode)
    except Exception:
        return [], regime_str, {"regime_score": regime_score}
    min_bars = MIN_DAILY_BARS.get(mode, 260)
    candidates, n_skip = [], 0
    for t in pool:
        h = sliced.get(t)
        if h is None or len(h) < min_bars:
            n_skip += 1
            continue
        try:
            res = analisa_saham_confluence(t, h, ihsg_tf, mode, min_turnover, min_price)
        except Exception:
            n_skip += 1
            continue
        if res.get("error"):
            n_skip += 1
            continue
        candidates.append(res)
    if not candidates:
        return [], regime_str, {"skipped": n_skip, "regime_score": regime_score}
    try:
        candidates = finalisasi_score_dan_status(candidates, mode, regime)
    except Exception as e:
        print(f"  Skip finalisasi {s.date()}: {e}")
        return [], regime_str, {"regime_score": regime_score}
    out = [c for c in candidates
           if STATUS_ALIAS.get(c["status"], c["status"])
           in (STATUS_READY, STATUS_WAIT)]
    return out, regime_str, {"universe": len(candidates),
                             "regime_score": regime_score}


def summarize(trades_df, signals_df):
    def max_dd(pnls):
        if len(pnls) == 0:
            return 0.0
        eq = np.cumsum(np.asarray(pnls, dtype=float))
        peak = np.maximum.accumulate(eq)
        return float(np.min(eq - peak))
    tr = trades_df
    if tr.empty:
        return {"n_signals": int(len(signals_df)), "n_trades": 0, "trigger_rate": 0.0,
                "win_rate": 0.0, "avg_ret_net_pct": 0.0, "expectancy_R": 0.0,
                "profit_factor": 0.0, "total_pnl_net": 0.0, "max_drawdown": 0.0,
                "avg_hold_days": 0.0, "by_exit": {}, "by_setup": {},
                "by_regime": {}, "by_status": {}}
    wins = tr[tr["pnl_net"] > 0]
    gross_w = float(tr[tr["pnl_net"] > 0]["pnl_net"].sum())
    gross_l = float(-tr[tr["pnl_net"] <= 0]["pnl_net"].sum())
    return {"n_signals": int(len(signals_df)), "n_trades": int(len(tr)),
            "trigger_rate": round(len(tr) / max(1, len(signals_df)), 4),
            "win_rate": round(len(wins) / max(1, len(tr)), 4),
            "avg_ret_net_pct": round(float(tr["ret_net_pct"].mean()), 3),
            "expectancy_R": round(float(tr["r_multiple"].mean()), 3),
            "profit_factor": round(gross_w / gross_l, 3) if gross_l > 0 else 0.0,
            "total_pnl_net": round(float(tr["pnl_net"].sum()), 2),
            "max_drawdown": round(max_dd(tr["pnl_net"].tolist()), 2),
            "avg_hold_days": round(float(tr["hold_days"].mean()), 2),
            "by_exit": tr["exit_reason"].value_counts().to_dict(),
            "by_setup": tr.groupby("setup")["r_multiple"].mean().round(3).to_dict(),
            "by_regime": tr.groupby("regime")["r_multiple"].mean().round(3).to_dict()}

def _score_bucket(q):
    try:
        q = float(q)
    except Exception:
        return "unknown"
    if q < 50:
        return "<50"
    if q < 60:
        return "50-59"
    if q < 70:
        return "60-69"
    if q < 80:
        return "70-79"
    if q < 90:
        return "80-89"
    return "90+"


def build_breakdown(tr_df):
    """Breakdown R per dimensi (serapan idx_screener_backtest.py)."""
    if tr_df is None or tr_df.empty:
        return pd.DataFrame()
    work = tr_df.copy()
    try:
        if "rank" in work.columns:
            _rk = pd.to_numeric(work["rank"], errors="coerce")
            work["rank_bucket"] = np.where(_rk <= 3, _rk.astype("Int64").astype(str), "4+")
            work.loc[_rk.isna(), "rank_bucket"] = "4+"
        else:
            work["rank_bucket"] = "4+"
    except Exception:
        work["rank_bucket"] = "4+"
    try:
        work["score_bucket"] = work["quality_score"].apply(_score_bucket) \
            if "quality_score" in work.columns else "unknown"
    except Exception:
        work["score_bucket"] = "unknown"
    base = ["status"] if "status" in work.columns else []
    if "sample_split" in work.columns:
        base = ["sample_split"] + base
    dims = ["rank_bucket", "score_bucket", "setup", "regime"] + \
        [c for c in [f"factor_{f}" for f in FACTOR_NAMES] if c in work.columns]
    rows = []
    for dim in dims:
        if dim not in work.columns:
            continue
        try:
            grp = work.groupby(base + [dim] if base else [dim], dropna=False)
        except Exception:
            continue
        for keys, g in grp:
            if not isinstance(keys, tuple):
                keys = (keys,)
            cols = (base + [dim]) if base else [dim]
            row = dict(zip(cols, keys))
            row["dimension"] = dim
            row["dimension_value"] = row.get(dim, "")
            row["n_trades"] = int(len(g))
            try:
                row["win_rate"] = round(float((g["pnl_net"] > 0).mean()), 4)
                row["avg_ret_net_pct"] = round(float(g["ret_net_pct"].mean()), 3)
                row["expectancy_R"] = round(float(g["r_multiple"].mean()), 3)
                _w = float(g[g["pnl_net"] > 0]["pnl_net"].sum())
                _l = float(-g[g["pnl_net"] <= 0]["pnl_net"].sum())
                row["profit_factor"] = round(_w / _l, 3) if _l > 0 else 0.0
            except Exception:
                row["win_rate"] = float("nan")
                row["avg_ret_net_pct"] = float("nan")
                row["expectancy_R"] = float("nan")
                row["profit_factor"] = 0.0
            rows.append(row)
    return pd.DataFrame(rows)


def summarize_full(trades_df, signals_df):
    base = summarize(trades_df, signals_df)
    if not trades_df.empty and "status" in trades_df.columns:
        base["by_status"] = trades_df.groupby("status")["r_multiple"].mean().round(3).to_dict()
    else:
        base["by_status"] = {}
    base["by_factor"] = {}
    base["by_score_bucket"] = {}
    base["by_rank_bucket"] = {}
    if trades_df is not None and not trades_df.empty:
        try:
            _bd = build_breakdown(trades_df)
            if _bd is not None and not _bd.empty:
                for _, _r in _bd.iterrows():
                    _dim = str(_r.get("dimension", ""))
                    _val = str(_r.get("dimension_value", ""))
                    _key = f"{_dim}={_val}"
                    try:
                        _exp = float(_r.get("expectancy_R", float("nan")))
                        _exp = round(_exp, 3)
                    except Exception:
                        _exp = float("nan")
                    if _dim.startswith("factor_"):
                        base["by_factor"][_key] = _exp
                    elif _dim == "score_bucket":
                        base["by_score_bucket"][_key] = _exp
                    elif _dim == "rank_bucket":
                        base["by_rank_bucket"][_key] = _exp
        except Exception:
            pass
    return base


def build_portfolio(tr_df, max_positions, initial_capital):
    """Filter kronologis + kurva ekuitas (serapan idx, versi V1 jujur)."""
    if tr_df is None or tr_df.empty or not max_positions or max_positions <= 0:
        return tr_df, pd.DataFrame(), {}
    work = tr_df.copy()
    try:
        work["_entry"] = pd.to_datetime(work["entry_date"])
        work["_exit"] = pd.to_datetime(work["exit_date"])
    except Exception:
        return work, pd.DataFrame(), {}
    work = work.sort_values(["_entry", "_exit", "ticker"]).reset_index(drop=True)
    kept, active_exits = [], []
    for _, r in work.iterrows():
        _e = r["_entry"]
        active_exits = [x for x in active_exits if x >= _e]
        if len(active_exits) >= int(max_positions):
            continue
        kept.append(r)
        active_exits.append(r["_exit"])
    filt = pd.DataFrame(kept)
    if filt.empty:
        return filt, pd.DataFrame(), {"n_trades": 0}
    filt = filt.sort_values(["_exit", "_entry"]).reset_index(drop=True)
    try:
        _pnl = pd.to_numeric(filt["pnl_net"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    except Exception:
        _pnl = np.zeros(len(filt))
    eq = float(initial_capital) + np.cumsum(_pnl)
    peak = np.maximum.accumulate(eq)
    dd = np.minimum(eq - peak, 0.0)
    curve = pd.DataFrame({"exit_date": filt["exit_date"], "equity": eq,
                          "drawdown": dd, "pnl_net": _pnl})
    summ = {"n_trades": int(len(filt)),
            "final_equity": round(float(eq[-1]), 2),
            "total_return_pct": round(float((eq[-1] / initial_capital - 1) * 100), 3),
            "max_drawdown": round(float(dd.min()), 2)}
    filt = filt.drop(columns=["_entry", "_exit"], errors="ignore")
    return filt, curve, summ


def run_self_test():
    """Uji sintetis eksekusi V1 (serapan idx, adaptasi V1)."""
    dates = pd.date_range("2025-01-02", periods=5, freq="B")
    base = pd.DataFrame({"Open": [100.0] * 5, "High": [101.0] * 5,
                         "Low": [99.0] * 5, "Close": [100.0] * 5}, index=dates)
    sig = {"ticker": "TEST.JK",
           "signal_date": str((dates[0] - pd.Timedelta(days=1)).date()),
           "entry_type": "active", "entry": 100.0, "trigger_price": 100.0,
           "stop": 95.0, "target": 110.0, "entry_window": 3,
           "notional": 10_000_000}
    px, rs = _exit_on_bar(100.0, 111.0, 99.0, 95.0, 110.0, 0.0, "stop")
    assert px == 110.0 and rs == "target", f"target gagal: {px},{rs}"
    px2, rs2 = _exit_on_bar(100.0, 111.0, 94.0, 95.0, 110.0, 0.0, "stop")
    assert rs2 == "STOP_SAME_BAR", f"same-bar harus STOP_SAME_BAR: {rs2}"
    px3, rs3 = _exit_on_bar(94.0, 95.0, 93.0, 95.0, 110.0, 0.0, "stop")
    assert rs3 == "STOP_GAP", f"gap-stop harus STOP_GAP: {rs3}"
    hit = base.copy()
    hit.loc[dates[1], "High"] = 112.0
    tr = simulate_trade(sig, {"TEST.JK": hit}, 5, 0.0, 0.0, "stop")
    assert tr and tr["exit_reason"] in ("target", "TARGET"), f"simulate target: {tr}"
    tr2 = simulate_trade(sig, {"TEST.JK": base}, 5, 0.0, 0.0, "stop")
    assert tr2 and tr2["exit_reason"] == "time-stop", f"time-stop: {tr2}"
    print("Self-test OK: target, STOP_SAME_BAR, STOP_GAP, time-stop, simulate_trade.")


def write_report(path, summary, args, mode, signal_dates, pool):
    cov = f"{len(pool)} ticker, {len(signal_dates)} tanggal sinyal"
    top_txt = args.top if args.top and args.top > 0 else "semua (signal-level)"
    overlap_txt = "allowed" if args.allow_overlap_same_ticker else "blocked until resolved"
    holdout_txt = getattr(args, "holdout_start", "") or "-"
    mp = getattr(args, "max_positions", 0) or 0
    pf_txt = f"maxpos={mp}" if mp > 0 else "signal-level"
    lines = [f"# Backtest Report V1 — {mode}",
             f"Periode {args.start}..{args.end} | {cov} | notional Rp {args.notional:,.0f}",
             f"fee {args.fee_pct}%/sisi, slippage {args.slippage_pct}%, "
             f"include_wait={args.include_wait}, top={top_txt}, "
             f"same_bar={args.same_bar_policy}, overlap={overlap_txt}, "
             f"holdout_start={holdout_txt}, portfolio={pf_txt}",
             "", "## Ringkasan",
             f"- Sinyal: {summary['n_signals']} | Trades: {summary['n_trades']} | "
             f"Trigger: {summary['trigger_rate']:.1%}",
             f"- Win: {summary['win_rate']:.1%} | Avg ret net: {summary['avg_ret_net_pct']:.2f}%"
             f" | Exp: {summary['expectancy_R']:.2f}R",
             f"- PF: {summary['profit_factor']} | PnL net: Rp {summary['total_pnl_net']:,.0f}"
             f" | MaxDD: Rp {summary['max_drawdown']:,.0f}",
             f"- Avg hold: {summary['avg_hold_days']} hari | Exit: {summary['by_exit']}",
             f"- R by setup: {summary['by_setup']}",
             f"- R by regime: {summary['by_regime']} | R by status: {summary['by_status']}",
             f"- R by factor: {summary.get('by_factor', {})}",
             f"- R by score: {summary.get('by_score_bucket', {})} | R by rank: {summary.get('by_rank_bucket', {})}",
             "", "> Baseline V1 frozen. Survivorship bias diakui (upper bound).",
             "> Lihat BACKTEST_PLAN_V1.md bagian 7-8 sebelum mengubah indikator V2.",
             "",
             "## V2 Review (serapan idx_screener_backtest.py)",
             "- Lihat breakdown_*.csv: rank_bucket/score_bucket/setup/regime/status/sample_split/factor_*.",
             "- Cek: Ready vs Wait, skor/rank vs expectancy, setup x regime negatif, factor_* lemah.",
             "- Jangan tuning V2 di HOLDOUT; kunci baseline dulu, validasi di window baru."]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None):
    args = parse_args(argv)
    if getattr(args, "self_test", False):
        run_self_test()
        return 0
    mode = normalize_timeframe(args.trend)
    cfg = get_timeframe_config(mode)
    max_hold = get_max_hold_days(mode)
    step = args.step_days or DEFAULT_STEP[mode]
    start, end = str(args.start), str(args.end)
    holdout = str(getattr(args, "holdout_start", "") or "").strip()
    holdout_ts = pd.Timestamp(holdout) if holdout else None
    if args.tickers.strip():
        pool = normalisasi_pool_tickers(args.tickers)
    else:
        pool = ambil_semua_ticker_dari_excel(args.excel)
        if args.max_tickers and args.max_tickers > 0:
            pool = sorted(pool)[: args.max_tickers]
    if not pool:
        print("Tidak ada ticker. Cek --excel / --tickers.")
        return 1
    print(f"Backtest V1 {mode}: {len(pool)} ticker, {start}..{end}, step {step}")
    print(f"max_hold={max_hold}, entry_window={cfg['entry_window']}, notional={args.notional:,.0f}")
    os.makedirs(args.output_dir, exist_ok=True)
    use_cache = args.use_cache and not args.no_cache
    frames, ihsg, _ = load_or_download(pool, mode, start, end,
                                       args.batch_size, args.output_dir, use_cache)
    if ihsg is None or ihsg.empty:
        print("Data IHSG kosong — berhenti.")
        return 1
    frames = {k: v for k, v in frames.items() if v is not None and not v.empty}
    pool = [t for t in pool if t in frames]
    signal_dates = build_signal_dates(ihsg, frames, start, end, step)
    print(f"Tanggal sinyal: {len(signal_dates)}")
    all_signals, all_trades = [], []
    busy_until = {}
    for i, s in enumerate(signal_dates, 1):
        sigs, regime, info = scan_one_date(s, frames, ihsg, pool, mode,
                                           args.min_turnover, args.min_price)
        regime_score = (info or {}).get("regime_score", 0)
        print(f"  [{i}/{len(signal_dates)}] {s.date()} regime={regime} sinyal={len(sigs)}")
        for c in sigs:
            diag = _signal_diag(c, s, mode, regime, regime_score, cfg, args)
            row0 = {"signal_date": str(s.date()), "ticker": c["ticker"],
                    "status": c["status"], "regime": regime,
                    "entry_type": c.get("entry_type", ""),
                    "entry": c["entry_level"],
                    "trigger_price": c.get("trigger_price", c["entry_level"]),
                    "stop": c["stop_level"], "target": c["target_price"],
                    "entry_window": cfg["entry_window"], "setup": c["setup_name"],
                    "quality_score": c["quality_score"], "notional": args.notional}
            row0.update(diag)
            all_signals.append(row0)
        if args.top and args.top > 0:
            ranked, _ = ranking_candidates(sigs, args.top)
            tradable = ranked[: args.top]
        else:
            tradable = list(sigs)
        for rank, c in enumerate(tradable, 1):
            st = STATUS_ALIAS.get(c.get("status"), c.get("status"))
            if st == STATUS_WAIT and not args.include_wait:
                continue
            if st not in (STATUS_READY, STATUS_WAIT):
                continue
            if not args.allow_overlap_same_ticker:
                until = busy_until.get(c["ticker"])
                if until is not None and s <= until:
                    continue
            diag = _signal_diag(c, s, mode, regime, regime_score, cfg, args)
            row = {"signal_date": str(s.date()), "ticker": c["ticker"],
                   "rank": rank, "status_rank": rank,
                   "status": c["status"], "regime": regime,
                   "entry_type": c.get("entry_type", ""),
                   "entry": c["entry_level"],
                   "trigger_price": c.get("trigger_price", c["entry_level"]),
                   "stop": c["stop_level"], "target": c["target_price"],
                   "entry_window": cfg["entry_window"], "setup": c["setup_name"],
                   "quality_score": c["quality_score"], "notional": args.notional}
            row.update(diag)
            row["status_rank"] = rank
            tr = simulate_trade(row, frames, max_hold, args.fee_pct,
                                args.slippage_pct, args.same_bar_policy)
            if tr:
                tr["rank"] = rank
                tr["status_rank"] = rank
                all_trades.append(tr)
                if not args.allow_overlap_same_ticker:
                    try:
                        busy_until[c["ticker"]] = pd.Timestamp(tr["exit_date"])
                    except Exception:
                        pass
    tag = f"{mode}_{start}_{end}"
    sig_df = pd.DataFrame(all_signals)
    tr_df = pd.DataFrame(all_trades)
    # Split SELECTION/HOLDOUT ala idx_screener_backtest.py (opsional, tanpa ubah eksekusi).
    if holdout_ts is not None:
        try:
            for _df in (sig_df, tr_df):
                if not _df.empty and "signal_date" in _df.columns:
                    _sd = pd.to_datetime(_df["signal_date"])
                    _df["sample_split"] = np.where(_sd < holdout_ts, "SELECTION", "HOLDOUT")
        except Exception:
            pass
    sig_path = os.path.join(args.output_dir, f"signals_{tag}.csv")
    tr_path = os.path.join(args.output_dir, f"trades_{tag}.csv")
    sum_path = os.path.join(args.output_dir, f"summary_{tag}.json")
    rep_path = os.path.join(args.output_dir, f"BACKTEST_REPORT_{tag}.md")
    bd_path = os.path.join(args.output_dir, f"breakdown_{tag}.csv")
    pf_path = os.path.join(args.output_dir, f"portfolio_{tag}.csv")
    pc_path = os.path.join(args.output_dir, f"portfolio_curve_{tag}.csv")
    sig_df.to_csv(sig_path, index=False)
    tr_df.to_csv(tr_path, index=False)
    try:
        build_breakdown(tr_df).to_csv(bd_path, index=False)
    except Exception as e:
        print(f"  Skip breakdown: {e}")
        bd_path = ""
    # Portfolio kronologis opsional (max-positions>0): filter + kurva ekuitas.
    pf_summary = {}
    try:
        _mp = int(getattr(args, "max_positions", 0) or 0)
    except Exception:
        _mp = 0
    if _mp > 0 and not tr_df.empty:
        try:
            filt_pf, curve_pf, pf_summary = build_portfolio(
                tr_df, _mp, float(getattr(args, "initial_capital", 100_000_000)))
            filt_pf.to_csv(pf_path, index=False)
            curve_pf.to_csv(pc_path, index=False)
        except Exception as e:
            print(f"  Skip portfolio: {e}")
            pf_summary = {}
    summary = summarize_full(tr_df, sig_df)
    summary.update({"mode": mode, "start": start, "end": end, "pool_size": len(pool),
                    "n_signal_dates": len(signal_dates), "step_days": step,
                    "max_hold_days": max_hold, "notional": args.notional,
                    "fee_pct": args.fee_pct, "slippage_pct": args.slippage_pct,
                    "include_wait": args.include_wait, "top": args.top,
                    "same_bar_policy": args.same_bar_policy,
                    "allow_overlap_same_ticker": bool(args.allow_overlap_same_ticker),
                    "holdout_start": holdout or "",
                    "max_positions": _mp,
                    "initial_capital": float(getattr(args, "initial_capital", 100_000_000)),
                    "portfolio": pf_summary,
                    "generated_at": datetime.now().isoformat(timespec="seconds")})
    with open(sum_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    write_report(rep_path, summary, args, mode, signal_dates, pool)
    print(f"Sinyal: {len(sig_df)} | Trades: {len(tr_df)} | Win: {summary['win_rate']:.1%} | "
          f"ExpR: {summary['expectancy_R']}")
    print(f"  {sig_path}\n  {tr_path}\n  {sum_path}\n  {rep_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

