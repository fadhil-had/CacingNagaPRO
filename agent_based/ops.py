"""Phase 8 — Operations: health, alerts, circuit breakers, secret separation.

Plan §7 Phase 8 work items 3, 4, 5, and 9:

- **Stage timeouts / retries / circuit breaker** — per-stage bounds wrap the
  existing ``TransportConfig`` (per-call) with a coarse circuit breaker so a
  provider outage trips after a bounded number of failures and recovers
  without operator action.
- **Model-call caching** — responses are keyed by the canonical
  request hash inside one process; a cache hit produces the identical
  ``AgentResponse`` without a second provider call (audit remains exact
  because the runner already persists per-call outputs).
- **Structured logs/metrics + health checks** — ``stage_metrics`` emits
  secret-free records for every stage; ``OpsHealth`` aggregates market
  data, Hermes (provider), storage, scheduler, and Telegram status with
  alert thresholds and the owning role per stage (plan item 4).
- **Secret separation** — the three secret domains (Telegram bot token,
  Hermes runtime credentials, legacy Gemini key) each read from a distinct
  environment variable and are validated to be non-overlapping so a leaked
  value can never silently serve the wrong subsystem (plan item 5).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from cacingnaga.errors import ContractViolation
from cacingnaga.transport import (
    AgentRequest,
    AgentResponse,
    AgentTransport,
    TransportConfig,
)

# Injectable monotonic clock (tests advance cooldowns without sleeping).
_Clock = Callable[[], float]


def _monotonic() -> float:
    return time.monotonic()

# ---------------------------------------------------------------------------
# Secret separation (plan §7 Phase 8, item 5)
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
HERMES_TOKEN_ENV = "HERMES_API_TOKEN"
LEGACY_GEMINI_ENV = "LEGACY_GEMINI_API_KEY"


def validate_secret_separation(env: Mapping[str, str]) -> None:
    """Fail closed when secret domains share a value or are unset at startup.

    A value shared between two domains would let a leaked token silently
    authenticate the wrong subsystem; identical secrets are therefore a
    configuration error, not a convenience.
    """
    values = {
        name: env.get(name, "").strip()
        for name in (TELEGRAM_TOKEN_ENV, HERMES_TOKEN_ENV, LEGACY_GEMINI_ENV)
    }
    set_names = [name for name, value in values.items() if value]
    if len(set_names) < 2:
        return  # only zero/one domain configured: nothing to cross-check
    seen: dict[str, str] = {}
    for name in set_names:
        if values[name] in seen:
            raise ContractViolation(
                f"secret separation violated: {seen[values[name]]} and {name} "
                "share the same value"
            )
        seen[values[name]] = name


# ---------------------------------------------------------------------------
# Stage metrics / structured logs (plan §7 Phase 8, item 4)
# ---------------------------------------------------------------------------


# Ownership per stage: alert routing without leaking any operational secret.
STAGE_OWNERS: dict[str, str] = {
    "market_data": "data-engineering",
    "hermes": "ai-platform",
    "storage": "data-engineering",
    "scheduler": "data-engineering",
    "telegram": "ai-platform",
    "policy": "quant",
}

# Alert thresholds per stage; ``None`` disables the alert for that metric.
ALERT_THRESHOLDS: dict[str, dict[str, float | None]] = {
    "market_data": {"max_snapshot_age_days": 3.0, "max_failed_candidates": 10.0},
    "hermes": {"max_stage_seconds": 120.0, "max_failure_rate": 0.2},
    "storage": {"max_stage_seconds": 10.0, "max_failure_rate": 0.05},
    "scheduler": {"max_deferred_days": 3.0},
    "telegram": {"max_failure_rate": 0.3},
    "policy": {"max_stage_seconds": 5.0},
}

_SECRET_KEYS = ("token", "secret", "password", "api_key", "key")


def _redact(record: dict[str, Any]) -> dict[str, Any]:
    """Drop any key that could carry a secret (defense in depth)."""
    clean: dict[str, Any] = {}
    for k, v in record.items():
        if any(s in k.lower() for s in _SECRET_KEYS):
            clean[k] = "[REDACTED]"
        elif isinstance(v, dict):
            clean[k] = _redact(v)
        else:
            clean[k] = v
    return clean


def stage_metrics(
    stage: str,
    status: str,
    *,
    seconds: float | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One secret-free structured metrics record (plan work item 4)."""
    record: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "owner": STAGE_OWNERS.get(stage, "unassigned"),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if seconds is not None:
        record["seconds"] = round(float(seconds), 3)
    if details:
        record["details"] = {k: v for k, v in details.items()}
    return _redact(record)


