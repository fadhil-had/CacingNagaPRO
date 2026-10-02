# CacingNagaPRO AI Analyst Team — Operations Runbook

Scope: the scheduled post-market run produced by `agent_based/scheduler.py`,
its operational surfaces (`agent_based/ops.py`), and the fallback to the
frozen legacy screener (`scripts/`). Ownership map lives in
`ops.STAGE_OWNERS`; alert thresholds in `ops.ALERT_THRESHOLDS`.

**Non-negotiables (PRD §4):** no LLM ever computes status/score; PARTIAL and
FAILED are never shown as NO_TRADE; the store is the only system of record;
the rollback target is the frozen legacy screener.

## 1. Daily schedule model

Operate through the deployment entrypoint (repository root):

```bash
python3 scripts/ai_team.py run       # scheduled post-market job
python3 scripts/ai_team.py health    # five-surface ops report
python3 scripts/ai_team.py screen    # manual run
python3 scripts/ai_team.py telegram  # gated bot service

# transport selection (default: auto)
python3 scripts/ai_team.py screen --transport live    # require the live LLM
python3 scripts/ai_team.py screen --transport smoke   # require the shadow stub
```

Exit codes: `0` = ran / replayed / safe skip; `1` = configuration or
contract failure; `2` = degraded run (PARTIAL/FAILED) — page the on-call
owner for that stage. Configuration lives in `config/ai_team.toml`
(validated at startup; secrets stay in environment variables).
Shadow runs are tagged `CACINGNAGA_SMOKE_V1` in the audit trail — never
mistake them for live-provider output. Live runs are tagged `gemini-live`
with the real per-agent `model` id. Check the tag before trusting any reading.

## 1a. Requirements

Three secret domains, all environment-only. **Never** put a value in the
config file or the repo — the config stores env-var *names* only.

| Env var | Needed for | Required? |
|---|---|---|
| `AGENT_LLM_API_KEY` | Live agent turns (`[llm] enabled = true`) | Only for live runs |
| `TELEGRAM_BOT_TOKEN` | Telegram bot, and only if `[telegram] enabled = true` | No — bot is off by default |
| `LEGACY_GEMINI_API_KEY` | Secret-separation check only | No — never read by the pipeline |
| `GEMINI_API_KEY` | The *frozen legacy* screener's own AI call (`scripts/financial_screener.py`) | Only for the legacy path |

Rules that apply to all of them:

- **`AGENT_LLM_API_KEY` is your Google AI Studio / Gemini API key.** "Hermes" is
  the internal codename for this project's agent transport, not a vendor — it
  has no key of its own. The name was changed from `HERMES_API_TOKEN` to
  `AGENT_LLM_API_KEY` because the old name sent people looking for a product
  that does not exist. If you have an existing config, either rename the key or
  pin the old name with `api_key_env = "HERMES_API_TOKEN"`.
- **The three domains must hold three different values.** Sharing one value
  across two domains fails closed at startup with `secret separation
  violated` — this is deliberate (a leaked token must not silently
  authenticate the wrong subsystem). See `ops.validate_secret_separation`.
- `pip install google-genai` must be installed for live runs
  (`requirements.txt` already lists it). Without it, live mode refuses to
  start rather than silently degrading.
- `LEGACY_GEMINI_API_KEY` and `GEMINI_API_KEY` are **different variables**:
  the first is only read by the separation check, the second by the frozen
  legacy script.

### Cost: it is free, but the daily request cap is the real limit

Google's **Free tier** bills input *and* output at "free of charge" for both
default models, and no credit card is required. What actually constrains a
daily run is the **requests-per-day (RPD)** cap, not money. Limits are per
project and reset at midnight Pacific time; exact numbers are shown in AI
Studio and change often, so check there before scaling up.

Call budget per run, where `N` = candidate pool size (`policy.max_agent_pool_size`,
default 30):

| Stage | Calls | On |
|---|---|---|
| Market | 1 | 3.8 Flash |
| Technical + Flow pass 1 | 2N | Flash-Lite |
| Decision | up to N | 3.8 Flash |
| Consultation (only if `peer_review = true`) | 2N | 3.8 Flash |

| `N` | `peer_review` | Calls/run |
|---|---|---|
| 10 | off | ~31 |
| 10 | on | ~51 |
| 30 | off | ~91 |
| 30 | on | ~151 |

