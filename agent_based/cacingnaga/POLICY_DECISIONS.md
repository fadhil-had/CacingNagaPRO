# CacingNagaPRO — Policy Decision Record (Phase 0)

Status: **Proposed → Adopted for Phase 0/1 code** · Date: 2026-09-25
Source: `Backlog/improvement_v3_implementation_plan.md` §4 (Section 4 reconciliation items).

Each decision below resolves one Section 4 item and names the code that owns it.

| # | Topic | Decision (v1) | Code owner |
|---|-------|----------------|------------|
| 1 | Legacy/new policy boundary | Legacy screener stays frozen as `FINAL_DAILY_D10_WEEKLY_MONTHLY`; new pipeline is `AI_TEAM_DAILY_V1`. Legacy is a benchmark, never relabeled. | `versioning.py`, `legacy_adapter.py` |
| 2 | Decision state semantics | Candidate states: `READY` / `WAIT` / `REJECT`. `NO_TRADE` is **run-level only** (complete run, zero READY). | `contracts.py::NO_TRADE_TOKEN`, run invariants |
| 3 | Run failure semantics | Run states `RUNNING` / `COMPLETE` / `PARTIAL` / `FAILED`; `PARTIAL`/`FAILED` ⇒ `NOT_EVALUATED`, never `NO_TRADE`. | `contracts.py::RunResult._validate_invariants` |
| 4 | Market regime values | Strict enum `BULLISH` / `NEUTRAL` / `BEARISH`. Nuance (e.g. `NEUTRAL-BULLISH`) lives in `secondary_direction`, not in `regime`. | `contracts.py::MarketInterpretation` |
| 5 | Market benchmark | `^JKSE`, `Asia/Jakarta`, IDX completed-candle cutoff (16:10 WIB buffer) reused from legacy `screener_core`. | `config.py`, `snapshot.py::completed_cutoff` |
| 6 | IDX universe | Frames/universe are injected by the caller (offline replay). Point-in-time universe auditing is deferred to Phase 2B. | `snapshot.py::build_snapshot(universe=...)` |
| 7 | Analysis cutoff | Every snapshot carries `as_of` (last completed candle); partial today-candle is dropped via legacy cutoff. | `snapshot.py` |
| 8 | Price basis | `auto_adjusted` (yfinance), recorded in every provenance record; single basis for analysis and future backtests. | `contracts.py::CandidateProvenance.price_basis` |
| 9 | Risk levels | Python calculates and validates entry/stop/TP1/TP2 (long-only ordering rules); agents may challenge, never alter. | `contracts.py::RiskLevels.validate` |
| 10 | Trade direction | **Long-only** in v1. Config rejects `long_only=False`. Shorts need a formal PRD amendment. | `config.py::AIAnalystConfig.validate` |
| 11 | Ranking | Python owns score components, weights, status, rank. Agents return constrained categories only; `FORBIDDEN_AGENT_FIELDS` enforced at the deserialization boundary. **Phase 2:** implemented in `policy.py` (`DecisionPolicy`, PRD §14 weights, configurable). | `contracts.py`, `policy.py` |
| 12 | Confidence | `LOW`/`MEDIUM`/`HIGH` bands only. No uncalibrated numeric confidence in v1. | `contracts.py::CONFIDENCE_BANDS` |
| 13 | Unknown component scoring | Unavailable data ⇒ explicit `UNKNOWN` / `None` + availability flags; never silently neutral. Degenerate indicator windows have documented conventions (e.g. MFI=100 on zero-loss windows). **Phase 2:** `UnknownComponentPolicy` (conservative 0.0, recorded; or reject) — weights never renormalized. | `contracts.py` (facts), `snapshot.py`, `config.py` |
| 14 | Catalyst source | No validated news source in v1 ⇒ catalyst stays `UNKNOWN`; no headline fabrication. **Phase 2:** catalyst weight (10%) scores 0.0 with an explicit audit note. | `contracts.py::CatalystInterpretation`, `policy.py` |
| 15 | Candidate pool | Screener returns the full eligible pool; stable preliminary ordering (score desc, ticker asc); only then a configurable cap (`max_agent_pool_size`, default 30 ≥ 4). | `snapshot.py::build_snapshot` |
| 16 | Hermes runtime | **Not yet confirmed** — no package guessed. **Phase 3 done:** provider-neutral `AgentTransport` (`transport.py`) + deterministic `FakeTransport`; Market/Technical/Flow agents run entirely over the fake transport with no SDK import anywhere. The real Hermes adapter plugs into the same protocol in Phase 4 without touching the agents. | `transport.py`, `agents/` |
| 17 | Orchestration ownership | Python owns run lifecycle/validation; agent-call orchestration arrives with the confirmed Hermes runtime (Phase 4). | `snapshot.py` (lifecycle-lite for now) |
| 18 | Scheduler | Deferred to Phase 8. Snapshots are schedulable via `as_of` replay. | deferred |
| 19 | Persistence | **Phase 2A done:** SQLite is the system of record (`store.py`); versioned schema via `PRAGMA user_version`, WAL, FK on; runs idempotent by `hash(pipeline, date, snapshot_hash, config_hash)`; snapshot payload + hash persisted *before* any agent call and verified recomputable from the DB; state machine `RUNNING → COMPLETE/PARTIAL/FAILED` enforced in code and CHECK constraints; JSON/CSV/Markdown exporters stay legacy-compatible. Durable volume/deployment choice deferred to Phase 8. | `store.py`, `exporters.py` |
| 20 | Evaluation window | Deferred to Phase 2B/7; evaluation protocol must be frozen before AI promotion. | deferred |