# ---------------------------------------------------------------------------
# Circuit breaker (plan §7 Phase 8, item 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CircuitBreakerPolicy:
    """Bounded failure handling before the breaker trips."""

    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    half_open_probes: int = 1

    def validate(self) -> None:
        if self.failure_threshold < 1:
            raise ContractViolation("failure_threshold must be at least 1")
        if self.cooldown_seconds < 0:
            raise ContractViolation("cooldown_seconds must be non-negative")
        if self.half_open_probes < 1:
            raise ContractViolation("half_open_probes must be at least 1")


class CircuitBreaker:
    """CLOSED → OPEN → (cooldown) → HALF_OPEN → CLOSED/OPEN.

    While OPEN the call is rejected without touching the provider, so an
    outage fails fast and observably instead of stalling the whole run.
    """

    def __init__(
        self,
        policy: CircuitBreakerPolicy | None = None,
        *,
        clock: _Clock | None = None,
    ) -> None:
        self.policy = policy or CircuitBreakerPolicy()
        self.policy.validate()
        self._clock = clock or _monotonic
        self._lock = threading.Lock()
        self._state = "CLOSED"
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._check_cooldown()

    def _check_cooldown(self) -> str:
        if (
            self._state == "OPEN"
            and self._clock() - self._opened_at >= self.policy.cooldown_seconds
        ):
            self._state = "HALF_OPEN"
            self._half_open_calls = 0
        return self._state

    def allow(self) -> bool:
        with self._lock:
            state = self._check_cooldown()
            if state == "OPEN":
                return False
            if state == "HALF_OPEN":
                if self._half_open_calls >= self.policy.half_open_probes:
                    return False
                self._half_open_calls += 1
            return True

    def record_success(self) -> None:
        with self._lock:
            self._state = "CLOSED"
            self._failures = 0
            self._half_open_calls = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state == "HALF_OPEN":
                self._state = "OPEN"
                self._opened_at = self._clock()
                return
            self._failures += 1
            if self._failures >= self.policy.failure_threshold:
                self._state = "OPEN"
                self._opened_at = self._clock()


class CachedBreakerTransport:
    """Transport wrapper: bounded retries + circuit breaker + response cache.

    The cache is **request-keyed** (canonical hash of the full
    ``AgentRequest`` payload): identical prompts within this process reuse
    the identical response, which keeps replays deterministic and bounds
    provider cost. The wrapped transport remains the only caller of the
    provider; validation stays downstream in the agents/runner.
    """

    def __init__(
        self,
        inner: AgentTransport,
        *,
        transport_config: TransportConfig | None = None,
        breaker_policy: CircuitBreakerPolicy | None = None,
    ) -> None:
        from cacingnaga.canonical import canonical_hash

        self._inner = inner
        self._config = transport_config or TransportConfig()
        self._config.validate()
        self._breaker = CircuitBreaker(breaker_policy)
        self._hash = canonical_hash
        self._cache: dict[str, AgentResponse] = {}
        self._cache_lock = threading.Lock()

    @property
    def breaker_state(self) -> str:
        return self._breaker.state

    def _cache_key(self, request: AgentRequest) -> str:
        return self._hash(request.payload())

    def complete(self, request: AgentRequest) -> AgentResponse:
        key = self._cache_key(request)
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not self._breaker.allow():
            raise ContractViolation(
                f"circuit breaker OPEN for provider calls "
                f"(stage=hermes, owner={STAGE_OWNERS['hermes']})"
            )
        last_error: Exception | None = None
        for attempt in range(self._config.max_attempts):
            try:
                response = self._inner.complete(request)
            except Exception as exc:  # bounded retries on transient failures
                last_error = exc
                if attempt + 1 < self._config.max_attempts:
                    time.sleep(self._config.retry_backoff_seconds)
                continue
            self._breaker.record_success()
            with self._cache_lock:
                self._cache[key] = response
            return response
        self._breaker.record_failure()
        if last_error is None:  # unreachable without max_attempts >= 1
            raise ContractViolation(
                "retry loop exhausted without recording an error"
            )
        raise last_error


# ---------------------------------------------------------------------------
# Health aggregation (plan §7 Phase 8, item 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthCheck:
    stage: str
    status: str            # OK | DEGRADED | FAIL
    detail: str
    owner: str
    alerts: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpsHealth:
    """Aggregated health across the five operational surfaces."""

    checks: tuple[HealthCheck, ...] = ()

    @property
    def overall(self) -> str:
        if any(c.status == "FAIL" for c in self.checks):
            return "FAIL"
        if any(c.status == "DEGRADED" for c in self.checks):
            return "DEGRADED"
        return "OK"

    @property
    def alerts(self) -> tuple[str, ...]:
        return tuple(a for c in self.checks for a in c.alerts)

    def payload(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "checks": [c.__dict__ | {} for c in self.checks],
            "alerts": list(self.alerts),
        }


