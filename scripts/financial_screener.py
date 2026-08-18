import os
import math
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None


IDX_BENCHMARK = "^JKSE"
IDX_TZ = ZoneInfo("Asia/Jakarta")
LOT_SIZE = 100

# Penentuan threshold - Pake AI kemaren nyari threshold nya wkwk
TIMEFRAME_CONFIG = {
    "1hari": {
        "min_rows": 260,
        "rs_lookback": 60,
        "sr_window": 20,
        "breakout_window": 20,
        "slope_lookback": 5,
        "rsi_min": 50,
        "rsi_max": 72,
        "max_extension_ema9": 0.045,
        "max_atr_pct": 0.085,
        "min_atr_pct": 0.008,
        "breakout_vol_ratio": 1.30,
        "strong_buy_score": 72,
        "stop_atr": 1.50,
        "target_rr": 2.00,
    },
    "1minggu": {
        "min_rows": 120,
        "rs_lookback": 13,
        "sr_window": 12,
        "breakout_window": 13,
        "slope_lookback": 4,
        "rsi_min": 48,
        "rsi_max": 72,
        "max_extension_ema9": 0.075,
        "max_atr_pct": 0.16,
        "min_atr_pct": 0.02,
        "breakout_vol_ratio": 1.20,
        "strong_buy_score": 70,
        "stop_atr": 2.00,
        "target_rr": 2.50,
    },
    "1bulan": {
        "min_rows": 72,
        "rs_lookback": 6,
        "sr_window": 8,
        "breakout_window": 6,
        "slope_lookback": 3,
        "rsi_min": 45,
        "rsi_max": 70,
        "max_extension_ema9": 0.12,
        "max_atr_pct": 0.28,
        "min_atr_pct": 0.04,
        "breakout_vol_ratio": 1.10,
        "strong_buy_score": 68,
        "stop_atr": 2.50,
        "target_rr": 3.00,
    },
}

FACTOR_WEIGHTS = {
    "trend": 20,
    "relative_strength": 20,
    "momentum": 10,
    "macd": 10,
    "volume": 15,
    "setup": 15,
    "volatility": 10,
}


