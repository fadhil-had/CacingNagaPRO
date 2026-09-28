"""Phase 8 — Scheduling, Operations, and Rollout (plan §7 Phase 8, items 1, 2, 7).

One reliable post-market daily run. The scheduler is a thin, deterministic
gate in front of the existing ``AnalysisService``: it decides **whether** a
run may start and **which run id** it gets; the service already owns the
idempotency/persistence path (CLI and Telegram share the same one — plan
work item 1), so the scheduler adds zero new state transitions of its own.

Gating layers (all evaluated before any agent call):

1. **Session gate** — the analysis date must be an actual IDX trading day
   (weekday + not in the explicit IDX-holiday calendar).
2. **Market-close gate** — the wall clock must be past the IDX close in
   ``Asia/Jakarta`` (``IDX_TZ`` is imported from the frozen legacy screener
   via the compatibility adapter, so the timezone can never drift apart).
   When the clock has not reached the close, the run is deferred — never
   silently executed early on still-open candles (plan item 2).
3. **Stale-data gate** — the snapshot's completed-candle ``as_of`` must equal
   the analysis date; a provider lagging behind produces ``SKIP_STALE_DATA``
   (plan item 2). Fresh-data confidence comes from the source manifest
   recorded with the snapshot, not from wall-clock trust.
4. **Duplicate suppression** — the idempotency key is snapshot-level, so a
   repeated trigger replays the stored run with zero agent calls.
5. **Missed-run detection** — a trading day with no terminal run in the
   store yields ``MISSED``; the operator performs the catch-up with
   ``run_backfill(as_of=...)`` which rebuilds point-in-time from the
   supplied historical frames (plan item 7: never silently reuse current
   news/universe for a historical date — the backfill refuses a snapshot
   whose ``as_of`` does not match the requested date).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from cacingnaga.config import AIAnalystConfig
from cacingnaga.legacy_adapter import get_legacy_ranking
from cacingnaga.snapshot import AnalysisSnapshot
from cacingnaga.store import AuditStore

# Frozen-legacy timezone (plan §7 Phase 8): one Asia/Jakarta definition, the
# one the legacy screener already computes its analysis_date_wib against.
IDX_TZ = get_legacy_ranking().IDX_TZ

IDX_CLOSE_TIME = (16, 0)  # 16:00 WIB — regular session fully closed.
IDX_HOLIDAYS_2025_2026 = frozenset({
    # 2025 (IDX official non-trading days)
    "2025-01-01", "2025-01-28", "2025-01-29", "2025-03-12", "2025-03-31",
    "2025-04-02", "2025-04-03", "2025-05-01", "2025-05-12", "2025-05-13",
    "2025-05-14", "2025-06-06", "2025-06-09", "2025-06-27", "2025-08-15",
    "2025-09-05", "2025-12-25",
    # 2026 (IDX official non-trading days)
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-03-03", "2026-03-20",
    "2026-04-03", "2026-05-01", "2026-05-27", "2026-05-28", "2026-06-01",
    "2026-06-25", "2026-08-17", "2026-12-24", "2026-12-25",
})


def idx_now(now: datetime | None = None) -> datetime:
    """Wall clock in Asia/Jakarta (naive, matching the legacy convention)."""
    if now is None:
        return datetime.now(IDX_TZ).replace(tzinfo=None)
    if now.tzinfo is None:
        return now
    return now.astimezone(IDX_TZ).replace(tzinfo=None)


@dataclass(frozen=True)
class IDXCalendar:
    """IDX trading-day calendar: weekdays minus the explicit holiday set.

    Extra closures (election day, unexpected decree) can be added per
    deployment via ``extra_holidays`` without touching the frozen legacy.
    """

    holidays: frozenset[str] = IDX_HOLIDAYS_2025_2026
    extra_holidays: frozenset[str] = frozenset()

    @property
    def _closed(self) -> frozenset[str]:
        return self.holidays | self.extra_holidays

    def is_trading_day(self, day: date | str) -> bool:
        d = day if isinstance(day, date) else date.fromisoformat(str(day))
        return d.weekday() < 5 and d.isoformat() not in self._closed

    def previous_trading_day(self, day: date | str) -> str:
        """Last trading day strictly before ``day`` (deterministic walk)."""
        d = day if isinstance(day, date) else date.fromisoformat(str(day))
        for _ in range(365):  # bounded; a year of pure holidays is broken data
            d -= timedelta(days=1)
            if self.is_trading_day(d):
                return d.isoformat()
        raise ValueError("no trading day found within one calendar year")

    def count_trading_days(self, start: date, end: date) -> int:
        """Inclusive count of IDX trading days between start and end."""
        total, d = 0, start
        while d <= end:
            total += 1 if self.is_trading_day(d) else 0
            d += timedelta(days=1)
        return total


# ---------------------------------------------------------------------------
# Backfill boundary (plan §7 Phase 8, item 7)
# ---------------------------------------------------------------------------


class SnapshotSource(Protocol):
    """Supplies a point-in-time snapshot for an explicit analysis date."""

    def __call__(self, *, analysis_date: str, as_of: str) -> AnalysisSnapshot: ...


# ---------------------------------------------------------------------------
# Scheduler configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchedulerConfig:
    """Post-market scheduling windows and thresholds (all in WIB)."""

    close_hour: int = IDX_CLOSE_TIME[0]
    close_minute: int = IDX_CLOSE_TIME[1]
    # Operators may not fire the run before the session fully closes.
    earliest_run_after_close_minutes: int = 0
    # ``look_back_days`` bounds the missed-run scan window.
    look_back_days: int = 5
    calendar: IDXCalendar = field(default_factory=IDXCalendar)

    def validate(self) -> None:
        from cacingnaga.errors import ContractViolation

        if not (0 <= self.close_hour <= 23 and 0 <= self.close_minute <= 59):
            raise ContractViolation("scheduler close time must be a valid WIB time")
        if self.earliest_run_after_close_minutes < 0:
            raise ContractViolation("earliest_run_after_close_minutes must be non-negative")
        if self.look_back_days < 1 or self.look_back_days > 30:
            raise ContractViolation("scheduler look_back_days must be within [1, 30]")


# ---------------------------------------------------------------------------
# Decisions and outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleDecision:
    """Why the scheduler did or did not start a run (fully auditable)."""

    action: str                       # RUN | SKIP_... | DEFERRED
    analysis_date: str                # candidate session being considered
    last_completed_session: str | None
    missed_days: tuple[str, ...] = ()
    reason: str = ""
    stage: str = "scheduler"          # failed stage for ops alerting

    @property
    def ran(self) -> bool:
        return self.action == "RUN"

    def payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "analysis_date": self.analysis_date,
            "last_completed_session": self.last_completed_session,
            "missed_days": list(self.missed_days),
            "reason": self.reason,
            "stage": self.stage,
        }


@dataclass(frozen=True)
class SchedulerOutcome:
    """Result of one ``run_once``/``run_backfill`` invocation."""

    decision: ScheduleDecision
    service_result: Any = None        # AnalysisServiceResult, or None on skip
    run_id: str | None = None
    replayed: bool = False


class _Clock(Protocol):
    def __call__(self, now: datetime | None = None) -> datetime: ...


# ---------------------------------------------------------------------------
# Missed-run detection (plan work item 2)
# ---------------------------------------------------------------------------


class MissedRunDetector:
    """Scan the store for completed sessions and flag trading-day gaps."""

    def __init__(self, store: AuditStore, calendar: IDXCalendar) -> None:
        self._store = store
        self._calendar = calendar

    def last_completed_session(self) -> str | None:
        """Most recent terminal (COMPLETE/PARTIAL/FAILED) analysis_date.

        PARTIAL/FAILED still count as *executed* sessions for miss detection:
        the runbook handles recovery separately, and a degraded run must not
        cause unbounded catch-up backfills.
        """
        dates = [
            run.analysis_date
            for run in self._store.list_runs(limit=500)
            if run.run_status in ("COMPLETE", "PARTIAL", "FAILED")
        ]
        return max(dates) if dates else None

    def missed_days(self, today: date, look_back_days: int = 5) -> tuple[str, ...]:
        """Trading days since the last completed session (bounded window)."""
        last = self.last_completed_session()
        if last is None:
            return ()
        d = date.fromisoformat(last) + timedelta(days=1)
        missed: list[str] = []
        while d < today:
            if self._calendar.is_trading_day(d):
                missed.append(d.isoformat())
            d += timedelta(days=1)
        return tuple(missed[-look_back_days:])


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Deterministic post-market gate in front of ``AnalysisService``.

    The service factory indirection keeps the scheduler network-free and
    fully testable: tests pass a callable that constructs the real service
    (or a fixture-backed one) on demand.
    """

    def __init__(
        self,
        config: AIAnalystConfig,
        store: AuditStore,
        service_factory: Callable[[str, AnalysisSnapshot], Any],
        *,
        scheduler_config: SchedulerConfig | None = None,
        clock: _Clock | None = None,
    ) -> None:
        """``service_factory(analysis_date, snapshot)`` builds the service.

        The snapshot is handed to the factory so the service (and its
        transport) are always constructed for the exact snapshot under
        analysis — a point-in-time backfill never borrows another run's
        agent bindings.
        """
        self._config = config
        self._store = store
        self._service_factory = service_factory
        self._scheduler_config = scheduler_config or SchedulerConfig()
        self._now = clock or idx_now
        self._detector = MissedRunDetector(
            store, self._scheduler_config.calendar
        )

    # -- internals ----------------------------------------------------------

    def _validate_compatibility(self) -> None:
        """Accept a run only under compatible schema/config/code (item 6)."""
        from cacingnaga.errors import ContractViolation
        from cacingnaga.versioning import PIPELINE_VERSION

        if self._config.pipeline_version != PIPELINE_VERSION:
            raise ContractViolation(
                f"scheduler refuses incompatible pipeline: "
                f"{self._config.pipeline_version} != {PIPELINE_VERSION}"
            )
        self._config.validate()
        self._scheduler_config.validate()

    def _before_close(self, now: datetime) -> bool:
        cfg = self._scheduler_config
        limit = (cfg.close_hour, cfg.close_minute, cfg.earliest_run_after_close_minutes)
        hh, mm = cfg.close_hour, cfg.close_minute
        close = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        close += timedelta(minutes=limit[2])
        return now < close

    def _run_id(self, trigger: str, analysis_date: str) -> str:
        """Deterministic run id per (trigger, session, calendar date fired)."""
        return f"sched-{analysis_date}-{trigger}-{idx_now(self._now()).date().isoformat()}"

    # -- public API ---------------------------------------------------------

    def should_run(self, *, now: datetime | None = None) -> ScheduleDecision:
        """Evaluate every gate; no side effects (health checks read this)."""
        cfg = self._scheduler_config
        now = idx_now(now if now is not None else self._now())
        today = now.date()

        # 1. Session gate: weekends and IDX holidays are not sessions.
        if not cfg.calendar.is_trading_day(today):
            return ScheduleDecision(
                "SKIP_NON_TRADING_DAY", today.isoformat(),
                self._detector.last_completed_session(),
                reason=f"{today.isoformat()} is not an IDX trading day",
            )

        # 2. Market-close gate: never run on open candles.
        if self._before_close(now):
            return ScheduleDecision(
                "DEFERRED", today.isoformat(),
                self._detector.last_completed_session(),
                reason=(
                    f"market still open or cooling down "
                    f"(close {cfg.close_hour:02d}:{cfg.close_minute:02d} WIB "
                    f"+{cfg.earliest_run_after_close_minutes}m)"
                ),
                stage="market_close_gate",
            )

        # 3. Missed-run detection (observability; the catch-up is explicit).
        missed = self._detector.missed_days(
            today, look_back_days=cfg.look_back_days
        )
        last = self._detector.last_completed_session()
        if last == today.isoformat():
            return ScheduleDecision(
                "SKIP_ALREADY_RUN", today.isoformat(), last,
                reason=f"session {today.isoformat()} already executed today",
            )
        if missed:
            return ScheduleDecision(
                "SKIP_MISSED_RUNS", today.isoformat(), last, missed,
                reason=(
                    f"{len(missed)} missed session(s) require explicit "
                    f"run_backfill(as_of=...) — never auto-backfill"
                ),
                stage="missed_run_detector",
            )

        return ScheduleDecision(
            "RUN", today.isoformat(), last,
            reason=f"post-market session {today.isoformat()} is due",
        )

    def _find_run_for_session(self, analysis_date: str):
        """Terminal run covering a session, if any (duplicate-trigger path)."""
        for run in self._store.list_runs(limit=500):
            if run.analysis_date == analysis_date and run.run_status in (
                "COMPLETE", "PARTIAL", "FAILED"
            ):
                return run
        return None

    def run_once(
        self,
        snapshot: AnalysisSnapshot,
        *,
        trigger: str = "schedule",
    ) -> SchedulerOutcome:
        """Fire the post-market run if every gate passes (item 1)."""
        self._validate_compatibility()
        decision = self.should_run()
        if decision.action == "SKIP_ALREADY_RUN":
            # The session already has a terminal run: surface it as a replay.
            existing = self._find_run_for_session(decision.analysis_date)
            return SchedulerOutcome(
                decision=decision,
                run_id=existing.run_id if existing else None,
                replayed=existing is not None,
            )
        if not decision.ran:
            return SchedulerOutcome(decision=decision)

        # 4. Stale-data gate: the snapshot must close the session being run.
        if snapshot.as_of != decision.analysis_date:
            run_id = self._run_id(trigger, decision.analysis_date)
            self._store.create_run(
                run_id=run_id,
                snapshot=snapshot,
                config=self._config,
                snapshot_payload=snapshot.payload(),
                trigger=f"{trigger}:gated",
            )
            self._store.transition_run(run_id, "FAILED")
            return SchedulerOutcome(
                decision=ScheduleDecision(
                    "SKIP_STALE_DATA", decision.analysis_date,
                    decision.last_completed_session, decision.missed_days,
                    reason=(
                        f"snapshot as_of {snapshot.as_of} does not cover "
                        f"session {decision.analysis_date}"
                    ),
                    stage="stale_data_gate",
                )
            )

        service = self._service_factory(decision.analysis_date, snapshot)
        run_id = self._run_id(trigger, decision.analysis_date)
        result = service.run_full_analysis(
            snapshot, run_id=run_id, trigger=trigger
        )
        # The store dedups by idempotency key: a repeated trigger replays.
        return SchedulerOutcome(
            decision=decision,
            service_result=result,
            run_id=result.run_result.run_id,
            replayed=result.replayed,
        )

    def run_backfill(
        self,
        analysis_date: str,
        source: SnapshotSource,
        *,
        trigger: str = "backfill",
    ) -> SchedulerOutcome:
        """Manual catch-up with an **explicit** as_of (plan work item 7).

        The ``source`` callable must build a point-in-time snapshot for the
        requested date (historical frames, archived universe). A snapshot
        whose ``as_of`` does not equal the requested date is refused —
        never silently reuse current data for a historical date.
        """
        self._validate_compatibility()
        if not self._scheduler_config.calendar.is_trading_day(analysis_date):
            return SchedulerOutcome(
                decision=ScheduleDecision(
                    "SKIP_NON_TRADING_DAY", analysis_date,
                    self._detector.last_completed_session(),
                    reason=f"{analysis_date} is not an IDX trading day",
                )
            )
        snapshot = source(analysis_date=analysis_date, as_of=analysis_date)
        if snapshot.as_of != analysis_date:
            return SchedulerOutcome(
                decision=ScheduleDecision(
                    "SKIP_STALE_DATA", analysis_date,
                    self._detector.last_completed_session(),
                    reason=(
                        f"backfill source returned as_of {snapshot.as_of}, "
                        f"not the requested {analysis_date}"
                    ),
                    stage="backfill_gate",
                )
            )
        service = self._service_factory(analysis_date, snapshot)
        result = service.run_full_analysis(
            snapshot, run_id=f"sched-backfill-{analysis_date}", trigger=trigger
        )
        return SchedulerOutcome(
            decision=ScheduleDecision(
                "RUN", analysis_date, self._detector.last_completed_session(),
                reason=f"manual backfill for {analysis_date}",
            ),
            service_result=result,
            run_id=result.run_result.run_id,
            replayed=result.replayed,
        )