## Phase 2 additions (2026-09-25)

- **RiskPlanCalculator** (`risk.py`): long-only entry/stop/TP1/TP2 from
  structure + ATR, IDX tick rounding, 2–5% stop and 3–10% target mandates,
  ≤10-session hold. Invalid plans return `None` + reasons (fail-closed).
- **DecisionPolicy** (`policy.py`): six normalized score components with
  documented ranges, PRD §14 weights (config-only), deterministic hard gates,
  score-desc/ticker-asc ranking, top-3 promotion, WAIT/REJECT derivation,
  and `finalize_run` producing invariant-checked `RunResult`s.
- **Execution-policy spec** (`EXECUTION_POLICY.md`): entry fill, gap,
  simultaneous TP/SL pessimism, two-target accounting — frozen before any
  Phase 2B backtest adapter.
- Evidence floors: flow/market components score 0.0 + audit note when their
  facts are unavailable; catalyst is 0.0 in v1 (no source). Nothing is
  silently treated as evidence-backed neutral.

## Phase 3 additions (2026-09-27)

- **Agent boundary (plan §7 Phase 3)**: agents receive only the minimal
  envelope slice (market-only for Market; facts-only / flow-only per
  candidate), must cite evidence ids that exist in that slice, and are
  rejected for injected `FORBIDDEN_AGENT_FIELDS`, ticker mutation,
  fabricated evidence ids (in prose or refs), or citation-free claims.
- **`UNKNOWN` ≠ neutral at the agent layer too**: Flow defaults to
  `UNKNOWN`/`UNKNOWN`, `UNKNOWN` flow can never carry `HIGH` confidence
  (contract), and assertive bandar/foreign/broker certainty claims are
  rejected while disclaimers are allowed.
- **Bounded turns** (`TransportConfig`): timeout budget, 1–5 attempts, backoff;
  exhaustion ⇒ `FAILED` (or `PARTIAL` when the budget ran out first) with the
  redacted error persisted — never a fabricated recommendation.
- **Failure isolation**: per-candidate failure ⇒ `PARTIAL`; Market failure ⇒
  `FAILED` (market is mandatory for every downstream decision).
- **Prompt/response audit**: prompt hash, redacted raw response, validated
  payload, latency, and usage metadata persist per turn via
  `AuditStore.save_agent_output`.

## Phase 4 additions (2026-09-27)