**A single run fits the Free tier comfortably; several runs in one day may not.**
One run per trading day is the design point. If you enable `peer_review` on a
large pool and start hitting 429s, lower `max_agent_pool_size` first — the
screener narrows the pool *before* any agent call, so it is the cheapest lever.

One caveat on the Free tier specifically: prompts and responses **may be used
to improve Google's products**. That is irrelevant for market data you already
publish, but worth knowing before pointing it at anything confidential.

Non-secret prerequisites:

| Requirement | Detail |
|---|---|
| Python | **3.14** |
| Dependencies | `pip install -r requirements.txt` (includes `google-genai`) |
| Universe file | `resource/daftar-saham.xlsx`, or set `scheduler.universe.source = "fixed"` |
| Outbound HTTPS | yfinance (market data) and the Gemini API |
| Disk | Audit store grows with runs (SQLite WAL) |

Minimal live setup:

```bash
pip install -r requirements.txt
export AGENT_LLM_API_KEY="..."   # your Google AI Studio key
# config/ai_team.toml:  [llm] enabled = true   and   [agents] peer_review = true
python3 scripts/ai_team.py screen --transport live
```

Verify it worked — every record in `agent_outputs.usage` must show
`transport: "gemini-live"` plus a real `model`, never `CACINGNAGA_SMOKE_V1`:

```bash
sqlite3 output/audit_store.sqlite3 \
  "SELECT agent_name, json_extract(payload_json,'\$.usage.model') AS model
   FROM agent_outputs ORDER BY created_at DESC LIMIT 10;"
```

To go back to the deterministic stub at any time, without editing the config:

```bash
python3 scripts/ai_team.py screen --transport smoke
```

## 1b. Pipeline flow

```
                      ┌─────────────────────────────────────┐
                      │ scripts/ai_team.py run|screen       │
                      └──────────────┬──────────────────────┘
                                     │
                    ┌────────────────▼─────────────────┐
                    │ load_deployment_config()         │  startup gate:
                    │  · TOML valid + version pinned   │  bad config ⇒ exit 1,
                    │  · secret separation enforced    │  nothing started
                    │  · llm bounds validated          │
                    └────────────────┬─────────────────┘
                                     │
                    ┌────────────────▼─────────────────┐
                    │ Scheduler.should_run()           │  trading day?
                    │  SKIP_NON_TRADING_DAY / DEFERRED │  ⇒ exit 0 (safe skip)
                    │  duplicate ⇒ replay (0 agent calls)│
                    │  SKIP_MISSED_RUNS ⇒ page owner   │
                    └────────────────┬─────────────────┘
                                     │ due
                    ┌────────────────▼─────────────────┐
                    │ load_live_snapshot()             │  frozen-legacy loaders
                    │  download_ihsg / download_saham  │  only; STALE ⇒ fail
                    │  ⇒ build_snapshot (facts+evidence)│  closed
                    └────────────────┬─────────────────┘
                                     │
                    ┌────────────────▼─────────────────┐
                    │ create_run() — idempotency key    │  dedups BEFORE any
                    │  (snapshot+config+date hash)      │  agent call
                    └────────────────┬─────────────────┘
                                     │
        ╔════════════════════════════▼════════════════════════════╗
        ║  AGENT PHASE — Python computes, agents interpret only    ║
        ╟─────────────────────────────────────────────────────────╢
        ║ 1. MarketAgent      gemini-3.8-flash  ── market slice   ║
        ║ 2. TechnicalAgent   gemini-3.5-flash-lite┐ per candidate   ║
        ║    FlowAgent        gemini-3.5-flash-lite┘ (independent)   ║
        ║                                                         ║
        ║    ══ peer_review = true ══                            ║
        ║ 3. Each analyst sees the OTHER's reading                 ║
        ║    + Market reading, then confirms or revises           ║
        ║    · claims cross, raw facts stay isolated              ║
        ║    · same contract ⇒ no status/score from an LLM        ║
        ║    · failure keeps pass-1 reading, run NOT degraded     ║
        ╚════════════════════════════╤═══════════════════════════╝
                                     │
                    ┌────────────────▼─────────────────┐
                    │ DecisionAgent    gemini-3.8-flash │  proposes status
                    │  fixed evidence packet + policy  │  over the packet
                    └────────────────┬─────────────────┘
                                     │
                    ┌────────────────▼─────────────────┐
                    │ Python: conflicts.py → risk →    │  ★ deterministic:
                    │ policy.py → merge.py             │    status, score,
                    │                                  │    rank, risk plan
                    └────────────────┬─────────────────┘
                                     │
                    ┌────────────────▼─────────────────┐
                    │ MATERIAL_CONFLICT?               │  yes ──▶ ChallengeAgent
                    │                                  │          (gemini-3.5-flash-lite)
                    │                                  │          debate ≤4 turns
                    │                                  │          DecisionAgent-
                    │                                  │          Resolution →
                    │                                  │          status_effect
                    └────────────────┬─────────────────┘
                                     │ no conflict
                    ┌────────────────▼─────────────────┐
                    │ finalize_run() → RunResult       │  READY (≤3, ranked)
                    │  COMPLETE | PARTIAL | FAILED     │  WAIT | REJECT | NO_TRADE
                    └────────────────┬─────────────────┘
                                     │
                    ┌────────────────▼─────────────────┐
                    │ AuditStore (system of record)    │  append-only SQLite:
                    │ runs · candidates · decisions ·  │  WAL, idempotent,
                    │ agent_outputs (incl. peer turns)  │  every turn stored
                    │ challenges · snapshots           │
                    └────────────────┬─────────────────┘
                                     │
                    ┌────────────────▼─────────────────┐
                    │ exit 0 ok | 2 if PARTIAL/FAILED  │  ⇒ page stage owner
                    └───────────────────────────────────┘  (RUNBOOK §2)
```

