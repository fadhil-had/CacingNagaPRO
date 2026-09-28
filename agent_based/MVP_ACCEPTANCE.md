# MVP Acceptance Checklist — record of completion (plan §12)

Every checkbox from `Backlog/improvement_v3_implementation_plan.md` §12 is
mapped to the tests that verify it. Recorded 2026-09-28 on the
`update/agent-based` branch: **278 tests green** (266 pipeline + 12 frozen
legacy) on Python 3.14 / pandas 3.0.5. Live-path note: the data-fetch
boundary was smoke-tested 2026-09-28 — network-blocked yfinance fails
closed (exit 1, no store row) and the fetch→snapshot path was proven
deterministic with stubbed legacy loaders. Shadow caveat: runs execute on
`CACINGNAGA_SMOKE_V1` (Hermes runtime unconfirmed, decision #16); all
assertions below are provider-independent — swapping in a real transport
does not change what these tests prove.

## Functional

- [x] **The scheduler runs `/screen` logic automatically once after a
  completed IDX session and records holiday/missed/duplicate outcomes.**
  `test_scheduler.py::test_scheduler_runs_due_session_through_service`,
  `test_idx_holiday_produces_safe_skip`, `test_weekend_produces_safe_skip`,
  `test_scheduler_skips_and_reports_missed_runs`,
  `test_duplicate_trigger_replays_with_zero_agent_calls`,
  `test_entry_run_defers_before_close_and_writes_nothing` — one run per
  session over the shared `AnalysisService` path; every skip state is a
  structured, auditable `ScheduleDecision`.
- [x] **Market, Technical, Flow, and Decision outputs validate against
  versioned interpretation schemas.** `test_agents.py` (31 tests) —
  forbidden fields, strict enums, evidence citations, bounded retries.
- [x] **`/analyze TICKER` works independently of the daily screener.**
  `test_telegram.py` — single-flight `/analyze` runs the focused service
  path; store-reads only on replay.
- [x] **`/market`, `/status`, and `/why TICKER` return persisted canonical
  data.** `test_telegram.py` + `render.py` tests — all three read the
  durable store; never re-run a model.
- [x] **Significant conflicts trigger an auditable challenge before
  operational Telegram exposure.** `test_challenges.py` (24 tests) —
  versioned triggers on R1–R4, bounded turns, Python-owned effects,
  complete `ChallengeRecord` persistence; Telegram stays feature-flag
  gated (`TelegramConfig.enabled=False` default,
  `test_config_rejects_shared_secrets_at_startup` enforces startup
  validation before any exposure).
- [x] **Telegram sends the final report, including `NO TRADE` when
  applicable.** `test_telegram.py` render/dispatch tests over the canonical
  renderer (escaping, 4096-chunking, disclaimer).
- [x] **A run has zero to three READY recommendations; every evaluated
  candidate appears exactly once across decision buckets.**
  `test_orchestrator.py::test_full_run_completes_and_persists_without_telegram`,
  `test_no_qualified_candidates_returns_no_trade`; `RunResult.validate()`
  enforces the partition invariant on every finalize.
- [x] **A run never invents data, news, foreign/broker flow, `bandar`
  activity, or missing evidence.** `test_agents.py` hallucination/claim
  guards; `test_deployment.py::test_smoke_run_passes_real_pipeline_validation`
  (shadow payloads pass the same rejection rules).

## Analytical integrity

- [x] **All indicator, setup, score, level, risk, status, and rank values
  originate in Python or the original source.** `test_policy.py` (25
  tests) + `test_contracts.py` (25) — forbidden agent fields
  (`score/price/entry/stop/target/rank`) rejected at the boundary.
- [x] **Agent responses are schema-validated, evidence-checked, and
  prevented from returning forbidden numerical fields.**
  `test_agents.py::test_forbidden_fields_rejected` and evidence-citation
  tests (`_check_evidence_citations` rejects ids absent from the envelope).
- [x] **The immutable fact hash is unchanged from pre-agent snapshot
  through final result.** `assert_snapshot_integrity` runs before every
  service run (`test_orchestrator.py` replay tests;
  `test_deployment.py::test_smoke_run_replays_with_zero_calls`).
- [x] **Historical/replay runs are protected from look-ahead and
  point-in-time universe/news leakage.** `test_scheduler.py::test_explicit_as_of_backfill_matches_manual_replay`
  + `test_backfill_refuses_stale_source` (as_of must match the requested
  date); completed-candle cutoff tested in snapshot tests.
- [x] **READY/WAIT/REJECT outcomes, MFE, MAE, and holding period are
  reproducible.** `test_backtest.py` (17 tests) — deterministic entry,
  two-target exit, gap, stop, same-bar rules.
- [x] **Matched ablations, agent metrics, evaluation-basket drawdown, and
  all four required baselines can be generated.** `test_evaluation.py`
  (28 tests) — five date-matched arms, IHSG / buy-and-hold / seeded-random
  / frozen-legacy baselines, basket drawdown, byte-reproducible report.

## Operational

- [x] **A complete run can be reconstructed from SQLite after process
  restart without rerunning the LLM.**
  `test_second_service_construction_same_day_replays`,
  `test_smoke_run_replays_with_zero_calls` — fresh service objects on the
  same store file; zero transport calls; identical payload.
- [x] **Duplicate/missed scheduler runs, timeouts, malformed agent output,
  stale market data, and provider outages have tested behavior.**
  `test_scheduler.py` (38 tests: duplicate, missed, stale gate,
  breaker OPEN with fast-fail) + `test_orchestrator.py` outage tests
  (PARTIAL/NOT_EVALUATED, never NO_TRADE) + malformed-output rejection in
  `test_agents.py`.
- [x] **Logs/metrics identify run/stage and alert on defined thresholds
  without exposing secrets.** `test_stage_metrics_are_structured_and_secret_free`,
  `test_health_report_identifies_failed_stage_without_secrets`,
  `test_alert_thresholds_are_defined_per_stage` — `STAGE_OWNERS` +
  `ALERT_THRESHOLDS`; redaction on token/key/secret-named fields.
- [x] **Configuration/schema compatibility, Telegram authorization, and
  durable backup/restore are enforced.** `test_scheduler_refuses_incompatible_pipeline`
  + `test_scheduler_validates_scheduler_config` (per-run compatibility);
  `test_deployment.py` config-version/secret gates; sender+chat allowlists
  in `test_telegram.py`; `AuditStore.backup/restore` in `test_store.py`.
- [x] **An operator runbook covers failures, manual replay, and rollback
  to the legacy screener, and its recovery drill passes.**
  `agent_based/RUNBOOK.md` §2.1–2.6 + §3 drill log — the drill scenarios
  are automated in `test_scheduler.py` (holiday, duplicate, missed run,
  stale source, Hermes outage, process restart).

## Acceptance verdict

All 17 checkboxes verified. Two deployment notes stand:

1. **Shadow mode**: every current run is `CACINGNAGA_SMOKE_V1`-tagged; the
   checklist assertions are transport-independent by design.
2. **Catalyst layer**: forward-paper only (plan item 13) — no
   provenance-proven archived news exists, so catalysts stay UNKNOWN(0)
   and are excluded from historical claims (see POLICY_DECISIONS.md).
