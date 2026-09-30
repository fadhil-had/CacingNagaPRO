# DEPLOY — Running the AI Analyst Team in production

How to take `AI_TEAM_DAILY_V1` from checkout to a scheduled, monitored,
recoverable deployment. Companion docs: `HANDOFF.md` (system overview,
promotion gate), `RUNBOOK.md` (incidents), `README.md` (architecture),
`config/ai_team.toml` (the file this guide configures).

Current mode: **shadow** — every agent turn runs on the deterministic
`CacingNagaSmokeTransport` and is tagged `CACINGNAGA_SMOKE_V1` in the audit
trail. The deployment steps below are mode-independent; swapping in a real
Hermes transport later changes one wiring point (§7), nothing else.

---

## 1. Prerequisites

| Requirement | Detail |
|---|---|
| Host | Any Linux box/VM/runner with outbound HTTPS (yfinance, Telegram API) |
| Python | **3.14** (verified: pandas 3.0.5, numpy 2.5.1, pytest 9.1.1) |
| Dependencies | `pip install -r requirements.txt` |
| Repository | Clone at the deploy path, e.g. `/opt/cacingnaga` (all paths below are relative to it) |
| Universe file | `resource/daftar-saham.xlsx` must exist (or switch `scheduler.universe.source = "fixed"`) |
| Disk | A few hundred MB; the audit store grows with runs (WAL mode) |
| Timezone | Not required on the host — the pipeline computes WIB internally via the frozen legacy `IDX_TZ` |

Sanity check before anything else:

```bash
python3.14 -m pytest agent_based/ tests/ -q   # 275 passed = healthy checkout
```

## 2. Configure

All non-secret settings live in **`config/ai_team.toml`** (version-pinned
`DEPLOYMENT_CONFIG_1`; the loader refuses mismatched versions, bad TOML,
invalid universes, and shared secrets at startup). Review every section:

- `[screener]` — turnover/price/ATR/history thresholds (defaults match the
  benchmark screener; change only with a recorded policy decision).
- `[scheduler]` — close window (`close_hour/minute` = 16:00 WIB),
  `earliest_run_after_close_minutes` cooldown, `look_back_days` for
  missed-run detection, and `extra_holidays` for decree closures announced
  after the built-in 2025–2026 calendar was frozen.
- `[scheduler.universe]` — `source = "excel"` + `excel_path`, or
  `source = "fixed"` + `tickers = ["BBCA.JK", ...]`; `period = "2y"`
  (≥ 260 daily bars required).
- `[ops]` — circuit-breaker bounds (`failure_threshold`, `cooldown_seconds`,
  `half_open_probes`).
- `[telegram]` — `enabled` stays **false** until §6; allowlists; the
  token env-var *name*.
- `[storage]` — `store_path` (default `output/audit_store.sqlite3`).

### Secrets (environment only — never in the file)

```bash
# .env-style or your secret manager; three distinct domains:
export TELEGRAM_BOT_TOKEN="..."        # only if §6 is enabled
export HERMES_API_TOKEN="..."          # only for a real transport (§7)
export LEGACY_GEMINI_API_KEY="..."     # legacy screener, if that feature is used
```

Two domains sharing a value is a configuration error: startup fails closed
with `secret separation violated` before anything runs.

## 3. Verify the wiring (no schedule yet)

```bash
python3 scripts/ai_team.py health            # five surfaces report OK
python3 scripts/ai_team.py screen            # one manual shadow run end-to-end
```

`screen` proves the whole chain — live fetch through the frozen-legacy
loaders → snapshot → agents (smoke) → policy → store — without touching the
scheduler gates. Confirm the run appears in the store and prints
`COMPLETE / TOP_3` (or `NO_TRADE`); expect the `CACINGNAGA_SMOKE_V1` tag on
agent outputs.

Then rehearse the gate logic with the drill clock (`--now`):

```bash
python3 scripts/ai_team.py run --now "2026-12-25T19:00:00"   # holiday → skip, exit 0
python3 scripts/ai_team.py run --now "$(date +%Y-%m-%d)T12:00:00"  # midday → DEFERRED, exit 0
python3 scripts/ai_team.py run --now "$(date +%Y-%m-%d)T19:00:00"  # evening → real run
```

Exit codes: **0** ran/replayed/safe skip · **1** config failure · **2**
degraded run (page per RUNBOOK §2).

## 4. Schedule the daily run

The scheduler itself enforces: trading day (weekends + IDX holidays +
`extra_holidays`), market close (16:00 WIB + cooldown), duplicates
(idempotent replay, zero calls), missed-run reporting (never auto-backfill),
and stale data (as_of must cover the session). Your cron job only needs to
fire *after* close and let the gates decide.

### Cron (recommended, simplest)

