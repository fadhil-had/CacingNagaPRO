# HANDOFF — CacingNagaPRO AI Analyst Team (AI_TEAM_DAILY_V1)

Handoff record required by plan §15 (Definition of Done). Read this top to
bottom before operating the system. Companion documents:
`agent_based/README.md` (architecture), `agent_based/RUNBOOK.md`
(incidents), `agent_based/MVP_ACCEPTANCE.md` (checklist record),
`agent_based/cacingnaga/POLICY_DECISIONS.md` (decision log),
`agent_based/EXECUTION_POLICY.md` (frozen backtest execution rules).

## 1. What this system is

A deterministic-first IDX swing-trading pipeline: **Python calculates.
Agents interpret. Decision Agent challenges. Backtest judges.** No LLM
computes status, score, levels, or rank anywhere. One run per completed
IDX session produces zero-to-three READY recommendations (or a run-level
NO_TRADE), persisted idempotently in SQLite with full provenance.

Current mode: **configurable**. Agent turns run either on the live Gemini
transport (`cacingnaga/llm_transport.py` — one routed model per agent, each
record tagged `gemini-live` with its real model id) or on the deterministic
`CacingNagaSmokeTransport` (tagged `CACINGNAGA_SMOKE_V1`). `auto` selects live
only when `[llm] enabled` is set **and** `$AGENT_LLM_API_KEY` is exported;
otherwise the run stays in shadow. The audit tag always records which provider
actually served a run.

A Phase 9 consultation round (`agents/peer_review.py`) can put the Technical and
Flow agents in the same conversation: each sees the other's reading plus the
Market reading for the same candidate and may revise. It is opt-in
(`peer_review = true` under `[agents]`) and never degrades a run — a failed
consultation keeps the first-pass reading.

## 2. Operator quick-start

Repository root, Python 3.14 (pandas 3.0.5):

```bash
python3.14 -m pytest agent_based/ tests/ -q   # 333 green = healthy checkout
python3 scripts/ai_team.py health             # five-surface ops report
python3 scripts/ai_team.py run                # scheduled post-market job
python3 scripts/ai_team.py screen             # manual run
python3 scripts/ai_team.py screen --transport smoke   # force deterministic
python3 scripts/ai_team.py telegram           # gated bot service (off by default)
```

Configuration: `config/ai_team.toml` (validated at startup; version-pinned
`DEPLOYMENT_CONFIG_1`; **no secret values in the file** — only env-var
names). Secrets: `TELEGRAM_BOT_TOKEN`, `AGENT_LLM_API_KEY`,
`LEGACY_GEMINI_API_KEY` — three distinct domains; sharing a value fails
closed at startup.

Exit codes for `run`/`screen`: **0** = ran, replayed, or documented safe
skip; **1** = configuration/contract failure (nothing started); **2** =
degraded run (PARTIAL/FAILED) — page the stage owner from
`ops.STAGE_OWNERS` per RUNBOOK §2.

## 3. Where things live

| Concern | Location |
|---|---|
| Entrypoint | `scripts/ai_team.py` |
| Deployment config | `config/ai_team.toml` + `agent_based/deployment.py` |
| Scheduler/gates/backfill | `agent_based/scheduler.py` |
| Ops (breaker, metrics, health, secrets) | `agent_based/ops.py` |
| Orchestration | `agent_based/orchestrator.py` (`AnalysisService`) |
| Contracts/policy/store/snapshot | `agent_based/cacingnaga/` |
| Analyst agents | `agent_based/agents/` |
| Live LLM transport (per-agent model routing) | `agent_based/cacingnaga/llm_transport.py` |
| Cross-agent consultation round | `agent_based/agents/peer_review.py` |
| Telegram MVP | `agent_based/telegram_layer/` (feature-flagged off) |
| Audit store | `output/audit_store.sqlite3` (WAL; system of record) |
| Tests (263 pipeline) | `agent_based/cacingnaga/tests/` |
| Frozen legacy benchmark | `scripts/screener_*.py` + `tests/` (12 tests) — **never modify** |

## 4. Operational invariants (violating any of these is a bug)

1. PARTIAL/FAILED runs are never presented as NO_TRADE; they recommend
   nothing (§4 item 7).
2. READY requires mandatory inputs, a valid risk plan, a trigger, and no
   unresolved hard conflict; a resolved material conflict lifts the WAIT
   cap only up to Python's own gate verdict.
3. The snapshot hash is verified before every run and every replay; a
   mismatch fails closed.
4. Replays make **zero** provider calls and rebuild byte-identical results
   from stored validated outputs.
5. The scheduler never auto-backfills; catch-up is the explicit
   `run_backfill(as_of=...)` with a point-in-time snapshot.
6. A stale snapshot (`as_of` ≠ session) records an auditable FAILED row
   and runs no agent.
7. The frozen legacy screener and its 12 tests stay untouched; the
   rollback target is `scripts/financial_screener.py` on the unchanged
   `financial_screener.yml` schedule.

## 5. Promotion gate (shadow → live)

1. Build the Phase 7 evaluation report (`run_evaluation`) and confirm the
   frozen promotion criterion: `full_decision` beats the current screener
   baseline by ≥ 0.5 points on `average_net_return_pct` with sufficient
   samples. The report is byte-reproducible; store it with the decision.
2. Enable the live transport (it ships in `cacingnaga/llm_transport.py`):
   set `[llm] enabled = true` in `config/ai_team.toml` and export
   `$AGENT_LLM_API_KEY`. Timeouts/retries/breaker already wrap it. Confirm one
   `screen --transport live` carries `transport: "gemini-live"` and a real
   per-agent `model` in `agent_outputs.usage`.
3. Keep `TelegramConfig.enabled=False` until the challenge contract and a
   live drill both pass; then enable with allowlists populated.
4. Roll forward only after a full `run` drill on a real session; roll
   back per RUNBOOK §2.6 at any time with no data loss (AI runs stay in
   the audit store).

## 6. Known limitations

- Catalysts are forward-paper (UNKNOWN, scored 0): no provenance-proven
  archived news source exists (plan item 13).
- The scheduler gates and pipeline are live-data-ready, and the live LLM
  transport ships with per-agent model routing, but recommendations are only
  model-driven once `[llm] enabled` is set and a key is exported; until then
  runs stay on the deterministic smoke transport.
- Telegram exposure is off by default; `/screen` on the bot runs the
  fixture snapshot in v1 (`build_screen_runner`), to be pointed at
  `load_live_snapshot` at promotion.
- yfinance instability is mitigated, not eliminated: freshness gates and
  the stale-data gate fail closed rather than label stale data current
  (verified live 2026-09-28: a blocked network produced a clean exit 1
  with `IHSG download returned no data` — never a stale run).

## 7. Handoff checklist

- [x] All 15 plan sections implemented or explicitly deferred (§14 list).
- [x] 333 tests green (321 pipeline + 12 frozen legacy), Python 3.14.
- [x] MVP Acceptance Checklist recorded with verifying tests
      (`MVP_ACCEPTANCE.md`).
- [x] Runbook with completed drill (`RUNBOOK.md` §3).
- [x] Rollback path tested (`test_legacy_schedule_files_untouched_by_scheduler`,
      legacy suite green on every run).
- [x] Decision log current through the deployment layer
      (`POLICY_DECISIONS.md`).
- [x] Deployment guide written (`DEPLOY.md`) and its drill commands
      verified against the real entrypoint.
