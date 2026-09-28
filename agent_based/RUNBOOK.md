# CacingNagaPRO AI Analyst Team — Operations Runbook

Scope: the scheduled post-market run produced by `agent_based/scheduler.py`,
its operational surfaces (`agent_based/ops.py`), and the fallback to the
frozen legacy screener (`scripts/`). Ownership map lives in
`ops.STAGE_OWNERS`; alert thresholds in `ops.ALERT_THRESHOLDS`.

**Non-negotiables (PRD §4):** no LLM ever computes status/score; PARTIAL and
FAILED are never shown as NO_TRADE; the store is the only system of record;
the rollback target is the frozen legacy screener.

## 1. Daily schedule model

- One run per IDX trading day, only after the close gate passes
  (16:00 WIB + `earliest_run_after_close_minutes`).
- Weekends and IDX holidays are `SKIP_NON_TRADING_DAY` (calendar in
  `scheduler.IDXCalendar`; extra closures go in `extra_holidays`).
- The scheduler never auto-backfills: a trading-day gap raises
  `SKIP_MISSED_RUNS` and pages the owner; the operator decides.
- Duplicate triggers are safe: the idempotency key dedups and replays with
  zero agent calls (`replayed=True`).

## 2. Incident playbook

### 2.1 Stale / partial data (`SKIP_STALE_DATA`)

1. Confirm the failed stage: `stage="stale_data_gate"` in the decision
   payload; `market_data` health shows `snapshot_age_days` over threshold.
2. Check the provider. If the provider is behind, wait for its catch-up and
   re-fire `run_once` — do not lower the gate.
3. If the provider is permanently behind, treat as the provider-outage
   playbook (2.2) and fall back if needed (2.6).

### 2.2 Provider outage (Hermes / agent transport)

1. `hermes` health flips when the `CircuitBreaker` opens
   (`CachedBreakerTransport.breaker_state == "OPEN"`); failures are
   bounded retries (`TransportConfig.max_attempts`), then fast-fail.
2. A mid-run outage degrades the run to PARTIAL/NOT_EVALUATED — this is the
   documented safe state; never present it as NO_TRADE.
3. Recovery: after the cooldown the breaker half-opens with one probe;
   no operator action is required. If the outage persists past
   `ALERT_THRESHOLDS["hermes"]["max_stage_seconds"]`, roll back (2.6).

### 2.3 Malformed agent output

1. Validation is fail-closed: a malformed payload is rejected by the
   agents/runner and recorded in `agent_outputs.validation_ok=0` with the
   error; no partially-trusted reading ever reaches policy.
2. If one candidate fails → PARTIAL run; if the Decision Agent fails →
   PARTIAL/NOT_EVALUATED (provider failure, §4 item 10).
3. Owner reviews the stored `validation_error`, fixes the prompt/schema,
   and replays with a fresh run id — never mutates the stored run.

### 2.4 Database recovery (audit store)

1. Stop writers (scheduler + Telegram service).
2. Restore the most recent backup: `AuditStore.restore(source, target)`
   (SQLite backup API; the store runs WAL + thread_safe mode).
3. Verify integrity, then restart. Runs after the backup horizon are
   re-executed as fresh runs — the store is append-only, nothing is
   silently overwritten.

### 2.5 Missed run (process down on a session day)

1. Health shows `scheduler` DEGRADED with `SKIP_MISSED_RUNS` and the
   missed day list.
2. Catch-up is **manual and explicit**:
   `Scheduler.run_backfill("YYYY-MM-DD", source)` where `source` builds a
   point-in-time snapshot with `build_snapshot(..., as_of="YYYY-MM-DD")`
   over *historical* frames. The backfill refuses any snapshot whose
   `as_of` does not equal the requested date.
3. Never auto-backfill; never reuse today's universe/news for a historical
   date (plan work item 7).

### 2.6 Rollback to the frozen legacy screener

1. Disable the AI pipeline trigger (scheduler feature flag) and the
   Telegram bot (`TelegramConfig.enabled=False` — default off).
2. Daily operation returns to `scripts/financial_screener.py` on the
   existing GitHub Actions schedule (unchanged during rollout — plan
   work item 9); it writes the same outputs it always has.
3. No data loss: AI-pipeline runs stay in the audit store for later
   evaluation; the legacy path never reads or writes that store.
4. Re-promotion follows the rollout checklist (README Phase 8 section)
   and requires the Phase 7 evaluation report to still pass promotion
   criteria.

## 3. Drill log

| Date | Scenario | Result |
|---|---|---|
| 2026-09-28 | Holiday → `SKIP_NON_TRADING_DAY`; duplicate trigger → replay, 0 calls; missed run → detected + explicit backfill replays deterministically; stale source → FAILED gated run + SKIP_STALE_DATA; Hermes outage → breaker OPEN, PARTIAL/NOT_EVALUATED; process restart → store dedups, health reports FAIL until restart | **PASS** (automated: `tests/test_scheduler.py`) |