```cron
# /etc/cron.d/cacingnaga-ai-team  — fire 18:30 WIB (11:30 UTC) Mon–Fri
30 11 * * 1-5  cd /opt/cacingnaga && /usr/bin/python3.14 scripts/ai_team.py run >> output/ai_team_cron.log 2>&1
```

Every weekday invocation is safe: holidays defer, already-run sessions
replay from the store, missed days are reported (`SKIP_MISSED_RUNS`) rather
than silently skipped — the log line tells you which.

### GitHub Actions (alternative)

Reuse the existing `financial_screener.yml` pattern: add a schedule entry
`30 11 * * 1-5` to a *new* workflow that runs
`python3 scripts/ai_team.py run`. Keep `financial_screener.yml` untouched —
it is the rollback target (§8).

### Backfill after downtime (manual, explicit)

Do **not** re-fire old crons. For each missed session:

1. Edit nothing; call the API path instead — write a short script that
   builds a point-in-time snapshot and hands it to the scheduler:

   ```python
   from deployment import load_deployment_config, ai_analyst_config, load_live_snapshot
   from cacingnaga.store import AuditStore
   from scheduler import Scheduler
   # ... build the service like scripts/ai_team.py::_build_service
   Scheduler(cfg, store, factory, scheduler_config=deploy.scheduler).run_backfill(
       "2026-09-21", lambda *, analysis_date, as_of: load_live_snapshot(...)
   )
   ```

   The backfill refuses any snapshot whose `as_of` ≠ requested date and
   refuses non-trading days.
2. Point-in-time data availability is your responsibility here: the
   snapshot must be built from frames as they existed on that date.

## 5. Monitor

```bash
python3 scripts/ai_team.py health
```

Surfaces, owners, and thresholds live in `agent_based/ops.py`
(`STAGE_OWNERS`, `ALERT_THRESHOLDS`): market data (snapshot age > 3 days,
failed candidates > 10), hermes (stage > 120 s, failure rate > 20%),
storage (unreachable → FAIL), scheduler (missed runs / skips → DEGRADED),
telegram (failure rate > 30%). The report names the failed stage and owner
and can never contain secret values (redaction on token/key/secret-named
fields — enforced by tests).

Wire `health` into alerting: run it on a schedule, alert on exit code 1
(FAIL) and on `DEGRADED` lines mentioning `scheduler` or `hermes`. Keep
cron logs; the structured `gate decision:` line at the top of every `run`
is your audit of skip/defer/execute decisions.

## 6. Enable Telegram (optional, last)

Only after the daily run has been stable in shadow for your acceptance
window:

1. Create the bot with BotFather; put the token in `TELEGRAM_BOT_TOKEN`.
2. Fill `allowed_user_ids` and `allowed_chat_ids` in `config/ai_team.toml`
   (both must pass for any command).
3. Set `[telegram] enabled = true`.
4. Run as a **separate long-lived service** — never inside the scheduled
   job: `python3 scripts/ai_team.py telegram` (under a supervisor:
   `systemd`/`pm2`/`tmux`).
5. Commands: `/screen /analyze /market /status /why /debate /help` —
   store-backed, single-flight, HTML-escaped, chunked under 4096 chars.

## 7. Swap in the real transport (post-shadow)

When the Hermes runtime is confirmed (decision #16):

1. Implement `AgentTransport.complete()` (see `cacingnaga/transport.py`
   for the contract; `CacingNagaSmokeTransport` in
   `agent_based/deployment.py` is the reference shape — return
   `AgentResponse(raw_text, payload, usage, latency_ms)`).
2. Replace the smoke transport in `scripts/ai_team.py::_build_service`.
   Everything else — retries, circuit breaker, response cache,
   validation, persistence — already wraps the transport generically.
3. Set `HERMES_API_TOKEN`; run `screen` once and confirm agent outputs now
   carry the real `usage.model`.
4. Promotion still requires the Phase 7 evaluation gate
   (`full_decision` ≥ screener + 0.5 `average_net_return_pct`, frozen
   criteria) — see `HANDOFF.md` §5.

## 8. Rollback

Any time, no data loss: disable the AI cron entry (or the new workflow)
and rely on the untouched legacy schedule in
`.github/workflows/financial_screener.yml` — `scripts/financial_screener.py`
keeps producing the legacy outputs it always has. AI-run history stays in
`output/audit_store.sqlite3` for later evaluation. Full procedure:
`RUNBOOK.md` §2.6.

## 9. Backup / restore the audit store

```bash
python3 - <<'EOF'
from datetime import date
from pathlib import Path
from cacingnaga.store import AuditStore

Path("backups").mkdir(exist_ok=True)
AuditStore("output/audit_store.sqlite3").backup(f"backups/audit-{date.today()}.sqlite3")
EOF
# restore (stop writers first):
#   AuditStore.restore("backups/audit-YYYY-MM-DD.sqlite3", "output/audit_store.sqlite3")
```

Nightly backup right before the cron window is sufficient; the store is
append-only and replays rebuild identical state from stored records.
