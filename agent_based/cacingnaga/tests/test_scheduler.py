"""Phase 8 — Scheduling, Operations, and Rollout (plan §7 Phase 8).

Exit criteria under test (plan work items 1, 2, 3, 4, 5, 6, 7 and the §7
Phase 8 exit list):

- a simulated holiday produces ``SKIP_NON_TRADING_DAY`` and runs nothing,
- a duplicate trigger replays the stored run with zero agent calls,
- a missed session is detected and only the explicit manual
  ``run_backfill`` (with an ``as_of``-consistent snapshot) recovers it,
- a stale source produces a FAILED gated run and never executes agents,
- a Hermes outage trips the circuit breaker and the run degrades to
  PARTIAL/NOT_EVALUATED — never NO_TRADE,
- a process restart (fresh Scheduler/store on the same database) dedups
  and keeps health reporting correct,
- manual replay reconstructs the same stored run/policy state,
- alerts fire within thresholds, identify the failed stage, and never
  leak secrets,
- the three secret domains (Telegram/Hermes/legacy Gemini) stay separate.

The frozen legacy screener (scripts/ + tests/) is never imported here
except through ``cacingnaga.legacy_adapter`` for ``IDX_TZ``.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
sys.path.insert(0, str(BASE))

from cacingnaga.config import AIAnalystConfig
from cacingnaga.errors import ContractViolation
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.snapshot import build_snapshot, snapshot_to_agent_envelope
from cacingnaga.store import AuditStore
from cacingnaga.transport import FakeTransport, TransportConfig

import ops
from ops import (
    ALERT_THRESHOLDS,
    STAGE_OWNERS,
    CachedBreakerTransport,
    CircuitBreaker,
    CircuitBreakerPolicy,
    stage_metrics,
    validate_secret_separation,
)
from scheduler import (
    IDXCalendar,
    MissedRunDetector,
    ScheduleDecision,
    Scheduler,
    SchedulerConfig,
    idx_now,
)
from orchestrator import AnalysisService

# test_orchestrator already proved handler factories; import them so Phase 8
# exercises the exact same validated agent behavior end to end.
from cacingnaga.tests.test_orchestrator import (  # noqa: E402
    full_transport,
)


CONFIG = AIAnalystConfig()
# Fixture frames end 2026-09-18 (a trading day, past from the test clock).
SESSION = "2026-09-18"
AFTER_CLOSE = datetime(2026, 9, 18, 19, 0)  # 19:00 WIB, session closed


def make_snapshot():
    return build_snapshot(market_frame(), default_frames(), CONFIG)


def make_service(store, snapshot):
    env = snapshot_to_agent_envelope(snapshot)
    transport = full_transport(env)
    return AnalysisService(CONFIG, store, transport)


def make_scheduler(store, *, now=AFTER_CLOSE, config=CONFIG,
                   scheduler_config=None):
    return Scheduler(
        config, store,
        lambda analysis_date, snapshot: make_service(store, snapshot),
        scheduler_config=scheduler_config,
        clock=lambda _now=None: now,
    )


# ---------------------------------------------------------------------------
# Work item 1: one post-market run through the shared persistence path
# ---------------------------------------------------------------------------


def test_scheduler_runs_due_session_through_service(tmp_path):
    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(store)
    outcome = scheduler.run_once(make_snapshot())
    assert outcome.decision.action == "RUN"
    assert outcome.run_id.startswith(f"sched-{SESSION}-schedule-")
    stored = store.get_run(outcome.run_id)
    assert stored.run_status == "COMPLETE"
    assert stored.payload["trigger"] == "schedule"
    # Same path as CLI/Telegram: decisions + agent outputs persisted.
    assert store.get_decisions(outcome.run_id)
    assert store.get_agent_outputs(outcome.run_id)


def test_run_id_is_deterministic_per_trigger_and_session(tmp_path):
    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(store)
    first = scheduler.run_once(make_snapshot())
    assert first.run_id == f"sched-{SESSION}-schedule-2026-09-18"
    # The store dedups by idempotency key regardless of the fresh run id.
    stored = store.get_run_by_idempotency_key(
        AuditStore.idempotency_key(
            make_snapshot().data_snapshot_hash,
            store.get_run(first.run_id).config_hash,
            SESSION,
        )
    )
    assert stored is not None and stored.run_id == first.run_id


# ---------------------------------------------------------------------------
# Work item 2: market-close gating and IDX holidays
# ---------------------------------------------------------------------------


def test_market_close_gate_defers_before_close(tmp_path):
    store = AuditStore(tmp_path / "p8.sqlite3")
    midday = datetime(2026, 9, 18, 12, 0)  # session still open
    scheduler = make_scheduler(store, now=midday)
    decision = scheduler.should_run()
    assert decision.action == "DEFERRED"
    assert decision.stage == "market_close_gate"
    # And run_once honors the gate: no run is created.
    outcome = scheduler.run_once(make_snapshot())
    assert outcome.decision.action == "DEFERRED"
    assert store.list_runs() == []


def test_earliest_run_after_close_is_respected(tmp_path):
    cfg = SchedulerConfig(earliest_run_after_close_minutes=30)
    store = AuditStore(tmp_path / "p8.sqlite3")
    at_1615 = datetime(2026, 9, 18, 16, 15)  # after close, inside cooldown
    scheduler = make_scheduler(store, now=at_1615, scheduler_config=cfg)
    assert scheduler.should_run().action == "DEFERRED"
    at_1631 = datetime(2026, 9, 18, 16, 31)
    scheduler = make_scheduler(store, now=at_1631, scheduler_config=cfg)
    assert scheduler.should_run().action == "RUN"


def test_idx_holiday_produces_safe_skip(tmp_path):
    store = AuditStore(tmp_path / "p8.sqlite3")
    # 2026-12-25 (Christmas) is an official IDX non-trading day.
    holiday = datetime(2026, 12, 25, 19, 0)
    scheduler = make_scheduler(store, now=holiday)
    decision = scheduler.should_run()
    assert decision.action == "SKIP_NON_TRADING_DAY"
    assert decision.analysis_date == "2026-12-25"
    outcome = scheduler.run_once(make_snapshot())
    assert outcome.decision.action == "SKIP_NON_TRADING_DAY"
    assert outcome.service_result is None
    assert store.list_runs() == []


def test_weekend_produces_safe_skip(tmp_path):
    store = AuditStore(tmp_path / "p8.sqlite3")
    saturday = datetime(2026, 9, 19, 19, 0)
    scheduler = make_scheduler(store, now=saturday)
    assert scheduler.should_run().action == "SKIP_NON_TRADING_DAY"


def test_calendar_knows_2026_holidays():
    cal = IDXCalendar()
    assert not cal.is_trading_day("2026-01-01")   # New Year
    assert not cal.is_trading_day("2026-08-17")   # Independence Day
    assert not cal.is_trading_day("2026-02-16")   # Lunar New Year observed
    assert cal.is_trading_day("2026-09-18")       # regular Friday session
    # Extra closure without touching the frozen set.
    extra = IDXCalendar(extra_holidays=frozenset({"2026-09-18"}))
    assert not extra.is_trading_day("2026-09-18")


def test_previous_trading_day_walks_holidays():
    cal = IDXCalendar()
    # Friday 2026-01-02 follows the New Year holiday.
    assert cal.previous_trading_day("2026-01-05") == "2026-01-02"
    assert cal.previous_trading_day("2026-01-02") == "2025-12-31"


def test_scheduler_config_validation():
    with pytest.raises(ContractViolation):
        SchedulerConfig(look_back_days=0).validate()
    with pytest.raises(ContractViolation):
        SchedulerConfig(close_hour=24).validate()
    with pytest.raises(ContractViolation):
        SchedulerConfig(earliest_run_after_close_minutes=-1).validate()


# ---------------------------------------------------------------------------
# Work item 2: duplicate triggers
# ---------------------------------------------------------------------------


def test_duplicate_trigger_replays_with_zero_agent_calls(tmp_path):
    snapshot = make_snapshot()
    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(store)
    first = scheduler.run_once(snapshot)
    assert first.decision.action == "RUN" and not first.replayed

    # Duplicate trigger on the same completed session: the scheduler
    # surfaces the stored run without re-running any service.
    second = scheduler.run_once(snapshot)
    assert second.replayed is True
    assert second.decision.action == "SKIP_ALREADY_RUN"
    # Identical stored run/policy state.
    assert second.run_id == first.run_id
    assert store.get_run(first.run_id).run_status == "COMPLETE"


def test_second_service_construction_same_day_replays(tmp_path):
    """Process restart: a fresh scheduler/service on the same store dedups."""
    snapshot = make_snapshot()
    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(store)
    first = scheduler.run_once(snapshot)

    # "Restart": brand-new service objects, same store file.
    store2 = AuditStore(tmp_path / "p8.sqlite3")
    env = snapshot_to_agent_envelope(snapshot)
    fresh_transport = full_transport(env)
    fresh_service = AnalysisService(CONFIG, store2, fresh_transport)
    fresh_scheduler = Scheduler(
        CONFIG, store2,
        lambda _d, snap: fresh_service,
        clock=lambda _n=None: AFTER_CLOSE,
    )
    second = fresh_scheduler.run_once(snapshot)
    assert second.replayed is True
    assert second.run_id == first.run_id
    assert len(fresh_transport.calls) == 0


# ---------------------------------------------------------------------------
# Work item 2: stale data gate
# ---------------------------------------------------------------------------


def test_stale_snapshot_is_gated_not_run(tmp_path):
    # A real stale snapshot: point-in-time data ending 2026-09-17.
    import pandas as pd

    frames = default_frames()
    ihsg = market_frame()
    cutoff = pd.Timestamp("2026-09-17")
    stale = build_snapshot(
        ihsg[ihsg.index <= cutoff],
        {t: f[f.index <= cutoff] for t, f in frames.items()},
        CONFIG,
        as_of="2026-09-17",
    )
    assert stale.as_of == "2026-09-17"
    assert stale.data_snapshot_hash != make_snapshot().data_snapshot_hash
    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(store)
    outcome = scheduler.run_once(stale)
    assert outcome.decision.action == "SKIP_STALE_DATA"
    assert outcome.decision.stage == "stale_data_gate"
    assert outcome.service_result is None
    # The gating is auditable: a FAILED run row exists, no agent ran.
    runs = store.list_runs()
    assert len(runs) == 1 and runs[0].run_status == "FAILED"
    assert "gated" in runs[0].payload["trigger"]
    assert store.get_agent_outputs(runs[0].run_id) == []


def test_backfill_refuses_stale_source(tmp_path):
    """Backfill never silently reuses current data for a historical date."""
    snapshot = make_snapshot()
    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(store)

    def bad_source(*, analysis_date, as_of):
        # Claims 2026-09-17 but returns the 2026-09-18 snapshot.
        return snapshot

    outcome = scheduler.run_backfill("2026-09-17", bad_source)
    assert outcome.decision.action == "SKIP_STALE_DATA"
    assert outcome.decision.stage == "backfill_gate"
    assert store.list_runs() == []


def test_backfill_refuses_non_trading_day(tmp_path):
    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(store)
    outcome = scheduler.run_backfill(
        "2026-12-25",
        lambda *, analysis_date, as_of: pytest.fail("must not build"),
    )
    assert outcome.decision.action == "SKIP_NON_TRADING_DAY"
    assert store.list_runs() == []


def test_explicit_as_of_backfill_matches_manual_replay(tmp_path):
    """Plan exit criterion: manual replay reconstructs the same state."""
    import pandas as pd

    frames = default_frames()
    ihsg = market_frame()
    cutoff = pd.Timestamp("2026-09-17")

    def source(*, analysis_date, as_of):
        assert analysis_date == as_of == "2026-09-17"
        return build_snapshot(
            ihsg[ihsg.index <= cutoff],
            {t: f[f.index <= cutoff] for t, f in frames.items()},
            CONFIG,
            as_of="2026-09-17",
        )

    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(store)
    outcome = scheduler.run_backfill("2026-09-17", source)
    assert outcome.decision.action == "RUN"
    assert outcome.run_id == "sched-backfill-2026-09-17"
    stored = store.get_run(outcome.run_id)
    assert stored.run_status == "COMPLETE"
    assert stored.analysis_date == "2026-09-17"
    assert stored.as_of == "2026-09-17"
    # The snapshot is the point-in-time one, not today's data re-cut.
    assert stored.data_snapshot_hash != make_snapshot().data_snapshot_hash


def test_backfill_replay_is_deterministic(tmp_path):
    import pandas as pd

    frames = default_frames()
    ihsg = market_frame()
    cutoff = pd.Timestamp("2026-09-17")

    def source(*, analysis_date, as_of):
        return build_snapshot(
            ihsg[ihsg.index <= cutoff],
            {t: f[f.index <= cutoff] for t, f in frames.items()},
            CONFIG,
            as_of="2026-09-17",
        )

    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(store)
    first = scheduler.run_backfill("2026-09-17", source)
    second = scheduler.run_backfill("2026-09-17", source)
    assert second.replayed is True
    assert second.run_id == first.run_id
    assert first.service_result.payload()["run_result"] == \
        second.service_result.payload()["run_result"]


# ---------------------------------------------------------------------------
# Work item 2: missed-run detection and manual catch-up
# ---------------------------------------------------------------------------


def test_missed_run_detected_after_gap(tmp_path):
    store = AuditStore(tmp_path / "p8.sqlite3")
    detector = MissedRunDetector(store, IDXCalendar())
    # Thursday session executed; Friday + Monday missed.
    run = store.create_run(
        run_id="manual-thu", snapshot=make_snapshot(), config=CONFIG,
        snapshot_payload=make_snapshot().payload(), trigger="manual",
    )
    store.transition_run("manual-thu", "COMPLETE")
    missed = detector.missed_days(date(2026, 9, 22), look_back_days=5)
    # Fixture snapshot is as_of 2026-09-18, so the run is dated 2026-09-18;
    # missing sessions are 2026-09-21 (Mon) — 2026-09-22 is "today", not missed.
    assert detector.last_completed_session() == SESSION
    assert missed == ("2026-09-21",)


def test_scheduler_skips_and_reports_missed_runs(tmp_path):
    store = AuditStore(tmp_path / "p8.sqlite3")
    # Session 2026-09-17 (Thursday) executed; Friday 2026-09-18 missed.
    import pandas as pd

    frames = default_frames()
    ihsg = market_frame()

    def source_for(day):
        cutoff = pd.Timestamp(day)
        return lambda *, analysis_date, as_of: build_snapshot(
            ihsg[ihsg.index <= cutoff],
            {t: f[f.index <= cutoff] for t, f in frames.items()},
            CONFIG,
            as_of=day,
        )

    first = scheduler_run_backfill(store, "2026-09-17", source_for("2026-09-17"))
    assert first.decision.action == "RUN"

    # Monday 2026-09-21 evening: Friday's session was never executed.
    late_scheduler = make_scheduler(store, now=datetime(2026, 9, 21, 19, 0))
    decision = late_scheduler.should_run()
    assert decision.action == "SKIP_MISSED_RUNS"
    assert decision.missed_days == ("2026-09-18",)
    assert decision.stage == "missed_run_detector"

    # Manual catch-up on the missed day with an as_of-consistent snapshot.
    backfill = late_scheduler.run_backfill("2026-09-18", source_for("2026-09-18"))
    assert backfill.decision.action == "RUN"
    assert backfill.run_id == "sched-backfill-2026-09-18"
    assert store.get_run(backfill.run_id).analysis_date == "2026-09-18"


def scheduler_run_backfill(store, day, source):
    scheduler = make_scheduler(store)
    return scheduler.run_backfill(day, source)


def test_no_missed_days_when_running_daily(tmp_path):
    store = AuditStore(tmp_path / "p8.sqlite3")
    detector = MissedRunDetector(store, IDXCalendar())
    assert detector.missed_days(date(2026, 9, 21)) == ()   # nothing yet to miss
    store.create_run(
        run_id="r1", snapshot=make_snapshot(), config=CONFIG,
        snapshot_payload=make_snapshot().payload(), trigger="manual",
    )
    store.transition_run("r1", "COMPLETE")
    # Daily runs through Friday leave nothing missed by Monday morning.
    assert detector.missed_days(date(2026, 9, 21)) == ()


# ---------------------------------------------------------------------------
# Work item 3: circuit breaker, bounded retries, safe caching
# ---------------------------------------------------------------------------


def test_circuit_breaker_states():
    now = [1000.0]
    breaker = CircuitBreaker(
        CircuitBreakerPolicy(failure_threshold=2, cooldown_seconds=60.0),
        clock=lambda: now[0],
    )
    assert breaker.state == "CLOSED"
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "CLOSED"
    breaker.record_failure()
    assert breaker.state == "OPEN"
    assert breaker.allow() is False          # cooldown still running

    now[0] += 61.0                           # cooldown elapses → probe
    assert breaker.state == "HALF_OPEN"
    assert breaker.allow()
    assert breaker.allow() is False          # probe budget spent
    breaker.record_success()
    assert breaker.state == "CLOSED"


def test_half_open_failure_reopens_breaker():
    now = [1000.0]
    breaker = CircuitBreaker(
        CircuitBreakerPolicy(failure_threshold=1, cooldown_seconds=10.0),
        clock=lambda: now[0],
    )
    breaker.record_failure()
    now[0] += 11.0
    assert breaker.state == "HALF_OPEN"      # cooldown elapsed
    breaker.record_failure()
    assert breaker.state == "OPEN"           # probe failed → reopen
    now[0] += 1.0
    assert breaker.state == "OPEN"           # new cooldown still running


def test_cached_transport_caches_identical_requests():
    calls = []

    class Counting(FakeTransport):
        def complete(self, request):
            calls.append(request)
            return super().complete(request)

    inner = Counting({"MarketAgent": lambda r: {"regime": "BULLISH"}})
    wrapped = CachedBreakerTransport(inner, transport_config=TransportConfig())
    r1 = wrapped.complete(_request())
    r2 = wrapped.complete(_request())
    assert len(calls) == 1                    # provider called once
    assert r1.payload == r2.payload


def _request(**over):
    from cacingnaga.transport import AgentRequest

    base = dict(agent_name="MarketAgent", envelope={"as_of": "2026-09-18"})
    base.update(over)
    return AgentRequest(**base)


def test_cached_transport_distinct_requests_not_shared():
    calls = []

    class Counting(FakeTransport):
        def complete(self, request):
            calls.append(request)
            return super().complete(request)

    inner = Counting({"MarketAgent": lambda r: {"regime": "BULLISH"}})
    wrapped = CachedBreakerTransport(inner, transport_config=TransportConfig())
    wrapped.complete(_request(envelope={"as_of": "2026-09-18"}))
    wrapped.complete(_request(envelope={"as_of": "2026-09-17"}))
    assert len(calls) == 2                    # different prompt → different call


def test_breaker_trips_after_repeated_failures():
    from cacingnaga.canonical import canonical_hash

    class AlwaysDown:
        def complete(self, request):
            raise RuntimeError("provider down")

    wrapped = CachedBreakerTransport(
        AlwaysDown(),
        transport_config=TransportConfig(max_attempts=2, retry_backoff_seconds=0.0),
        breaker_policy=CircuitBreakerPolicy(failure_threshold=2, cooldown_seconds=60.0),
    )
    for _ in range(2):
        with pytest.raises(RuntimeError):
            wrapped.complete(_request())
    assert wrapped.breaker_state == "OPEN"
    # While OPEN, calls fail fast with the breaker violation — no provider.
    with pytest.raises(ContractViolation, match="circuit breaker OPEN"):
        wrapped.complete(_request())


def test_breaker_respects_transport_bounds():
    with pytest.raises(ContractViolation):
        CachedBreakerTransport(
            FakeTransport(), transport_config=TransportConfig(max_attempts=99)
        )


# ---------------------------------------------------------------------------
# Work items 3/4: explicit partial/failed transitions stay intact
# ---------------------------------------------------------------------------


def test_provider_outage_degrades_run_not_no_trade(tmp_path):
    """Hermes outage mid-run → PARTIAL/NOT_EVALUATED (documented safe state)."""
    snapshot = make_snapshot()
    env = snapshot_to_agent_envelope(snapshot)
    store = AuditStore(tmp_path / "p8.sqlite3")
    # A transport whose Decision Agent dies on one candidate: bounded retries
    # then failure isolation produce the documented PARTIAL outcome.
    transport = full_transport(env, dead_decision_for=("BKSL.JK",))
    service = AnalysisService(CONFIG, store, transport)
    result = service.run_full_analysis(snapshot, run_id="run-outage")
    assert result.run_result.run_status == "PARTIAL"
    assert result.run_result.decision_outcome == "NOT_EVALUATED"
    stored = store.get_run("run-outage")
    assert stored.run_status == "PARTIAL"


def test_scheduler_outage_run_is_terminal_and_not_backfilled(tmp_path):
    """A PARTIAL session counts as executed; no unbounded auto-catch-up."""
    snapshot = make_snapshot()
    store = AuditStore(tmp_path / "p8.sqlite3")
    env = snapshot_to_agent_envelope(snapshot)
    transport = full_transport(env, dead_flow_for=("ERTX.JK",))
    service = AnalysisService(CONFIG, store, transport)
    result = service.run_full_analysis(snapshot, run_id="run-partial")
    assert result.run_result.run_status == "PARTIAL"

    detector = MissedRunDetector(store, IDXCalendar())
    # The PARTIAL session itself counts as executed (2026-09-18 is not
    # flagged); the detector still observably reports the later gap.
    assert detector.last_completed_session() == SESSION
    assert detector.missed_days(date(2026, 9, 22)) == ("2026-09-21",)


# ---------------------------------------------------------------------------
# Work item 4: metrics, alerts, health, ownership — no secret leakage
# ---------------------------------------------------------------------------


def test_stage_metrics_are_structured_and_secret_free():
    record = stage_metrics(
        "hermes", "failure", seconds=125.0,
        details={"api_key": "sk-super-secret", "attempts": 2},
    )
    assert record["stage"] == "hermes"
    assert record["owner"] == STAGE_OWNERS["hermes"] == "ai-platform"
    assert record["seconds"] == 125.0
    assert record["details"]["api_key"] == "[REDACTED]"
    assert record["details"]["attempts"] == 2


def test_alert_thresholds_are_defined_per_stage():
    assert ALERT_THRESHOLDS["hermes"]["max_stage_seconds"] == 120.0
    assert STAGE_OWNERS["market_data"] == "data-engineering"
    assert STAGE_OWNERS["telegram"] == "ai-platform"
    for stage, owner in STAGE_OWNERS.items():
        assert stage in ALERT_THRESHOLDS or stage == "policy"


def test_health_report_identifies_failed_stage_without_secrets():
    health = ops.run_health_checks(
        store=AuditStore(":memory:"),
        scheduler_decision={"action": "SKIP_MISSED_RUNS",
                            "missed_days": ["2026-09-21"]},
        snapshot_age_days=6.0,
    )
    assert health.overall == "DEGRADED"
    by_stage = {c.stage: c for c in health.checks}
    assert by_stage["market_data"].status == "DEGRADED"
    assert any("snapshot_age_days" in a for a in by_stage["market_data"].alerts)
    assert by_stage["scheduler"].status == "DEGRADED"
    assert "2026-09-21" in " ".join(by_stage["scheduler"].alerts)
    report = ops.render_health_report(health)
    assert "market_data" in report and "scheduler" in report
    assert "sk-super-secret" not in report and "token" not in report.lower()


def test_storage_failure_is_a_fail_alert():
    class BrokenStore:
        def list_runs(self, **kw):
            raise RuntimeError("disk on fire")

    health = ops.run_health_checks(store=BrokenStore())
    by_stage = {c.stage: c for c in health.checks}
    assert by_stage["storage"].status == "FAIL"
    assert health.overall == "FAIL"
    assert by_stage["storage"].owner == STAGE_OWNERS["storage"]


def test_healthy_system_reports_ok():
    health = ops.run_health_checks(
        store=AuditStore(":memory:"),
        scheduler_decision={"action": "RUN"},
        snapshot_age_days=0.5,
        telegram={"failure_rate": 0.0},
    )
    assert health.overall == "OK"
    assert health.alerts == ()


# ---------------------------------------------------------------------------
# Work item 5: secret separation
# ---------------------------------------------------------------------------


def test_secret_separation_rejects_shared_values():
    with pytest.raises(ContractViolation, match="secret separation"):
        validate_secret_separation({
            "TELEGRAM_BOT_TOKEN": "same-secret",
            "AGENT_LLM_API_KEY": "same-secret",
        })


def test_secret_separation_allows_distinct_and_absent():
    validate_secret_separation({})                      # nothing configured
    validate_secret_separation({"TELEGRAM_BOT_TOKEN": "a"})
    validate_secret_separation({
        "TELEGRAM_BOT_TOKEN": "a", "AGENT_LLM_API_KEY": "b",
        "LEGACY_GEMINI_API_KEY": "c",
    })


def test_secret_env_names_are_distinct():
    names = {ops.TELEGRAM_TOKEN_ENV, ops.AGENT_LLM_TOKEN_ENV, ops.LEGACY_GEMINI_ENV}
    assert len(names) == 3


# ---------------------------------------------------------------------------
# Work item 6: schema/config/code compatibility before accepting a run
# ---------------------------------------------------------------------------


def test_scheduler_refuses_incompatible_pipeline(tmp_path):
    from dataclasses import replace

    bad = replace(CONFIG, pipeline_version="AI_TEAM_DAILY_V0")
    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(store, config=bad)
    with pytest.raises(ContractViolation, match="incompatible pipeline"):
        scheduler.run_once(make_snapshot())
    assert store.list_runs() == []


def test_scheduler_validates_scheduler_config(tmp_path):
    store = AuditStore(tmp_path / "p8.sqlite3")
    scheduler = make_scheduler(
        store, scheduler_config=SchedulerConfig(look_back_days=99)
    )
    with pytest.raises(ContractViolation):
        scheduler.run_once(make_snapshot())


# ---------------------------------------------------------------------------
# Work item 9: the legacy schedule remains untouched
# ---------------------------------------------------------------------------


def test_legacy_schedule_files_untouched_by_scheduler():
    """The frozen legacy workflow keeps its cron entries (plan item 9)."""
    from pathlib import Path as _Path

    workflow = _Path(BASE).parent / ".github" / "workflows"
    files = list(workflow.glob("*.yml")) + list(workflow.glob("*.yaml"))
    assert files, "legacy workflow must exist"
    crons = " ".join(
        line for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
        if "cron:" in line
    )
    assert "0 11 * * 1-5" in crons           # daily post-close, unchanged