- **Decision ownership (plan §7 Phase 4 item 6)**: the Decision Agent
  proposes one of READY/WAIT/REJECT over a fixed evidence packet;
  `agent_proposed_status` is recorded verbatim. Python derives
  `final_status` via the merge: conflict effects first, upgrade veto
  (favorability floor = Python's derivation), justified downgrades accepted
  (reason required), failed-turn floor keeps Python's status.
- **Conflict rules are versioned code** (`CONFLICT_RULES_1`,
  `conflicts.py`): HARD_INVALIDATION blocks READY, MATERIAL_CONFLICT caps at
  WAIT (the Phase 6 challenge trigger), MINOR_CAUTION is surface-only.
  Same inputs ⇒ same conflicts, so triggers are reproducible and replayable.
- **Hermes-outage policy (§4 item 10)**: any Decision Agent failure degrades
  the run to `PARTIAL`/`NOT_EVALUATED` — never `NO_TRADE`, never a local
  improvised recommendation. Analyst-phase failures keep the Phase 3 rules
  (candidate failure ⇒ PARTIAL, market failure ⇒ FAILED).
- **Replay contract**: a terminal run re-triggered with the same
  snapshot/config replays from the stored synthesis with zero transport
  calls and reproduces an identical run result, decision records, and
  conflicts (tested, including the tuple/list JSON round-trip).

## Phase 5 additions (2026-09-27)

- **Telegram is a render/adapter, never a decision path (plan §7 Phase 5)**:
  the bot core is a pure dispatcher over the audit store; all five MVP
  commands render the canonical stored result. `/why` never re-runs an LLM
  and never parses messages; rendering cannot mutate decisions (tested).
- **Authorization on every command**: sender AND chat allowlists; unauthorized
  callers receive a rejection without data.
- **`/screen` single-flight + idempotency**: overlapping triggers get an
  in-progress notice; actual screening always goes through
  `AnalysisService`'s idempotency key, so concurrent Telegram/CLI/scheduler
  triggers converge on one stored run.
- **Operational enablement gated (item 11)**: exposure is off until
  `CACINGNAGA_TELEGRAM_ENABLED=1` AND the Phase 6 challenge contract passes;
  enabling additionally requires a bot token and a non-empty allowlist.
  Deploy as a separate long-lived service; never inside the scheduled job.
- **Package naming**: `telegram_layer` (importing as `telegram` would shadow
  `python-telegram-bot` itself).
- **Store threading**: `AuditStore(thread_safe=True)` serializes one SQLite
  connection for handler threads; default behavior unchanged.

## Phase 6 additions (2026-09-27)

- **Challenge trigger is versioned and deterministic (plan §7 Phase 6
  item 1)**: `CHALLENGE_RULES_1` — debates fire only from known conflict
  rule ids (R1–R4) with MATERIAL/HARD severity. Benign low-confidence
  differences produce no `Conflict` and can never trigger. An explicit
  `/debate TICKER` additionally debates that ticker's MINOR-caution
  conflicts; it cannot invent a debate where no conflict was detected.
- **Claim boundary (items 2–4)**: challenged agents answer a focused
  question with SUPPORT/REVISE/WITHDRAW over their own validated reading,
  strict `ChallengeResponse` schema (evidence citations required, missing
  data acknowledged, forbidden fields rejected). No new prices, scores, or
  tickers can enter the debate.
- **The Decision Agent classifies; Python disposes (items 5, 7)**:
  `python_status_effect` recomputes the effect from the classification AND
  the recorded stances — a claimed REVISED without an actual
  revise/withdraw stance is UNRESOLVED. Effect vocabulary is
  NONE / CAP_AT_WAIT / BLOCK_READY; READY is structurally unreachable. A
  failed resolution turn serializes as the canonical UNRESOLVED stub.
- **Lift semantics**: a genuinely resolved material conflict lifts the
  Phase 4 WAIT cap only up to Python's own gate verdict (hard gates keep
  their floor) — a debate can resolve a disagreement, never override a
  gate.
- **Bounded debates (item 9)**: one round, `CHALLENGE_MAX_TURNS=2` attempts
  per agent; a failed debate ends CONSERVATIVE (UNRESOLVED + preconfigured
  effect) and never fails the run — the run completes with the conflict
  standing.
