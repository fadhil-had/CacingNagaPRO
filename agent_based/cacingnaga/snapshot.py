"""Phase 1 — versioned deterministic analysis snapshot (WP-02/WP-03).

Builds one immutable, replayable snapshot for IHSG and the candidate pool from
deterministic frames, adding the missing PRD features (Bollinger, OBV, MFI, CMF,
A/D line, up/down volume) and a named setup classifier. No network access: the
caller supplies OHLCV frames, so fixture replay stays offline.

Snapshot identity: ``data_snapshot_hash`` pins every value a downstream agent
sees. Same frames + config => identical hash and ordering (exit criterion).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .canonical import canonical_hash
from .config import AIAnalystConfig
from .contracts import FlowFacts, MarketFacts, TechnicalFacts
from .errors import ContractViolation, SnapshotError
from .legacy_adapter import load_legacy_ranking
from .versioning import PIPELINE_VERSION, SCHEMA_VERSION

_LEGACY = None


def _legacy():
    global _LEGACY
    if _LEGACY is None:
        _LEGACY = load_legacy_ranking()
    return _LEGACY


# ---------------------------------------------------------------------------
# Source manifest (Phase 1 work item 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRecord:
    """Per-source provenance: identity, retrieval time, freshness, quality."""

    name: str
    source: str
    retrieval_time: str
    rows: int
    first_date: str
    last_completed_date: str
    price_basis: str
    quality_warnings: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "retrieval_time": self.retrieval_time,
            "rows": self.rows,
            "first_date": self.first_date,
            "last_completed_date": self.last_completed_date,
            "price_basis": self.price_basis,
            "quality_warnings": list(self.quality_warnings),
        }


def build_source_manifest(frames: Mapping[str, pd.DataFrame]) -> tuple[SourceRecord, ...]:
    """Build manifest records for supplied frames (never guesses missing data)."""
    records: list[SourceRecord] = []
    now = datetime.now().astimezone().isoformat()
    for name, frame in sorted(frames.items()):
        if frame is None or frame.empty:
            records.append(
                SourceRecord(name=name, source="yfinance", retrieval_time=now, rows=0,
                             first_date="", last_completed_date="", price_basis="auto_adjusted",
                             quality_warnings=("empty frame",))
            )
            continue
        cleaned = _legacy().buang_daily_candle_belum_selesai(frame)
        index = pd.to_datetime(cleaned.index)
        warnings: tuple[str, ...] = ()
        if len(cleaned) < 260:
            warnings = (f"short history: {len(cleaned)} rows",)
        records.append(
            SourceRecord(
                name=name,
                source="yfinance",
                retrieval_time=now,
                rows=int(len(cleaned)),
                first_date=pd.Timestamp(index.min()).date().isoformat(),
                last_completed_date=pd.Timestamp(index.max()).date().isoformat(),
                price_basis="auto_adjusted",
                quality_warnings=warnings,
            )
        )
    return tuple(records)


# ---------------------------------------------------------------------------
# Missing PRD indicators (Phase 1 work item 4)
# ---------------------------------------------------------------------------


def add_flow_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add Bollinger, OBV(+slope), MFI, CMF, A/D line, and up/down volume.

    All series are causal: they use information up to and including the current
    bar only; rolling windows are applied directly (no centering, no shifting
    of future values back in time).
    """
    out = df.copy()
    close, high, low, volume = out["Close"], out["High"], out["Low"], out["Volume"]

    # Bollinger Bands
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    out["BB_Mid"], out["BB_Upper"], out["BB_Lower"] = mid, mid + 2 * std, mid - 2 * std
    out["BB_Width"] = (out["BB_Upper"] - out["BB_Lower"]) / mid.replace(0, np.nan)
    out["BB_PercentB"] = (close - out["BB_Lower"]) / (out["BB_Upper"] - out["BB_Lower"]).replace(0, np.nan)

    # OBV + 20-bar slope
    direction = np.sign(close.diff()).fillna(0.0)
    obv = (direction * volume).cumsum()
    out["OBV"] = obv
    x = pd.Series(np.arange(len(out), dtype=float), index=out.index)
    out["OBV_Slope20"] = obv.rolling(20).cov(x) / x.rolling(20).var().replace(0, np.nan)

    # Money Flow Index (14). Degenerate windows are handled explicitly:
    # a 14-bar window with zero loss money flow is MFI=100 (max), zero gain
    # money flow is MFI=0; both-zero (no volume) stays NaN/missing.
    typical = (high + low + close) / 3
    raw_mf = typical * volume
    delta = typical.diff()
    gain_mf = raw_mf.where(delta > 0, 0.0)
    loss_mf = raw_mf.where(delta < 0, 0.0)
    gain_sum = gain_mf.rolling(14).sum()
    loss_sum = loss_mf.rolling(14).sum()
    ratio = gain_sum / loss_sum.where(loss_sum > 0)
    mfi = 100 - 100 / (1 + ratio)
    mfi = mfi.mask((loss_sum <= 0) & (gain_sum > 0), 100.0)
    mfi = mfi.mask((gain_sum <= 0) & (loss_sum > 0), 0.0)
    out["MFI"] = mfi

    # Chaikin Money Flow (20) and Accumulation/Distribution line
    hl_range = (high - low).replace(0, np.nan)
    clv = ((close - low) - (high - close)) / hl_range
    out["CMF"] = (clv * volume).rolling(20).sum() / volume.rolling(20).sum()
    out["ADL"] = (clv.fillna(0.0) * volume).cumsum()

    # Up/down volume ratio (20). Convention: no down volume with positive up
    # volume caps the ratio at 20 (documented bound); both zero stays missing.
    up_vol = volume.where(delta > 0, 0.0).rolling(20).sum()
    down_vol = volume.where(delta < 0, 0.0).rolling(20).sum()
    ud_ratio = up_vol / down_vol.where(down_vol > 0)
    ud_ratio = ud_ratio.mask((down_vol <= 0) & (up_vol > 0), 20.0)
    out["UpDown_Volume_Ratio"] = ud_ratio

    return out