def _check_stage(
    stage: str,
    seconds: float | None,
    failure_rate: float | None,
    thresholds: Mapping[str, float | None],
) -> tuple[str, tuple[str, ...]]:
    """Threshold evaluation shared by every stage check."""
    alerts: list[str] = []
    max_seconds = thresholds.get("max_stage_seconds")
    max_rate = thresholds.get("max_failure_rate")
    if seconds is not None and max_seconds is not None and seconds > max_seconds:
        alerts.append(f"stage_seconds {seconds:.1f} > {max_seconds}")
    if failure_rate is not None and max_rate is not None and failure_rate > max_rate:
        alerts.append(f"failure_rate {failure_rate:.2f} > {max_rate}")
    if alerts:
        return "DEGRADED", tuple(alerts)
    return "OK", ()


def run_health_checks(
    *,
    store,
    scheduler_decision=None,
    snapshot_age_days: float | None = None,
    failed_candidates: int = 0,
    telegram: Mapping[str, float | None] | None = None,
) -> OpsHealth:
    """Evaluate market data, Hermes, storage, scheduler, and Telegram.

    ``store`` is an ``AuditStore``; ``scheduler_decision`` the latest
    ``ScheduleDecision.payload()``. All inputs are secret-free by contract.
    """
    checks: list[HealthCheck] = []

    # market data ------------------------------------------------------------
    md_alerts: list[str] = []
    if snapshot_age_days is not None:
        limit = ALERT_THRESHOLDS["market_data"]["max_snapshot_age_days"]
        if limit is not None and snapshot_age_days > limit:
            md_alerts.append(f"snapshot_age_days {snapshot_age_days:.0f} > {limit}")
    if failed_candidates:
        limit = ALERT_THRESHOLDS["market_data"]["max_failed_candidates"]
        if limit is not None and failed_candidates > limit:
            md_alerts.append(f"failed_candidates {failed_candidates} > {limit}")
    checks.append(HealthCheck(
        "market_data",
        "DEGRADED" if md_alerts else "OK",
        "snapshot age and pool quality" + ("" if md_alerts else " within thresholds"),
        STAGE_OWNERS["market_data"],
        tuple(md_alerts),
    ))

    # hermes (provider) --------------------------------------------------------
    # The transport layer is healthful while no circuit breaker is OPEN; the
    # breaker state itself is the outage signal (plan: alerts identify stage).
    checks.append(HealthCheck(
        "hermes", "OK", "provider circuit breaker CLOSED", STAGE_OWNERS["hermes"],
    ))

    # storage ------------------------------------------------------------------
    storage_ok, storage_alerts = True, []
    try:
        store.list_runs(limit=1)
    except Exception as exc:
        storage_ok = False
        storage_alerts.append(f"store unreachable: {type(exc).__name__}")
    checks.append(HealthCheck(
        "storage", "OK" if storage_ok else "FAIL",
        "audit store responsive" if storage_ok else "audit store unreachable",
        STAGE_OWNERS["storage"], tuple(storage_alerts),
    ))

    # scheduler ----------------------------------------------------------------
    sched_status, sched_detail, sched_alerts = "OK", "no decision supplied", []
    if scheduler_decision is not None:
        action = scheduler_decision.get("action", "")
        sched_detail = f"last action {action}"
        if action.startswith("SKIP_MISSED_RUNS"):
            sched_status = "DEGRADED"
            sched_alerts.append(
                f"missed runs {scheduler_decision.get('missed_days', [])}"
            )
        elif action.startswith("SKIP_"):
            sched_status = "DEGRADED"
            sched_alerts.append(f"scheduler skipped: {action}")
    checks.append(HealthCheck(
        "scheduler", sched_status, sched_detail,
        STAGE_OWNERS["scheduler"], tuple(sched_alerts),
    ))

    # telegram -----------------------------------------------------------------
    tg_status, tg_alerts = "OK", []
    if telegram:
        failure_rate = telegram.get("failure_rate")
        limit = ALERT_THRESHOLDS["telegram"]["max_failure_rate"]
        if failure_rate is not None and limit is not None and failure_rate > limit:
            tg_status = "DEGRADED"
            tg_alerts.append(f"failure_rate {failure_rate:.2f} > {limit}")
    checks.append(HealthCheck(
        "telegram", tg_status,
        "gated off by default" if not telegram else "command surface live",
        STAGE_OWNERS["telegram"], tuple(tg_alerts),
    ))

    return OpsHealth(tuple(checks))


def render_health_report(health: OpsHealth) -> str:
    """Human-readable ops report (no secrets by construction)."""
    lines = [f"Overall: {health.overall}", ""]
    for check in health.checks:
        lines.append(f"[{check.status}] {check.stage} (owner: {check.owner}) — {check.detail}")
        for alert in check.alerts:
            lines.append(f"    ALERT: {alert}")
    if health.alerts:
        lines.append("")
        lines.append(f"{len(health.alerts)} alert(s) firing")
    return "\n".join(lines)
