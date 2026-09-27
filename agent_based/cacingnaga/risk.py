"""Phase 2 — long-only risk-plan calculator (WP-04, plan §7 Phase 2).

Python owns every number: entry zone, structural stop, TP1/TP2, risk per
share, and reward/risk — computed from immutable `TechnicalFacts` with IDX tick
rounding and the 2–5% stop / 3–10% target mandates. Agents may challenge the
plan's consistency; they can never alter its values (plan §5 boundary rules).

Invalid plans fail closed: `RiskPlanCalculator.calculate` returns ``None`` with
machine-readable reasons, and only a *valid* plan can back a `READY` decision.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .config import PolicyConfig
from .contracts import RiskLevels, TechnicalFacts
from .legacy_adapter import get_legacy_ranking


@dataclass(frozen=True)
class RiskPlanResult:
    """Outcome of one deterministic risk-plan calculation."""

    levels: RiskLevels | None
    valid: bool
    reject_reasons: tuple[str, ...] = ()
    # Documented formulas/rules actually applied (audit requirement §6.7).
    rules_applied: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "levels": self.levels.payload() if self.levels is not None else None,
            "valid": self.valid,
            "reject_reasons": list(self.reject_reasons),
            "rules_applied": list(self.rules_applied),
        }


def _positive(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


class RiskPlanCalculator:
    """Deterministic long-only entry/stop/target calculator (v1 policy)."""

    version = "AI_TEAM_DAILY_V1_RISK_1"

    def __init__(self, config: PolicyConfig) -> None:
        self._config = config

    def calculate(self, facts: TechnicalFacts) -> RiskPlanResult:
        """Return a validated plan, or ``None`` + reasons (fail-closed)."""
        cfg = self._config.risk
        rules: list[str] = [
            "entry_zone = [support, round_up(price)] clamped to [price, min(resistance, price*(1+entry_max_above_price_pct))]",
            "stop = round_down(max(support*(1-stop_buffer_pct), price*(1-max_stop_pct)))",
            "tp1 = round_down(entry_mid*(1+rr_tp1*stop_pct)) capped at entry*(1+max_target_pct)",
            "tp2 = round_down(entry_mid*(1+rr_tp2*stop_pct)) capped at entry*(1+max_target_pct)",
            "IDX tick rounding via fraksi_harga_idx; stop_pct/target_pct mandates enforced",
        ]
        price = _positive(facts.price)
        support = _positive(facts.support)
        resistance = _positive(facts.resistance)
        atr = _positive(facts.atr)

        # --- mandatory inputs -------------------------------------------------
        missing = [
            name
            for name, value in (
                ("price", price), ("support", support), ("resistance", resistance), ("atr", atr),
            )
            if value is None
        ]
        if missing:
            return RiskPlanResult(None, False, tuple(
                f"missing mandatory fact: {name}" for name in missing
            ), tuple(rules))

        legacy = get_legacy_ranking()
        round_idx_price = legacy.round_idx_price

        # --- entry zone (never above the documented ceiling) -------------------
        entry_low = max(price, support)                      # longs buy at/above support
        entry_ceiling = min(resistance, price * (1 + cfg.entry_max_above_price_pct))
        entry_high = min(price, entry_ceiling)               # v1 fills near the last close
        if entry_high < entry_low:
            return RiskPlanResult(None, False, (
                "entry zone inverted: price is below support (broken structure)",
            ), tuple(rules))

        entry_low_tick = round_idx_price(entry_low, "down")
        entry_high_tick = round_idx_price(entry_high, "up")
        if not entry_low_tick or not entry_high_tick or entry_low_tick > entry_high_tick:
            return RiskPlanResult(None, False, (
                "entry zone collapsed after IDX tick rounding",
            ), tuple(rules))

        # --- structural stop, clamped to the 2–5% mandate ----------------------
        structural_stop = support * (1 - cfg.stop_buffer_pct)
        atr_stop = price - cfg.stop_atr_multiplier * atr
        stop_raw = max(structural_stop, atr_stop, price * (1 - cfg.max_stop_pct))
        stop_tick = round_idx_price(stop_raw, "down")
        if not stop_tick or stop_tick >= entry_high_tick:
            return RiskPlanResult(None, False, (
                "stop is not below entry after rounding/mandate clamp",
            ), tuple(rules))

        entry_mid = (entry_low_tick + entry_high_tick) / 2
        stop_pct = (entry_mid - stop_tick) / entry_mid
        if stop_pct < cfg.min_stop_pct:
            # Respect the minimum-risk mandate without rounding the stop UP.
            stop_tick = round_idx_price(entry_mid * (1 - cfg.min_stop_pct), "down")
            stop_pct = (entry_mid - stop_tick) / entry_mid
            rules.append("stop widened to the 2% minimum-risk mandate")
        if not stop_tick or stop_tick <= 0 or stop_pct > cfg.max_stop_pct + 1e-9:
            return RiskPlanResult(None, False, (
                f"stop_pct {stop_pct:.4f} outside [{cfg.min_stop_pct}, {cfg.max_stop_pct}]",
            ), tuple(rules))

        # --- targets: R-multiples of the stop, capped by the 10% mandate ------
        levels: list[float | None] = []
        for rr, cap_note in ((cfg.rr_tp1, "tp1"), (cfg.rr_tp2, "tp2")):
            target = round_idx_price(entry_mid * (1 + rr * stop_pct), "down")
            cap = round_idx_price(entry_mid * (1 + cfg.max_target_pct), "down")
            if not target or target <= entry_high_tick:
                return RiskPlanResult(None, False, (
                    f"{cap_note} must clear the entry zone after rounding",
                ), tuple(rules))
            if target > cap:
                target = cap
                rules.append(f"{cap_note} capped at the 10% target mandate")
            if target <= entry_mid:
                return RiskPlanResult(None, False, (
                    f"{cap_note} at/below entry after mandate cap",
                ), tuple(rules))
            levels.append(target)
        tp1, tp2 = levels  # type: ignore[misc]
        if tp2 is None or tp1 is None or tp2 <= tp1:
            return RiskPlanResult(None, False, "TP2 must exceed TP1", tuple(rules))

        # --- final cross-checks (plan §2: fail closed) -------------------------
        actual_stop_pct = (entry_mid - stop_tick) / entry_mid
        risk_per_share = entry_mid - stop_tick
        rr_tp1_value = (tp1 - entry_mid) / risk_per_share
        rr_tp2_value = (tp2 - entry_mid) / risk_per_share
        if not (cfg.min_target_pct <= (tp1 - entry_mid) / entry_mid <= cfg.max_target_pct):
            return RiskPlanResult(None, False, (
                f"TP1 target_pct outside [{cfg.min_target_pct}, {cfg.max_target_pct}]",
            ), tuple(rules))
        if rr_tp1_value < 1.0 or rr_tp2_value <= rr_tp1_value:
            return RiskPlanResult(None, False, (
                "reward/risk ordering invalid (RR1 < 1 or RR2 <= RR1)",
            ), tuple(rules))

        invalidation = (
            f"daily close below {stop_tick:.0f} or two closes below "
            f"{entry_low_tick:.0f} (support loss)"
        )
        risk = RiskLevels(
            ticker=facts.ticker,
            entry_low=entry_low_tick,
            entry_high=entry_high_tick,
            stop_loss=stop_tick,
            tp1=tp1,
            tp2=tp2,
            maximum_holding_days=cfg.max_holding_days,
            invalidation=invalidation,
            risk_per_share=risk_per_share,
            risk_reward_tp1=rr_tp1_value,
            risk_reward_tp2=rr_tp2_value,
        )
        risk.validate()  # contract ordering rules must agree with the calculator
        return RiskPlanResult(risk, True, (), tuple(rules))