Read the diagram as two guarantees rather than two stages:

- **Everything inside the AGENT PHASE box interprets.** Every enum, reason,
  and citation comes from a model; the numbers around them come from Python.
- **Everything below the box disposes.** No LLM computes a status, score,
  price, or rank anywhere in the flow, and PARTIAL/FAILED is never displayed
  as NO_TRADE.

- One run per IDX trading day, only after the close gate passes
  (16:00 WIB + `earliest_run_after_close_minutes`).
- Weekends and IDX holidays are `SKIP_NON_TRADING_DAY` (calendar in
  `scheduler.IDXCalendar`; extra closures go in `extra_holidays`
  inside `config/ai_team.toml`).
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
3. A **live** outage (Gemini quota/auth/network) surfaces as the same
   `ContractViolation` from `GeminiLLMTransport.complete`; the breaker and
   retry bounds are unchanged, so the recovery path is identical.
4. Recovery: after the cooldown the breaker half-opens with one probe;
   no operator action is required. If the outage persists past
   `ALERT_THRESHOLDS["hermes"]["max_stage_seconds"]`, roll back (2.6).
5. Fastest containment while diagnosing: `--transport smoke` on the next
   run. That forces the deterministic stub without editing the config, so
   operations continue while the provider issue is investigated.
6. A **consultation-only** outage (`peer_review = true`) is *not* an outage
   incident: the first-pass readings are kept, the run stays COMPLETE, and
   the failed turns are recorded as `agent_outputs.validation_ok=0` for
   `TechnicalAgentPeerReview` / `FlowAgentPeerReview`. Check those agent
   names to distinguish "enrichment lost" from "run degraded".

### 2.3 Malformed agent output

1. Validation is fail-closed: a malformed payload is rejected by the
   agents/runner and recorded in `agent_outputs.validation_ok=0` with the
   error; no partially-trusted reading ever reaches policy.
2. If one candidate fails → PARTIAL run; if the Decision Agent fails →
   PARTIAL/NOT_EVALUATED (provider failure, §4 item 10).
3. Owner reviews the stored `validation_error`, fixes the prompt/schema,
   and replays with a fresh run id — never mutates the stored run.
4. On a **live** run, check `agent_outputs.usage.normalized` first: it lists
   any case/shape repairs the transport applied before validation. A run full
   of repairs means the prompt or the response schema drifted, not that the
   model misread the evidence. Enum tokens are only repaired when they match a
   contract token after case normalization — an unrecognized value is passed
   through and rejected, never guessed into a valid reading.

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
| 2026-10-02 | Phase 9: per-agent model routing resolves a distinct model per agent; strict-JSON schema pinned per agent; missing API key fails closed with an actionable message; provider error / empty text / non-JSON / JSON-array all refused; consultation turn cannot smuggle status, score, price, or a foreign ticker; fact slices stay isolated; fabricated evidence id rejected; failed consultation keeps pass-1 reading and leaves the run COMPLETE; both turns persisted for audit | **PASS** (automated: `test_llm_transport.py`, `test_peer_review.py`) |