- **Resolution-turn identity**: the Decision Agent's resolution turn
  requests under `DecisionAgentResolution` so transports/tests route it
  independently of Phase 4 proposals, but persists under the Decision
  Agent name (one agent, two turn types).
- **Persistence (item 6)**: complete `ChallengeRecord` (rule versions,
  packet, per-turn views, raw rows via `save_agent_output`, resolution,
  Python effect, deterministic `CH-…` id) in the `challenges` table;
  `CandidateDecision.challenge_ref` and the `decisions` rows reference it;
  replays rebuild records with zero transport calls.
- **Telegram (item 8)**: `/debate TICKER` shows the concise stored debate
  when one exists (explicit no-debate message otherwise); `/why` surfaces
  the debate reference. Both read the store only.
- **Gate status**: the Phase 6 challenge contract now passes (192 pipeline
  tests green, 2026-09-27). The remaining operational gate is the operator
  flag `CACINGNAGA_TELEGRAM_ENABLED=1` plus token/allowlist, per Phase 5.

## Phase 7 additions (2026-09-27)

- **Accepted status is configuration, not code (item 1)**:
  `BacktestConfig.accepted_status` ∈ {READY, WAIT}; REJECT is refused
  (diagnostic only). Status semantics stay distinct (items 5–6): WAIT rows
  carry `NOT_TAKEN` plus a separately labeled trigger counterfactual filled
  at the plan's entry-zone floor; unfilled entry-zone recommendations stay
  `NOT_EXECUTED`; REJECT returns never enter performance metrics.
- **Trade records are outcome-complete (items 3, 4, 7)**: fill date/price,
  TP1/TP2/SL hit dates, `actual_result` ∈ {WIN, LOSS, FLAT}, gross and
  cost-adjusted returns, holding sessions, per-position IHSG benchmark;
  MFE/MAE measured from the actual entry fill over the same evaluation
  window. Execution rules remain the frozen EXECUTION_POLICY v1 set —
  unchanged when the accepted status changes.
- **Every signal carries its provenance (item 2)**: `SIGNAL_RECORD_1` pins
  policy version, consulted agents, challenge reference, prompt hash,
  model reference, config hash, and the trade row — so ablations and
  metrics reproduce from stored recommendations over aligned dates/costs
  (exit criterion 3).
- **Ablations are date-matched, pool-matched, and weight-only (item 9)**:
  five arms (`no_ai`, `technical_only`, `technical_flow`,
  `technical_flow_market`, `full_decision`) run the same point-in-time
  snapshots on the same dates; only `ScoreWeights` differ per arm
  (renormalized to sum 1.0) — gates, risk plans, and costs are identical,
  so agent value is isolated by matched counterfactuals, not final P&L.
- **Baselines are deterministic (item 11)**: IHSG horizon, fee-free
  buy-and-hold, seeded random eligible picks (seed pinned at 42), the
  frozen legacy screener run point-in-time through the read-only adapter
  (never mutating `scripts/`), and the deterministic policy replay.
- **Drawdown is an evaluation construct (item 10)**: maximum drawdown on a
  documented equal-weight basket of executed trades (chronological equity
  of net returns). Explicitly not production position sizing or portfolio
  management.
- **Promotion criteria frozen before selection (item 12)**:
  `EvalCriteria` pins minimum sample (30 executed trades), horizon (10
  sessions), costs (fee 0.2% / slippage 0.1% per side), and the promotion
  rule (full_decision must beat the current screener by ≥ 0.5 points on
  `average_net_return_pct`). Frozen criteria are immutable; unknown
  metrics and under-sampled arms never promote; an unfrozen criteria
  object refuses to promote at all.
- **Catalysts stay forward-paper (item 13)**: no archived point-in-time
  news source with publication/retrieval provenance exists, so the
  catalyst component remains UNKNOWN (scored 0) and catalysts are excluded
  from historical claims.
- **One reproducible entry point (item 14)**: `run_evaluation` emits
  `EVALUATION_REPORT_1` (byte-identical for identical inputs) containing
  status rates, WAIT confirmation, losses avoided by REJECT, basket
  drawdown, all ablation arms, all baselines, the promotion decision, and
  the historical-results disclaimer. No AI improvement is claimed on
  synthetic fixture data.