def safe_float(value, default=np.nan):
    try:
        v = float(value)
        return v if np.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def wilder_rma(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's smoothing / RMA."""
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def hitung_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = wilder_rma(gain, period)
    avg_loss = wilder_rma(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def fraksi_harga_idx(price: float) -> int:
    """Fraksi harga saham BEI pasar reguler."""
    if price < 200:
        return 1
    if price < 500:
        return 2
    if price < 2000:
        return 5
    if price < 5000:
        return 10
    return 25


def round_idx_price(price: float, direction: str = "nearest") -> float:
    if not np.isfinite(price) or price <= 0:
        return np.nan
    tick = fraksi_harga_idx(price)
    units = price / tick
    if direction == "up":
        return float(math.ceil(units) * tick)
    if direction == "down":
        return float(math.floor(units) * tick)
    return float(round(units) * tick)


# Fallback - Karena bisa jadi gagal download dari yfinance, disediakan excel, cuma notes nya perlu di update jika ada IPO dll
def ambil_semua_ticker_dari_excel(file_path="resource/daftar-saham.xlsx"):
    try:
        if not os.path.exists(file_path):
            print(f"❌ File '{file_path}' tidak ditemukan!")
            return []

        df_excel = pd.read_excel(file_path)
        if "Kode" not in df_excel.columns:
            print("❌ Kolom 'Kode' tidak ditemukan di file Excel!")
            return []

        tickers = (
            df_excel["Kode"]
            .dropna()
            .astype(str)
            .str.strip()
            .str.upper()
        )
        tickers = tickers[tickers != ""]
        tickers = tickers.apply(lambda x: x if x.endswith(".JK") else f"{x}.JK")
        return tickers.drop_duplicates().tolist()
    except Exception as e:
        print(f"❌ Gagal membaca Excel: {e}")
        return []


def ekstrak_ticker_frame(data_massal: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Robust terhadap MultiIndex yfinance group_by='ticker'."""
    if data_massal is None or data_massal.empty:
        return pd.DataFrame()

    try:
        if isinstance(data_massal.columns, pd.MultiIndex):
            level0 = data_massal.columns.get_level_values(0)
            level1 = data_massal.columns.get_level_values(1)
            if ticker in level0:
                df = data_massal[ticker].copy()
            elif ticker in level1:
                df = data_massal.xs(ticker, axis=1, level=1).copy()
            else:
                return pd.DataFrame()
        else:
            df = data_massal.copy()

        df.columns = [str(c).title() for c in df.columns]
        needed = ["Open", "High", "Low", "Close", "Volume"]
        if not all(c in df.columns for c in needed):
            return pd.DataFrame()
        return df
    except Exception:
        return pd.DataFrame()


def buang_daily_candle_belum_selesai(df: pd.DataFrame) -> pd.DataFrame:
    """
    Menghindari daily candle hari ini sebelum data EOD cukup aman dianggap final.
    BEI regular market selesai sekitar 15:49 WIB; buffer dipakai sampai 16:10 WIB.
    """
    if df.empty:
        return df

    out = df.copy()
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_convert(IDX_TZ).tz_localize(None)

    now = datetime.now(IDX_TZ)
    today = pd.Timestamp(now.date())
    last_date = pd.Timestamp(out.index.max()).normalize()

    if last_date == today and (now.hour, now.minute) < (16, 10):
        out = out.iloc[:-1]

    return out


def resample_timeframe(df_daily: pd.DataFrame, mode_tren: str) -> pd.DataFrame:
    required = ["Open", "High", "Low", "Close", "Volume"]
    df = df_daily.dropna(subset=required).copy()
    if df.empty:
        raise ValueError("Data OHLCV tidak tersedia")

    # Turnover dihitung pada level daily agar weekly/monthly tidak sekadar
    # menggunakan Close akhir periode × total volume.
    df["Turnover"] = df["Close"] * df["Volume"]

    if mode_tren == "1hari":
        tf = df[required + ["Turnover"]].copy()
    elif mode_tren == "1minggu":
        tf = (
            df[required + ["Turnover"]]
            .resample("W-FRI", label="right", closed="right")
            .agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
                "Turnover": "sum",
            })
            .dropna(subset=required)
        )
    elif mode_tren == "1bulan":
        tf = (
            df[required + ["Turnover"]]
            .resample("ME", label="right", closed="right")
            .agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
                "Turnover": "sum",
            })
            .dropna(subset=required)
        )
    else:
        raise ValueError(f"Mode tren tidak dikenal: {mode_tren}")

    # Untuk weekly/monthly, hanya pakai candle yang label periodenya sudah
    # <= tanggal daily terakhir yang sudah dianggap selesai.
    if mode_tren != "1hari" and not tf.empty:
        last_completed_daily = pd.Timestamp(df.index.max()).normalize()
        tf = tf[tf.index.normalize() <= last_completed_daily]

    return tf


def tambah_indikator(tf: pd.DataFrame, mode_tren: str) -> pd.DataFrame:
    cfg = TIMEFRAME_CONFIG[mode_tren]
    df = tf.copy()

    if len(df) < cfg["min_rows"]:
        raise ValueError(f"Data tidak cukup: {len(df)} bar (min {cfg['min_rows']})")

    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()

    df["RSI"] = hitung_rsi(df["Close"], 14)

    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["Signal_Line"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["Histogram"] = df["MACD"] - df["Signal_Line"]

    df["Vol_MA20"] = df["Volume"].rolling(20).mean()
    log_vol = np.log1p(df["Volume"].clip(lower=0))
    vol_mean = log_vol.rolling(20).mean()
    vol_std = log_vol.rolling(20).std().replace(0, np.nan)
    df["Vol_Z"] = (log_vol - vol_mean) / vol_std

    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["ATR"] = wilder_rma(tr, 14)
    df["ATR_Pct"] = df["ATR"] / df["Close"].replace(0, np.nan)

    breakout_window = cfg["breakout_window"]
    df["Prev_High"] = df["High"].rolling(breakout_window).max().shift(1)

    df["Body"] = df["Close"] - df["Open"]
    df["Upper_Wick"] = df["High"] - df[["Open", "Close"]].max(axis=1)
    df["Lower_Wick"] = df[["Open", "Close"]].min(axis=1) - df["Low"]
    df["Candle_Range"] = df["High"] - df["Low"]

    return df


def siapkan_data_untuk_timeframe(df_saham: pd.DataFrame, mode_tren: str) -> pd.DataFrame:
    daily = buang_daily_candle_belum_selesai(df_saham)
    tf = resample_timeframe(daily, mode_tren)
    return tambah_indikator(tf, mode_tren)


def deteksi_pola_candle(hari_ini: pd.Series, kemarin: pd.Series):
    body = safe_float(hari_ini["Body"], 0)
    candle_range = max(safe_float(hari_ini["Candle_Range"], 0), 1e-9)
    lower_wick = safe_float(hari_ini["Lower_Wick"], 0)
    upper_wick = safe_float(hari_ini["Upper_Wick"], 0)
    kemarin_body = safe_float(kemarin["Body"], 0)

    bullish_engulfing = (
        body > 0
        and kemarin_body < 0
        and safe_float(hari_ini["Open"]) <= safe_float(kemarin["Close"])
        and safe_float(hari_ini["Close"]) >= safe_float(kemarin["Open"])
    )
    hammer = (
        body > 0
        and lower_wick >= 2 * abs(body)
        and upper_wick <= abs(body) * 0.6
        and (lower_wick / candle_range) >= 0.50
    )
    marubozu = body > 0 and (abs(body) / candle_range) >= 0.75
    strong_close = ((safe_float(hari_ini["Close"]) - safe_float(hari_ini["Low"])) / candle_range) >= 0.70

    if bullish_engulfing:
        return "Bullish Engulfing", True, strong_close
    if marubozu:
        return "Marubozu Bullish", True, strong_close
    if hammer:
        return "Hammer", True, strong_close
    if strong_close and body > 0:
        # Strong close saja belum cukup dianggap setup; dipakai sebagai konfirmasi
        # untuk breakout/pullback agar tidak terlalu mudah menambah score.
        return "Strong Bullish Close", False, strong_close
    return "Tidak ada pola bullish kuat", False, strong_close


def hitung_market_breadth(data_massal: pd.DataFrame, pool: list[str]):
    above50 = 0
    above200 = 0
    valid50 = 0
    valid200 = 0
    returns20 = []

    for ticker in pool:
        df = ekstrak_ticker_frame(data_massal, ticker)
        if df.empty:
            continue
        df = buang_daily_candle_belum_selesai(df).dropna(subset=["Close"])
        if len(df) < 60:
            continue

        close = df["Close"]
        ema50 = close.ewm(span=50, adjust=False).mean()
        valid50 += 1
        if close.iloc[-1] > ema50.iloc[-1]:
            above50 += 1

        if len(df) >= 220:
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


def analisa_market_regime(ihsg_daily: pd.DataFrame, mode_tren: str, breadth: dict):
    df = siapkan_data_untuk_timeframe(ihsg_daily, mode_tren)
    now = df.iloc[-1]
    cfg = TIMEFRAME_CONFIG[mode_tren]

    close = safe_float(now["Close"])
    ema20 = safe_float(now["EMA20"])
    ema50 = safe_float(now["EMA50"])
    ema200 = safe_float(now["EMA200"])
    rsi = safe_float(now["RSI"], 50)

    if mode_tren == "1hari":
        long_trend = close > ema200
    elif mode_tren == "1minggu":
        long_trend = close > ema50
    else:
        long_trend = close > ema20

    medium_trend = ema20 > ema50
    slope_n = cfg["slope_lookback"]
    ema20_slope_up = len(df) > slope_n and ema20 > safe_float(df["EMA20"].iloc[-1 - slope_n], ema20)
    momentum_ok = rsi >= 50

    b50 = safe_float(breadth.get("breadth50"), 0.5)
    breadth_ok = b50 >= 0.52

    score = sum([long_trend, medium_trend, ema20_slope_up, momentum_ok, breadth_ok])
    if score >= 4 and b50 >= 0.50:
        regime = "BULLISH"
    elif score >= 3:
        regime = "NEUTRAL"
    else:
        regime = "BEARISH"

    return {
        "regime": regime,
        "score": score,
        "score_max": 5,
        "close": close,
        "rsi": rsi,
        "breadth50": b50,
        "breadth200": safe_float(breadth.get("breadth200"), np.nan),
        "median_return20": safe_float(breadth.get("median_return20"), np.nan),
        "detail": (
            f"IHSG longTrend={'Y' if long_trend else 'N'}, "
            f"EMA20>EMA50={'Y' if medium_trend else 'N'}, "
            f"EMA20Slope={'UP' if ema20_slope_up else 'DOWN'}, "
            f"RSI={rsi:.1f}, Breadth>EMA50={b50:.1%}"
        ),
    }


def hitung_relative_strength(stock_tf: pd.DataFrame, ihsg_tf: pd.DataFrame, lookback: int):
    aligned = pd.concat(
        [stock_tf["Close"].rename("stock"), ihsg_tf["Close"].rename("ihsg")],
        axis=1,
        join="inner",
    ).dropna()

    if len(aligned) <= lookback:
        return np.nan, np.nan, False

    stock_ret = aligned["stock"].iloc[-1] / aligned["stock"].iloc[-1 - lookback] - 1
    ihsg_ret = aligned["ihsg"].iloc[-1] / aligned["ihsg"].iloc[-1 - lookback] - 1
    excess = stock_ret - ihsg_ret

    rs_line = aligned["stock"] / aligned["ihsg"].replace(0, np.nan)
    rs_ema20 = rs_line.ewm(span=20, adjust=False).mean()
    rs_trend_up = bool(rs_line.iloc[-1] > rs_ema20.iloc[-1])

    return float(excess), float(stock_ret), rs_trend_up


def hitung_daily_liquidity(df_saham: pd.DataFrame):
    daily = buang_daily_candle_belum_selesai(df_saham).dropna(
        subset=["Close", "Volume"]
    ).copy()
    if len(daily) < 20:
        return np.nan, np.nan

    turnover = daily["Close"] * daily["Volume"]
    median20 = float(turnover.tail(20).median())
    median60 = float(turnover.tail(min(60, len(turnover))).median())
    return median20, median60


def analisa_saham_confluence(
    ticker_code: str,
    df_saham: pd.DataFrame,
    ihsg_tf: pd.DataFrame,
    mode_tren: str,
    min_turnover: float,
    min_price: float,
):
    try:
        cfg = TIMEFRAME_CONFIG[mode_tren]
        df = siapkan_data_untuk_timeframe(df_saham, mode_tren)
        if len(df) < 2:
            raise ValueError("Bar timeframe tidak cukup")

        hari_ini = df.iloc[-1]
        kemarin = df.iloc[-2]
        close = safe_float(hari_ini["Close"])
        ema9 = safe_float(hari_ini["EMA9"])
        ema20 = safe_float(hari_ini["EMA20"])
        ema50 = safe_float(hari_ini["EMA50"])
        ema200 = safe_float(hari_ini["EMA200"])
        atr = safe_float(hari_ini["ATR"], close * 0.02)
        atr_pct = safe_float(hari_ini["ATR_Pct"], atr / close if close else np.nan)

        if not all(np.isfinite(v) for v in [close, ema9, ema20, ema50, atr, atr_pct]):
            raise ValueError("Indikator utama mengandung NaN")

        # ---------- Likuiditas IDX: gunakan Rupiah turnover daily ----------
        turnover20, turnover60 = hitung_daily_liquidity(df_saham)
        liquidity_ok = np.isfinite(turnover20) and turnover20 >= min_turnover
        price_ok = close >= min_price

        # ---------- Hard trend filter ----------
        if mode_tren == "1hari":
            structural_trend = close > ema200 and ema20 > ema50
        elif mode_tren == "1minggu":
            structural_trend = close > ema50 and ema20 > ema50
        else:
            structural_trend = close > ema20 and ema20 > ema50

        extension = (close - ema9) / ema9 if ema9 else np.inf
        not_extended = extension <= cfg["max_extension_ema9"]

        candle_range = max(safe_float(hari_ini["Candle_Range"], 0), 1e-9)
        upper_wick_ratio = safe_float(hari_ini["Upper_Wick"], 0) / candle_range
        valid_wick = upper_wick_ratio <= (0.38 if mode_tren == "1hari" else 0.50)

        volatility_ok = cfg["min_atr_pct"] <= atr_pct <= cfg["max_atr_pct"]
        hard_pass = all([
            structural_trend,
            not_extended,
            liquidity_ok,
            price_ok,
            volatility_ok,
            valid_wick,
        ])

        # ---------- Trend factor ----------
        slope_n = cfg["slope_lookback"]
        ema20_prev = safe_float(df["EMA20"].iloc[-1 - slope_n], ema20)
        trend_ok = close > ema20 and ema9 > ema20 and ema20 > ema20_prev

        # ---------- Relative strength vs IHSG ----------
        rs_excess, stock_return, rs_trend_up = hitung_relative_strength(
            df, ihsg_tf, cfg["rs_lookback"]
        )

        # ---------- Momentum ----------
        rsi_now = safe_float(hari_ini["RSI"], 50)
        rsi_prev = safe_float(kemarin["RSI"], rsi_now)
        momentum_ok = (
            cfg["rsi_min"] <= rsi_now <= cfg["rsi_max"]
            and rsi_now >= rsi_prev - 2.0
        )

        # ---------- MACD ----------
        hist_now = safe_float(hari_ini["Histogram"], 0)
        hist_prev = safe_float(kemarin["Histogram"], hist_now)
        macd_now = safe_float(hari_ini["MACD"], 0)
        signal_now = safe_float(hari_ini["Signal_Line"], 0)
        macd_ok = macd_now > signal_now and hist_now > 0 and hist_now >= hist_prev

        # ---------- Volume ----------
        vol_ma20 = safe_float(hari_ini["Vol_MA20"], 0)
        vol_ratio = safe_float(hari_ini["Volume"], 0) / vol_ma20 if vol_ma20 > 0 else 0
        vol_z = safe_float(hari_ini["Vol_Z"], 0)
        bullish_body = safe_float(hari_ini["Body"], 0) > 0
        volume_ok = bullish_body and (vol_ratio >= 1.20 or vol_z >= 1.0)

        # ---------- Price action: breakout / pullback / candle ----------
        prev_high = safe_float(hari_ini["Prev_High"], close)
        nama_pola, pola_candle_ok, strong_close = deteksi_pola_candle(hari_ini, kemarin)

        raw_breakout = close > prev_high
        breakout_ok = (
            raw_breakout
            and strong_close
            and vol_ratio >= cfg["breakout_vol_ratio"]
        )

        low = safe_float(hari_ini["Low"], close)
        prev_close = safe_float(kemarin["Close"], close)
        pullback_ok = (
            low <= ema20 * 1.02
            and close >= ema9
            and close > prev_close
            and bullish_body
            and strong_close
        )

        setup_ok = breakout_ok or pullback_ok or pola_candle_ok
        if breakout_ok:
            setup_name = "Breakout Confirmed"
        elif pullback_ok:
            setup_name = "Bullish Pullback/Reclaim"
        elif pola_candle_ok:
            setup_name = nama_pola
        elif raw_breakout:
            setup_name = "Breakout tanpa konfirmasi volume/close"
        else:
            setup_name = "Belum ada trigger"

        # ---------- Support / Entry / Stop / Target ----------
        sr_window = cfg["sr_window"]
        support = safe_float(df["Low"].rolling(sr_window).min().iloc[-1], close - atr)
        resistance = safe_float(df["High"].rolling(sr_window).max().iloc[-1], close + atr)

        tick = fraksi_harga_idx(close)
        if breakout_ok:
            entry_raw = max(close, prev_high + tick)
        else:
            entry_raw = close
        entry = round_idx_price(entry_raw, "up")

        structure_stop = min(ema20, support) - 0.25 * atr
        atr_stop = entry - cfg["stop_atr"] * atr
        # Pilih stop yang tidak terlalu longgar, tetapi tetap berada di bawah struktur.
        stop_raw = max(structure_stop, atr_stop)
        if stop_raw >= entry:
            stop_raw = entry - cfg["stop_atr"] * atr
        stop = round_idx_price(stop_raw, "down")

        risk_per_share = max(entry - stop, tick)
        target_raw = entry + cfg["target_rr"] * risk_per_share
        target = round_idx_price(target_raw, "up")
        risk_reward = (target - entry) / risk_per_share if risk_per_share > 0 else np.nan

        conditions = {
            "trend": trend_ok,
            "relative_strength": False,  # diisi setelah percentile universe diketahui
            "momentum": momentum_ok,
            "macd": macd_ok,
            "volume": volume_ok,
            "setup": setup_ok,
            "volatility": volatility_ok,
        }

        hard_fail_reasons = []
        if not structural_trend:
            hard_fail_reasons.append("struktur tren")
        if not not_extended:
            hard_fail_reasons.append("harga terlalu extended")
        if not liquidity_ok:
            hard_fail_reasons.append("likuiditas rupiah")
        if not price_ok:
            hard_fail_reasons.append("harga minimum")
        if not volatility_ok:
            hard_fail_reasons.append("ATR%")
        if not valid_wick:
            hard_fail_reasons.append("upper wick")

        return {
            "ticker": ticker_code,
            "date": str(pd.Timestamp(df.index[-1]).date()),
            "harga_terakhir": close,
            "ema9": ema9,
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "rsi": rsi_now,
            "atr": atr,
            "atr_pct": atr_pct,
            "support_level": support,
            "resistance_level": resistance,
            "entry_level": entry,
            "stop_level": stop,
            "target_price": target,
            "risk_reward": risk_reward,
            "risk_per_share": risk_per_share,
            "turnover20": turnover20,
            "turnover60": turnover60,
            "vol_ratio": vol_ratio,
            "vol_z": vol_z,
            "rs_excess": rs_excess,
            "stock_return": stock_return,
            "rs_trend_up": rs_trend_up,
            "rs_percentile": np.nan,
            "setup_name": setup_name,
            "raw_breakout": raw_breakout,
            "breakout_ok": breakout_ok,
            "pullback_ok": pullback_ok,
            "pola_candle": nama_pola,
            "hard_pass": hard_pass,
            "hard_fail_reasons": hard_fail_reasons,
            "conditions": conditions,
            "quality_score": 0.0,
            "status": "Pending",
            "error": False,
        }
    except Exception as e:
        return {"ticker": ticker_code, "error": True, "alasan": str(e)}


def finalisasi_score_dan_status(candidates: list[dict], mode_tren: str, market_regime: dict):
    valid_rs = [c["rs_excess"] for c in candidates if np.isfinite(c.get("rs_excess", np.nan))]

    if valid_rs:
        rs_series = pd.Series(valid_rs)
        for c in candidates:
            rs = c.get("rs_excess", np.nan)
            if np.isfinite(rs):
                # Percentile empiris pada universe saat ini.
                c["rs_percentile"] = float((rs_series <= rs).mean() * 100)
            else:
                c["rs_percentile"] = 0.0
    else:
        for c in candidates:
            c["rs_percentile"] = 0.0

    base_threshold = TIMEFRAME_CONFIG[mode_tren]["strong_buy_score"]
    regime = market_regime["regime"]

    for c in candidates:
        rs_ok = (
            c["rs_excess"] > 0
            and c["rs_trend_up"]
            and c["rs_percentile"] >= 60
        )
        c["conditions"]["relative_strength"] = bool(rs_ok)

        score = sum(
            FACTOR_WEIGHTS[name] * int(bool(c["conditions"][name]))
            for name in FACTOR_WEIGHTS
        )
        c["quality_score"] = float(score)

        if regime == "BULLISH":
            required_score = base_threshold
            min_rs_pct = 60
        elif regime == "NEUTRAL":
            required_score = min(base_threshold + 5, 90)
            min_rs_pct = 70
        else:
            required_score = 101  # tidak keluarkan Strong Buy saat regime bearish
            min_rs_pct = 80

        strong_buy = (
            c["hard_pass"]
            and c["conditions"]["setup"]
            and c["quality_score"] >= required_score
            and c["rs_percentile"] >= min_rs_pct
            and regime != "BEARISH"
        )

        if strong_buy:
            c["status"] = "Strong Buy"
        elif c["hard_pass"] and c["quality_score"] >= 50:
            c["status"] = "Watchlist"
        else:
            c["status"] = "Avoid"

        c["kondisi_detail"] = ", ".join(
            f"{'✅' if c['conditions'][k] else '❌'} {k}"
            for k in FACTOR_WEIGHTS
        )

    return candidates


def hitung_position_size(candidate: dict, capital: float, risk_pct: float, max_position_pct: float):
    if not capital or capital <= 0:
        return None

    risk_budget = capital * (risk_pct / 100)
    max_position_budget = capital * (max_position_pct / 100)
    risk_per_lot = candidate["risk_per_share"] * LOT_SIZE
    cost_per_lot = candidate["entry_level"] * LOT_SIZE

    if risk_per_lot <= 0 or cost_per_lot <= 0:
        return None

    lots_by_risk = math.floor(risk_budget / risk_per_lot)
    lots_by_capital = math.floor(max_position_budget / cost_per_lot)
    lots = max(0, min(lots_by_risk, lots_by_capital))

    return {
        "lots": lots,
        "shares": lots * LOT_SIZE,
        "estimated_value": lots * cost_per_lot,
        "max_risk_rupiah": lots * risk_per_lot,
    }


def ranking_candidates(candidates: list[dict], limit: int = 3):
    strong = [c for c in candidates if c["status"] == "Strong Buy"]
    watch = [c for c in candidates if c["status"] == "Watchlist"]

    def key(c):
        return (
            c["quality_score"],
            c.get("rs_percentile", 0),
            int(c.get("breakout_ok", False)),
            c.get("vol_z", 0),
            c.get("turnover20", 0),
        )

    strong = sorted(strong, key=key, reverse=True)
    watch = sorted(watch, key=key, reverse=True)

    if strong:
        return strong[:limit], "Strong Buy"
    return watch[:limit], "Watchlist"


def format_rupiah(v):
    if not np.isfinite(safe_float(v)):
        return "-"
    return f"Rp {float(v):,.0f}".replace(",", ".")


def format_pct(v):
    if not np.isfinite(safe_float(v)):
        return "-"
    return f"{float(v) * 100:.1f}%"


def deterministic_report(top_picks, mode_tren, market_regime, capital=0):
    lines = []
    lines.append(f"### 📊 IDX Stock Report — {mode_tren.upper()} (Top Picks)")
    lines.append("")
    lines.append(
        f"**Market Regime:** {market_regime['regime']} "
        f"({market_regime['score']}/{market_regime['score_max']}) | "
        f"IHSG {format_rupiah(market_regime['close'])} | "
        f"Breadth > EMA50 {market_regime['breadth50']:.1%}"
    )
    lines.append("")
    lines.append("| Ticker | Status | Quality | RS Pctl | Close | Entry | Stop | Target | Setup |")
    lines.append("| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |")

    for c in top_picks:
        lines.append(
            f"| {c['ticker'].replace('.JK','')} | {c['status']} | {c['quality_score']:.0f}/100 | "
            f"{c['rs_percentile']:.0f}% | {format_rupiah(c['harga_terakhir'])} | "
            f"{format_rupiah(c['entry_level'])} | {format_rupiah(c['stop_level'])} | "
            f"{format_rupiah(c['target_price'])} | {c['setup_name']} |"
        )

    lines.append("")
    lines.append("**Detail:**")
    for c in top_picks:
        liq_m = c["turnover20"] / 1e9 if np.isfinite(c["turnover20"]) else np.nan
        lines.append(
            f"- **{c['ticker'].replace('.JK','')}** — RSI {c['rsi']:.1f}, "
            f"RS excess {format_pct(c['rs_excess'])}, RS percentile {c['rs_percentile']:.0f}%, "
            f"volume {c['vol_ratio']:.2f}x MA20 / Z {c['vol_z']:.2f}, "
            f"median turnover 20D Rp {liq_m:.2f} miliar, ATR {format_pct(c['atr_pct'])}."
        )
        if capital > 0 and c.get("position_size"):
            p = c["position_size"]
            lines.append(
                f"  Position sizing: {p['lots']} lot ({p['shares']} saham), "
                f"nilai sekitar {format_rupiah(p['estimated_value'])}, "
                f"risiko sekitar {format_rupiah(p['max_risk_rupiah'])}."
            )

    lines.append("")
    lines.append("*Scanner teknikal bersifat probabilistik; validasi dengan backtest dan disiplin risk management.*")
    return "\n".join(lines)


def ambil_berita(ticker: str, limit: int = 2):
    try:
        items = yf.Ticker(ticker).get_news(count=limit, tab="news") or []
    except Exception:
        return []

    result = []
    for item in items[:limit]:
        content = item.get("content", {}) if isinstance(item, dict) else {}
        title = (
            content.get("title")
            or (item.get("title") if isinstance(item, dict) else None)
            or "Tanpa judul"
        )
        provider = content.get("provider", {}) if isinstance(content, dict) else {}
        publisher = (
            provider.get("displayName")
            if isinstance(provider, dict)
            else None
        ) or (item.get("publisher") if isinstance(item, dict) else None) or "Unknown"
        result.append(f"- {title} ({publisher})")
    return result


def generate_gemini_report(top_picks, mode_tren, market_regime, fallback_report):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or genai is None or types is None:
        if not api_key:
            print("ℹ️ GEMINI_API_KEY tidak ada; memakai report deterministik.")
        else:
            print("ℹ️ google-genai tidak tersedia; memakai report deterministik.")
        return fallback_report

    prompt = [
        f"TIMEFRAME: {mode_tren.upper()}",
        f"MARKET REGIME: {market_regime['regime']} ({market_regime['score']}/{market_regime['score_max']})",
        f"IHSG: {market_regime['close']:.2f}",
        f"Breadth > EMA50: {market_regime['breadth50']:.1%}",
        f"Market detail: {market_regime['detail']}",
        "",
    ]

    for c in top_picks:
        news = ambil_berita(c["ticker"], 2)
        prompt.extend([
            f"Ticker: {c['ticker']}",
            f"Status: {c['status']} | Quality: {c['quality_score']:.0f}/100",
            f"Close: {c['harga_terakhir']:.2f} | Entry: {c['entry_level']:.2f} | Stop: {c['stop_level']:.2f} | Target: {c['target_price']:.2f}",
            f"Setup: {c['setup_name']} | R:R plan: {c['risk_reward']:.2f}:1",
            f"RS excess: {c['rs_excess']:.2%} | RS percentile: {c['rs_percentile']:.0f}%",
            f"RSI: {c['rsi']:.1f} | ATR%: {c['atr_pct']:.2%}",
            f"Volume: {c['vol_ratio']:.2f}x MA20 | Vol Z: {c['vol_z']:.2f}",
            f"Median turnover 20D: Rp {c['turnover20']:,.0f}",
            f"Factors: {c['kondisi_detail']}",
            "Berita:",
            *(news if news else ["- Tidak ada berita terbaru dari feed Yahoo."]),
            "---",
        ])

    system_instruction = (
        "Anda adalah reporting layer untuk scanner teknikal saham Indonesia/IDX. "
        "JANGAN mengubah ranking, status, entry, stop, target, atau score yang diberikan program. "
        "Jangan mengarang fundamental atau sentimen bila berita tidak tersedia. "
        "Fokus menjelaskan data secara ringkas.\n\n"
        "FORMAT OUTPUT langsung tanpa pembuka:\n"
        f"### 📊 IDX Stock Report — {mode_tren.upper()} (Top Picks)\n"
        "Tampilkan tabel: Ticker | Status | Quality | RS Percentile | Close | Entry | Stop | Target | Setup.\n"
        "Lalu Market Regime Summary 2-3 bullet.\n"
        "Lalu per ticker: thesis, trigger entry, invalidation/stop, dan risiko utama.\n"
        "Jika status Watchlist, jelaskan apa yang belum terkonfirmasi.\n"
        "Akhiri disclaimer satu kalimat."
    )

    try:
        client = genai.Client(api_key=api_key)
        model = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")
        response = client.models.generate_content(
            model=model,
            contents="\n".join(prompt),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
            ),
        )
        return response.text or fallback_report
    except Exception as e:
        print(f"⚠️ Gemini API gagal ({e}); memakai report deterministik.")
        return fallback_report


def simpan_csv(candidates: list[dict], path: str):
    if not path:
        return
    rows = []
    for c in candidates:
        rows.append({
            "ticker": c["ticker"],
            "date": c["date"],
            "status": c["status"],
            "quality_score": c["quality_score"],
            "rs_percentile": c["rs_percentile"],
            "rs_excess": c["rs_excess"],
            "close": c["harga_terakhir"],
            "entry": c["entry_level"],
            "stop": c["stop_level"],
            "target": c["target_price"],
            "setup": c["setup_name"],
            "rsi": c["rsi"],
            "atr_pct": c["atr_pct"],
            "vol_ratio": c["vol_ratio"],
            "vol_z": c["vol_z"],
            "turnover20": c["turnover20"],
            "hard_pass": c["hard_pass"],
            "hard_fail_reasons": ";".join(c["hard_fail_reasons"]),
            "factors": c["kondisi_detail"],
        })
    out = pd.DataFrame(rows)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    out.to_csv(path, index=False)


def main():
    parser = argparse.ArgumentParser(description="IDX technical confluence scanner")
    parser.add_argument("--trend", choices=["1hari", "1minggu", "1bulan"], default="1hari")
    parser.add_argument("--excel", default="resource/daftar-saham.xlsx")
    parser.add_argument("--period", default="10y", choices=["5y", "10y", "max"])
    parser.add_argument(
        "--min-turnover",
        type=float,
        default=1_000_000_000,
        help="Median turnover harian minimum 20D dalam rupiah (default Rp1 miliar)",
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=100,
        help="Harga minimum heuristic scanner (default Rp100, bukan aturan BEI)",
    )
    parser.add_argument("--capital", type=float, default=0, help="Modal untuk position sizing, opsional")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="Risk per trade dalam persen modal")
    parser.add_argument("--max-position-pct", type=float, default=20.0, help="Maksimum nilai satu posisi terhadap modal")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--output-csv", default="output/idx-screening.csv")
    args = parser.parse_args()

    pool = ambil_semua_ticker_dari_excel(args.excel)
    if not pool:
        return

    download_tickers = list(dict.fromkeys(pool + [IDX_BENCHMARK]))
    print(f"📥 Unduh {args.period} data: {len(pool)} saham IDX + IHSG | Mode {args.trend}...")

    try:
        data_massal = yf.download(
            download_tickers,
            period=args.period,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            repair=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"❌ Download yfinance gagal: {e}")
        return

    ihsg_daily = ekstrak_ticker_frame(data_massal, IDX_BENCHMARK)
    if ihsg_daily.empty:
        print("❌ Data IHSG (^JKSE) tidak tersedia. Scanner dihentikan karena market regime tidak bisa dihitung.")
        return

    print("🌏 Menghitung market breadth dan regime IHSG...")
    breadth = hitung_market_breadth(data_massal, pool)

    try:
        ihsg_tf = siapkan_data_untuk_timeframe(ihsg_daily, args.trend)
        market_regime = analisa_market_regime(ihsg_daily, args.trend, breadth)
    except Exception as e:
        print(f"❌ Gagal menghitung market regime: {e}")
        return

    print(
        f"🇮🇩 Market Regime: {market_regime['regime']} "
        f"({market_regime['score']}/{market_regime['score_max']}) | "
        f"Breadth > EMA50: {market_regime['breadth50']:.1%}"
    )

    print("🔎 Screening saham...")
    candidates = []
    errors = []

    for ticker in pool:
        df_saham = ekstrak_ticker_frame(data_massal, ticker)
        if df_saham.empty:
            continue

        res = analisa_saham_confluence(
            ticker,
            df_saham,
            ihsg_tf,
            args.trend,
            args.min_turnover,
            args.min_price,
        )
        if res.get("error"):
            errors.append(res)
        else:
            candidates.append(res)

    if not candidates:
        print("❌ Tidak ada saham yang memiliki data cukup untuk dianalisis.")
        return

    candidates = finalisasi_score_dan_status(candidates, args.trend, market_regime)

    for c in candidates:
        c["position_size"] = hitung_position_size(
            c, args.capital, args.risk_pct, args.max_position_pct
        )

    simpan_csv(candidates, args.output_csv)

    top_picks, pick_type = ranking_candidates(candidates, args.top)
    status_counts = pd.Series([c["status"] for c in candidates]).value_counts().to_dict()
    print(
        "📈 Hasil universe: "
        f"Strong Buy={status_counts.get('Strong Buy', 0)}, "
        f"Watchlist={status_counts.get('Watchlist', 0)}, "
        f"Avoid={status_counts.get('Avoid', 0)}"
    )

    if not top_picks:
        print("⚠️ Tidak ada kandidat Strong Buy maupun Watchlist yang memenuhi filter dasar.")
        print(f"📄 Full screening tetap disimpan ke: {args.output_csv}")
        return

    if pick_type == "Watchlist":
        print("⚠️ Tidak ada Strong Buy yang lolos; menampilkan Watchlist terbaik yang tetap lolos hard filter.")

    fallback_report = deterministic_report(top_picks, args.trend, market_regime, args.capital)
    report = generate_gemini_report(top_picks, args.trend, market_regime, fallback_report)
    print("\n" + report)

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write("\n" + report + "\n")
                f.write(f"\nFull CSV: `{args.output_csv}`\n")
        except Exception as e:
            print(f"⚠️ Gagal menulis GITHUB_STEP_SUMMARY: {e}")

    print(f"\n📄 Full screening CSV: {args.output_csv}")
    if errors:
        print(f"ℹ️ {len(errors)} ticker dilewati karena data/indikator tidak cukup.")


if __name__ == "__main__":
    main()