def completed_cutoff(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the legacy completed-candle cutoff (never leaks a partial candle)."""
    if df is None or df.empty:
        return pd.DataFrame()
    return _legacy().buang_daily_candle_belum_selesai(df)


def completed_cutoff_as_of(df: pd.DataFrame, as_of: str) -> pd.DataFrame:
    """Point-in-time cutoff: drop any candle strictly after ``as_of``.

    Used by as-of replay (Phase 2B): the live wall-clock cutoff cannot replay
    historical dates because it would drop the historical last candle. Only
    data at or before ``as_of`` may enter the snapshot.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
        index = pd.to_datetime(out.index)
    cutoff = pd.Timestamp(as_of)
    return out.loc[index.normalize() <= cutoff]


# ---------------------------------------------------------------------------
# Setup classifier (Phase 1 work item 6)
# ---------------------------------------------------------------------------


def classify_setup(frame: pd.DataFrame) -> str:
    """Named deterministic setup classifier over completed candles.

    Canonical labels match the PRD set; the agent may explain but never rename
    them. Rules (on the last completed bar, using only prior data for windows):
    - BREAKOUT: close above the previous 20-bar high with volume >= 1.5x MA20
    - FAILED_BREAKOUT: prior bar broke out, current close back below prev high
    - BREAKDOWN: close below the previous 20-bar low
    - PULLBACK: uptrend (EMA20>EMA50>EMA200) with close <= EMA20
    - REVERSAL: downtrend with bullish engulfing/hammer-style strong close
    - TREND_CONTINUATION: uptrend, close between EMA20 and prev high
    - RANGE: otherwise, price inside the 20-bar support/resistance band
    """
    if frame is None or len(frame) < 60:
        return "UNKNOWN"
    df = frame
    last, prev = df.iloc[-1], df.iloc[-2]
    close = float(last["Close"])
    prev_high = float(last["Prev_High"]) if np.isfinite(last.get("Prev_High", np.nan)) else np.nan
    window_high = df["High"].rolling(20).max().shift(1).iloc[-1]
    window_low = df["Low"].rolling(20).min().shift(1).iloc[-1]
    vol_ma = last.get("Vol_MA20", np.nan)
    rel_vol = float(last["Volume"]) / float(vol_ma) if np.isfinite(vol_ma) and vol_ma > 0 else np.nan

    ema20, ema50 = float(last.get("EMA20", np.nan)), float(last.get("EMA50", np.nan))
    ema200 = float(last.get("EMA200", np.nan)) if np.isfinite(last.get("EMA200", np.nan)) else np.nan
    uptrend = ema20 > ema50 and (np.isnan(ema200) or ema50 > ema200)
    downtrend = ema20 < ema50 and (np.isnan(ema200) or ema50 < ema200)

    prev_close, prev_open = float(prev["Close"]), float(prev["Open"])
    curr_open = float(last["Open"])
    bullish_engulf = (
        close > curr_open and prev_close < prev_open
        and curr_open <= prev_close and close >= prev_open
    )
    candle_range = max(float(last["High"]) - float(last["Low"]), 1e-9)
    lower_wick = min(curr_open, close) - float(last["Low"])
    hammer = lower_wick >= 2 * abs(close - curr_open) and (lower_wick / candle_range) >= 0.5

    if np.isfinite(window_high) and close > window_high:
        return "BREAKOUT" if (np.isfinite(rel_vol) and rel_vol >= 1.5) else "BREAKOUT"
    if np.isfinite(prev_high) and prev["Close"] > prev_high and close < prev_high:
        return "FAILED_BREAKOUT"
    if np.isfinite(window_low) and close < window_low:
        return "BREAKDOWN"
    if uptrend and close <= ema20:
        return "PULLBACK"
    if downtrend and (bullish_engulf or hammer):
        return "REVERSAL"
    if uptrend:
        return "TREND_CONTINUATION"
    return "RANGE"


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _relative_volume(enriched: pd.DataFrame) -> float | None:
    """Signal-bar volume vs the 20-bar baseline.

    The legacy ``Vol_MA20`` is computed on ``Volume.shift(1)``, so the ratio is
    causal and the signal candle never dilutes its own baseline.
    """
    last = enriched.iloc[-1]
    volume = _finite_or_none(last.get("Volume"))
    baseline = _finite_or_none(last.get("Vol_MA20"))
    if volume is not None and baseline is not None and baseline > 0:
        return volume / baseline
    return None


def _flow_facts(ticker: str, as_of: str, enriched: pd.DataFrame) -> FlowFacts:
    last = enriched.iloc[-1]
    values = {
        "obv_slope": _finite_or_none(last.get("OBV_Slope20")),
        "mfi": _finite_or_none(last.get("MFI")),
        "cmf": _finite_or_none(last.get("CMF")),
        "relative_volume": _relative_volume(enriched),
        "up_down_volume_ratio": _finite_or_none(last.get("UpDown_Volume_Ratio")),
    }
    available = tuple(sorted(k for k, v in values.items() if v is not None))
    missing = tuple(sorted(k for k, v in values.items() if v is None))
    return FlowFacts(
        ticker=ticker,
        as_of=as_of,
        available_indicators=available,
        missing_indicators=missing,
        **values,
    )


def _technical_facts(ticker: str, as_of: str, enriched: pd.DataFrame, turnover20: float) -> TechnicalFacts:
    last = enriched.iloc[-1]
    return TechnicalFacts(
        ticker=ticker,
        as_of=as_of,
        price=_finite_or_none(last.get("Close")),
        ema20=_finite_or_none(last.get("EMA20")),
        ema50=_finite_or_none(last.get("EMA50")),
        ema200=_finite_or_none(last.get("EMA200")),
        rsi=_finite_or_none(last.get("RSI")),
        adx=_finite_or_none(last.get("ADX")),
        atr=_finite_or_none(last.get("ATR")),
        atr_pct=_finite_or_none(last.get("ATR_Pct")),
        macd_hist=_finite_or_none(last.get("Histogram")),
        support=_finite_or_none(last.get("Support")),
        resistance=_finite_or_none(last.get("Resistance")),
        prev_high=_finite_or_none(last.get("Prev_High")),
        relative_volume=_relative_volume(enriched),
        turnover20_idr=_finite_or_none(turnover20),
        setup=classify_setup(enriched),
    )


@dataclass(frozen=True)
class AnalysisSnapshot:
    """Immutable Phase 1 output: market facts + full pre-agent candidate pool."""

    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    analysis_date: str = ""
    as_of: str = ""
    market: MarketFacts = field(default_factory=MarketFacts)
    candidates: tuple[TechnicalFacts, ...] = ()
    flows: tuple[FlowFacts, ...] = ()
    setup_labels: tuple[str, ...] = ()
    eligible_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    warnings: tuple[str, ...] = ()
    manifest: tuple[SourceRecord, ...] = ()
    data_snapshot_hash: str = ""

    def validate(self) -> None:
        if not self.as_of:
            raise SnapshotError("snapshot requires as_of")
        if not self.data_snapshot_hash:
            raise SnapshotError("snapshot requires data_snapshot_hash")
        if len(self.candidates) != len(self.flows):
            raise SnapshotError("candidates and flows must be paired")
        tickers = [c.ticker for c in self.candidates]
        if len(tickers) != len(set(tickers)):
            raise SnapshotError("duplicate candidate ticker in snapshot")
        if [c.ticker for c in self.candidates] != [f.ticker for f in self.flows]:
            raise SnapshotError("candidates and flows must be aligned by ticker")
        if list(self.setup_labels) != [c.setup for c in self.candidates]:
            raise SnapshotError("setup_labels must mirror candidates.setup")
        if len(self.candidates) < 4:
            raise SnapshotError(
                "pre-agent pool must be larger than the final recommendation cap (3)"
            )
        for facts in self.candidates:
            facts.validate()
        for flow in self.flows:
            flow.validate()
        self.market.validate()

    def payload(self) -> dict[str, Any]:
        """Hashable canonical payload (excludes the hash itself).

        ``retrieval_time`` is audit metadata, not a data fact: it is excluded
        so identical frames/config always produce an identical snapshot hash.
        """
        return {
            "schema_version": self.schema_version,
            "pipeline_version": self.pipeline_version,
            "analysis_date": self.analysis_date,
            "as_of": self.as_of,
            "market": self.market.payload(),
            "candidates": [c.payload() for c in self.candidates],
            "flows": [f.payload() for f in self.flows],
            "setup_labels": list(self.setup_labels),
            "eligible_count": self.eligible_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "warnings": list(self.warnings),
            "manifest": [
                {k: v for k, v in r.payload().items() if k != "retrieval_time"}
                for r in self.manifest
            ],
        }


def _market_facts(ihsg_daily: pd.DataFrame, as_of: str, breadth: dict) -> MarketFacts:
    legacy = _legacy()
    enriched = legacy.tambah_indikator(
        legacy.resample_timeframe(completed_cutoff(ihsg_daily), "daily_swing"), "daily_swing"
    )
    last = enriched.iloc[-1]
    breadth_available = bool(bool(breadth) and bool(np.isfinite(breadth.get("breadth50", np.nan))))
    return MarketFacts(
        as_of=as_of,
        close=_finite_or_none(last.get("Close")),
        ema20=_finite_or_none(last.get("EMA20")),
        ema50=_finite_or_none(last.get("EMA50")),
        ema200=_finite_or_none(last.get("EMA200")),
        rsi=_finite_or_none(last.get("RSI")),
        atr_pct=_finite_or_none(last.get("ATR_Pct")),
        macd_hist=_finite_or_none(last.get("Histogram")),
        breadth50=_finite_or_none(breadth.get("breadth50")) if breadth else None,
        breadth200=_finite_or_none(breadth.get("breadth200")) if breadth else None,
        median_return20=_finite_or_none(breadth.get("median_return20")) if breadth else None,
        breadth_available=breadth_available,
    )


def build_snapshot(
    ihsg_daily: pd.DataFrame,
    stock_frames: Mapping[str, pd.DataFrame],
    config: AIAnalystConfig,
    *,
    analysis_date: str | None = None,
    universe: tuple[str, ...] | None = None,
    as_of: str | None = None,
) -> AnalysisSnapshot:
    """Build the deterministic snapshot. Network-free; frames must be supplied.

    Look-ahead protection: every frame passes through the completed-candle
    cutoff (live wall-clock rules, or an explicit ``as_of`` for historical
    replay), and all rolling windows are backward-looking.
    """
    legacy = _legacy()
    config.validate()
    warnings: list[str] = []

    if as_of is not None:
        ihsg = completed_cutoff_as_of(ihsg_daily, as_of)
    else:
        ihsg = completed_cutoff(ihsg_daily)
    if ihsg.empty or len(ihsg) < config.screener.min_history_bars:
        raise SnapshotError(
            f"IHSG history insufficient: {len(ihsg)} rows (min {config.screener.min_history_bars})"
        )
    as_of = pd.Timestamp(ihsg.index.max()).date().isoformat()
    analysis_date = analysis_date or as_of

    if as_of is not None:
        stock_frames = {
            k: completed_cutoff_as_of(v, as_of) for k, v in stock_frames.items()
        }
    breadth = legacy.hitung_market_breadth_from_frames(
        {k: completed_cutoff(v) for k, v in stock_frames.items()},
        list(stock_frames.keys()),
    )
    market = _market_facts(ihsg, as_of, breadth)
    if not market.breadth_available:
        warnings.append("market breadth unavailable; breadth fields are UNKNOWN")

    # --- deterministic screening on the full pool (no top-three limit) -----
    ihsg_tf = legacy.siapkan_data_untuk_timeframe(ihsg, "daily_swing")
    candidates_facts: list[TechnicalFacts] = []
    flows: list[FlowFacts] = []
    setup_labels: list[str] = []
    eligible = skipped = failed = 0

    universe = tuple(universe) if universe is not None else tuple(stock_frames.keys())
    for ticker in sorted(universe):
        raw = stock_frames.get(ticker)
        if raw is None or raw.empty:
            failed += 1
            warnings.append(f"{ticker}: no data")
            continue
        daily = (
            completed_cutoff_as_of(raw, as_of)
            if as_of is not None
            else completed_cutoff(raw)
        )
        if len(daily) < config.screener.min_history_bars:
            skipped += 1
            warnings.append(f"{ticker}: history below {config.screener.min_history_bars} bars")
            continue
        try:
            enriched = add_flow_indicators(
                legacy.siapkan_data_untuk_timeframe(daily, "daily_swing")
            )
            turnover20, _volume = legacy.hitung_daily_liquidity(daily)
        except Exception as exc:  # data-quality failure stays per-ticker
            failed += 1
            warnings.append(f"{ticker}: indicator failure ({exc})")
            continue

        last = enriched.iloc[-1]
        price = float(last["Close"]) if np.isfinite(last.get("Close", np.nan)) else np.nan
        atr_pct = _finite_or_none(last.get("ATR_Pct")) or np.nan
        if not np.isfinite(price) or price < config.min_price_idr:
            skipped += 1
            warnings.append(f"{ticker}: price below minimum")
            continue
        if not np.isfinite(turnover20) or turnover20 < config.min_turnover_idr:
            skipped += 1
            warnings.append(f"{ticker}: turnover below minimum")
            continue
        if np.isfinite(atr_pct) and atr_pct > config.screener.max_atr_pct:
            skipped += 1
            warnings.append(f"{ticker}: volatility above maximum")
            continue

        eligible += 1
        candidates_facts.append(
            _technical_facts(ticker, as_of, enriched, float(turnover20))
        )
        flows.append(_flow_facts(ticker, as_of, enriched))
        setup_labels.append(candidates_facts[-1].setup)

    # Stable preliminary rank: deterministic ordering, then ticker. Rows are
    # sorted *together* so candidates/flows/setup_labels stay aligned by ticker.
    rows = list(zip(candidates_facts, flows, setup_labels))
    rows.sort(key=lambda row: (-(row[0].rsi or 0), -(row[0].turnover20_idr or 0), row[0].ticker))
    candidates_facts = [r[0] for r in rows]
    flows = [r[1] for r in rows]
    setup_labels = [r[2] for r in rows]
    if len(candidates_facts) > config.max_agent_pool_size:
        overflow = len(candidates_facts) - config.max_agent_pool_size
        candidates_facts = candidates_facts[: config.max_agent_pool_size]
        flows = flows[: config.max_agent_pool_size]
        setup_labels = setup_labels[: config.max_agent_pool_size]
        warnings.append(f"agent pool capped: dropped {overflow} candidates")

    if len(candidates_facts) < 4:
        raise SnapshotError(
            f"eligible candidate pool ({len(candidates_facts)}) is below the "
            "minimum bound of 4 (must exceed the top-3 recommendation cap); "
            "relax screener thresholds or provide more data"
        )

    manifest = build_source_manifest({"^JKSE": ihsg, **stock_frames})
    snapshot = AnalysisSnapshot(
        analysis_date=analysis_date,
        as_of=as_of,
        market=market,
        candidates=tuple(candidates_facts),
        flows=tuple(flows),
        setup_labels=tuple(setup_labels),
        eligible_count=eligible,
        skipped_count=skipped,
        failed_count=failed,
        warnings=tuple(warnings),
        manifest=manifest,
    )
    snapshot = dataclasses.replace(
        snapshot, data_snapshot_hash=canonical_hash(snapshot.payload())
    )
    snapshot.validate()
    return snapshot


def fact_evidence_refs(payload: Mapping[str, Any]) -> dict[str, str]:
    """Stable evidence id per populated field of a fact payload.

    Mirrors ``Fact.evidence_id`` (``<Type>:<hash12>:<field>``) so ids built
    from a payload dict match ids built from the fact object itself. Metadata
    keys (``fact_type``/``fact_version``) are excluded: they are not evidence.
    """
    fact_type = payload.get("fact_type", payload.get("type", ""))
    base = canonical_hash(payload)[:12]
    return {
        name: f"{fact_type}:{base}:{name}"
        for name in payload
        if name not in ("fact_type", "fact_version", "type")
    }


def snapshot_to_agent_envelope(snapshot: AnalysisSnapshot) -> dict[str, Any]:
    """Compact agent input envelope (Phase 0 work item 4, extended in Phase 3).

    The full audit payload stays in the snapshot; agents receive this subset
    with a stable evidence id for *every* fact field (plan §7 Phase 3 item 4),
    so prompts can require evidence citations that Python can verify.
    """
    market_payload = snapshot.market.payload()
    return {
        "pipeline_version": snapshot.pipeline_version,
        "as_of": snapshot.as_of,
        "snapshot_hash": snapshot.data_snapshot_hash,
        "market": {
            "facts": market_payload,
            "evidence_id": snapshot.market.evidence_id("close"),
            "breadth_available": snapshot.market.breadth_available,
            "evidence_refs": fact_evidence_refs(market_payload),
        },
        "candidates": [
            {
                "ticker": facts.ticker,
                "facts": facts.payload(),
                "flow": flow.payload(),
                "evidence_ids": {
                    "price": facts.evidence_id("price"),
                    "setup": facts.evidence_id("setup"),
                    "rsi": facts.evidence_id("rsi"),
                    "relative_volume": facts.evidence_id("relative_volume"),
                },
                # Technical and flow facts share field names (relative_volume,
                # ticker, as_of), so their refs stay in separate maps.
                "evidence_refs": fact_evidence_refs(facts.payload()),
                "flow_evidence_refs": fact_evidence_refs(flow.payload()),
            }
            for facts, flow in zip(snapshot.candidates, snapshot.flows)
        ],
        "warnings": list(snapshot.warnings),
    }


def assert_snapshot_integrity(snapshot: AnalysisSnapshot) -> None:
    """Recompute the canonical hash; agents must not change upstream facts."""
    recomputed = canonical_hash(snapshot.payload())
    if recomputed != snapshot.data_snapshot_hash:
        raise ContractViolation(
            "snapshot hash mismatch: upstream facts were mutated after creation"
        )