## Phase 8 additions (2026-09-28)

- **The scheduler adds zero new state semantics (items 1–2)**:
  `agent_based/scheduler.py` is a deterministic gate in front of the
  existing `AnalysisService` path. It decides *whether* and *with which
  run id* a run starts; idempotency, lifecycle transitions, and
  persistence remain the store's alone. Safe states are explicit and
  skippable: `SKIP_NON_TRADING_DAY`, `DEFERRED` (market close gate),
  `SKIP_ALREADY_RUN` (replay, zero agent calls), `SKIP_MISSED_RUNS`
  (never auto-catch-up), `SKIP_STALE_DATA` (auditable FAILED row, no
  agent call).
- **One timezone, one calendar (item 2)**: `IDX_TZ` is imported from the
  frozen legacy screener through the compatibility adapter — the new
  pipeline cannot compute a different WIB wall clock than the benchmark.
  The IDX holiday calendar (`IDX_HOLIDAYS_2025_2026`) is explicit,
  versioned data, extendable per deployment via `extra_holidays`
  (unexpected decree closures) without touching the frozen set.
- **PARTIAL/FAILED sessions count as executed**: the missed-run detector
  treats any terminal run as covering its session. A degraded run must
  not cause unbounded automatic catch-up; recovery from degraded runs is
  a runbook action, not a scheduler reflex.
- **Backfill is manual, explicit, and hash-checked (item 7)**:
  `run_backfill(date, source)` requires a snapshot built for that exact
  `as_of` from historical frames and refuses any snapshot whose `as_of`
  does not match (never silently reuse current data for a historical
  date). The scheduler hands the exact snapshot to the service factory so
  a backfill never borrows another run's transport bindings.
- **Circuit breaker reopens on a failed probe (item 3)**: OPEN → (cooldown)
  → HALF_OPEN admits a bounded probe budget; a failed probe re-opens with
  a fresh cooldown, a success closes. The breaker wraps the provider
  transport only — validation, policy, and status effects stay in Python
  beneath it. The response cache is request-keyed (canonical request
  hash) and process-local, so replays stay deterministic; it never
  bypasses validation.
- **Metrics never carry secrets (item 4)**: `stage_metrics` redacts any
  field whose name contains token/secret/password/key/api_key. Every
  stage has a named owner (`STAGE_OWNERS`) and threshold
  (`ALERT_THRESHOLDS`); alerts identify the failed stage only — never a
  credential.
- **Secret separation fails closed (item 5)**: three domains, three
  environment variables (`TELEGRAM_BOT_TOKEN`, `HERMES_API_TOKEN`,
  `LEGACY_GEMINI_API_KEY`). A value shared between two domains is a
  configuration error (a leaked token must not silently authenticate the
  wrong subsystem).
- **Compatibility is re-verified per run (item 6)**: the scheduler checks
  `pipeline_version == PIPELINE_VERSION` and validates both the config
  and the scheduler config before accepting any run.
- **Rollout keeps the legacy job (item 9)**: `financial_screener.yml` is
  untouched; the new `ai_team_tests.yml` workflow only runs the package
  tests. Rollback (runbook §2.6) disables the AI trigger and the bot and
  returns daily operation to the frozen legacy screener with no data
  loss — AI runs remain in the audit store for later evaluation.

## Frozen legacy baseline

`tests/` (12 tests) pins the legacy screener/backtest behavior. Phase 0 exit
criterion: **all 12 legacy tests pass unchanged** after introducing the
`agent_based/cacingnaga` package — verified 2026-09-25 (24 new + 12 legacy green).

## Contract-freeze outputs (Phase 0 deliverables)

- Contracts as typed dataclasses with executable validation: `contracts.py`
  (JSON Schema generation can be added later from the same models).
- Strict enums + `UNKNOWN` semantics: `contracts.py` header block.
- One named Python owner per numerical field: fact classes above.
- Fixture snapshots (bullish / conflict / stale): `fixtures.py`.
- Decision record: this file.
