# CacingNagaPRO AI Analyst Team — Implementation Plan

- **Status:** Proposed
- **Date:** 25 September 2026
- **Source PRD:** [`improvement_v3.md`](./improvement_v3.md)
- **Target release:** MVP (four agents, Telegram, deterministic analytics, and evaluation hooks)

---

## 1. Objective

Deliver the CacingNagaPRO MVP as a deterministic-first, auditable swing-trading analysis pipeline:

1. Python collects and validates market data.
2. Python calculates indicators, setup features, risk levels, and ranking inputs.
3. Hermes orchestrates the Market, Technical, Flow, and Decision agents.
4. Agents interpret precomputed evidence without changing numerical facts.
5. Telegram exposes the run status and final report.
6. Every run and recommendation is persisted for later backtest evaluation.

The implementation must support `READY`, `WAIT`, and `REJECT` decisions and may return run-level `NO TRADE`; it must never force three recommendations.

---

## 2. Delivery Principles

1. **Python calculates.** All OHLCV-derived indicators, levels, scores, and risk calculations remain deterministic code.
2. **Agents interpret.** LLM calls receive structured evidence and return schema-constrained assessments.
3. **The system is auditable.** Every recommendation links to its input snapshot, prompts/configuration, agent outputs, and validation results.
4. **Optional data degrades explicitly.** Missing breadth, sector, foreign-flow, broker-flow, or news data becomes `UNKNOWN`; it is never inferred.
5. **Build vertical slices.** Each phase produces a testable capability rather than a layer that has no end-to-end value.
6. **Prefer reuse.** Extend proven repository components before introducing parallel pipelines or dependencies.
7. **Fail closed.** An incomplete or invalid analysis may produce `WAIT`/run failure, but must not become a fabricated `READY` decision.

---

## 3. Repository Baseline and Migration Strategy

### What exists today

The current application is a flat Python 3.12 screener:

- `scripts/financial_screener.py` is the CLI and owns the frozen daily policy.
- `scripts/screener_ranking.py` adds ranking features and currently limits the final watchlist to three names.
- `scripts/screener_core.py` contains yfinance collection, most indicators, market breadth, reports, and the existing Gemini integration.
- These files use dynamic imports, exported-symbol copying, and ranking-hook patching. `tests/backtest_data.py` depends on that API, so an immediate in-place package refactor would risk both CLI and backtest behavior.
- `.github/workflows/financial_screener.yml` already runs Python 3.12 tests and schedules stateless daily/weekly/monthly jobs, but its short-lived artifacts are not a durable store for AI runs or a long-running Telegram bot.
- No Hermes runtime or multi-agent package exists. Existing Gemini functions are a legacy, unstructured reporting layer and must not silently become the Decision Agent.
- The current daily result is an equal-confidence `Watchlist D+10`; entry, stop, and target are intentionally unavailable. Existing flow resources under `resource/research_v11/` and `resource/research_v14/` are currently header-only, and `resource/research_v11/sector_membership.csv` has no sector records. These inputs must remain `UNKNOWN` until populated and validated.

### Reusable foundations

- Reuse `download_ihsg`, `download_saham_batch`, `download_satu_sahm`, completed-candle cutoff logic, breadth, EMA/RSI/MACD/ATR/ADX, prior highs, support/resistance, volume statistics, and current-universe handling from `scripts/screener_core.py`.
- Reuse `analisa_market_regime`, `analisa_saham_confluence`, `finalisasi_score_dan_status`, the full candidate stage inside `_screen_timeframe`, and deterministic ranking features from `scripts/screener_ranking.py`.
- Reuse causal snapshot history, next-session execution, fees/slippage, holding-period exits, capacity, duplicate-ticker, and point-in-time universe logic from `tests/backtest_engine.py` and `tests/backtest_data.py`.
- Keep current CSV, Markdown, and JSON exports for compatibility while adding a durable run store for new pipeline records.

### Required migration boundary

Do not replace or relabel the current screener in place. Preserve it as a versioned benchmark and add a separate `AI_TEAM_DAILY_V1` policy:

- The existing D+10 watchlist remains `FINAL_DAILY_D10_WEEKLY_MONTHLY` behavior.
- Existing names without a validated trigger, entry, stop, and targets map to `WAIT` in the new pipeline, never `READY`.
- `READY` becomes eligible only after the new deterministic setup, trigger, risk, and hard-gate policy passes offline validation.
- The new candidate pool must return all eligible names before a configurable agent cap; the current top-three watchlist limit cannot be reused as the pre-agent pool.
- Existing `scripts/financial_screener.py`, `scripts/screener_ranking.py`, and `scripts/screener_core.py` remain compatibility surfaces until the new importable package passes the legacy CLI and backtest tests.

---

## 4. Decisions Required Before Contract Freeze

Resolve and record these items in ADR/config documentation before implementing dependent agents.

**Normative precedence:** this plan does not silently override the draft PRD. The recommended resolutions in this section and the contracts in Section 6 are proposed v1 interpretations. They must be approved and backported into the PRD's schemas/examples before the affected code is implemented. Until then, the affected contracts are an implementation gate, not permission to choose behavior in code.

Known reconciliation items are: explicit `UNKNOWN` values for missing Flow/Catalyst data; Python ownership of technical score/levels and final status/rank; strict market-regime values; run-level `NO_TRADE`; and separation of complete/partial/failed run states.

| Topic | Required decision | Recommended v1 resolution |
|---|---|---|
| Legacy/new policy boundary | Prevent the current D+10 watchlist from being mislabeled as actionable | Keep `FINAL_DAILY_D10_WEEKLY_MONTHLY` frozen; build and version `AI_TEAM_DAILY_V1` separately |
| Decision state semantics | Clarify whether `NO TRADE` is per candidate or per run | Use candidate states `READY`, `WAIT`, `REJECT`; reserve `NO_TRADE` for a completed run with zero `READY` candidates |
| Run failure semantics | Distinguish a market decision from an incomplete system run | Use `COMPLETE`, `PARTIAL`, and `FAILED`; never label a provider/system failure as `NO_TRADE` |
| Market regime values | Reconcile `BULLISH/NEUTRAL/BEARISH` with the `NEUTRAL-BULLISH` examples | Use the strict enum in the schema; store nuance in separate direction/evidence fields rather than a free-form regime |
| Market benchmark | Confirm the yfinance symbol for IHSG and timezone | Standardize `^JKSE`, `Asia/Jakarta` timestamps, and the IDX trading calendar in one data module |
| IDX universe | Decide how symbols and active listings are maintained | Version `resource/daftar-saham.xlsx`/Yahoo mappings and point-in-time membership; never infer active listings ad hoc |
| Analysis cutoff | Define which completed candles may be used | Include `as_of`, requested lookback, and last completed trading date in every run snapshot |
| Price basis | Choose adjusted/unadjusted semantics for indicators, levels, and execution | Store the basis explicitly and use one documented basis consistently in live analysis and backtests |
| Risk levels | Clarify who calculates entry, stop, TP1, and TP2 | Python calculates and validates all levels; agents may challenge consistency but cannot alter them |
| Trade direction | Confirm whether v1 supports shorts | Keep the MVP long-only unless the PRD is formally amended |
| Ranking | Define how deterministic scoring and AI interpretation interact | Python owns normalized components, weights, status, and rank; agents supply constrained categorical assessments only |
| Confidence | Avoid treating an uncalibrated LLM number as objective | Store `LOW/MEDIUM/HIGH` bands in v1; use fixed, documented point mappings if ranking needs confidence |
| Unknown component scoring | Define Catalyst/Flow behavior when data is unavailable | Store component availability separately; by default assign the configured conservative score (often zero), record it, and do not silently call it neutral or renormalize weights |
| Catalyst source | Select a free, attributable news source or mark the feature unavailable in MVP | Default to no-news/`UNKNOWN`; require source URL, publication time, and retrieval time when enabled |
| Candidate pool | Define deterministic screener output size sent to agents | Return all eligible candidates, assign a stable preliminary rank, then cap the agent pool by configurable token/candidate budget |
| Hermes runtime | Identify the exact SDK, CLI, or service intended by “Hermes Agent” | Do not guess a package; implement a provider-neutral transport and freeze its concrete adapter after the runtime is confirmed |
| Orchestration ownership | Separate lifecycle coordination from agent-call orchestration | Python owns application/run lifecycle and validation; Hermes coordinates constrained agent calls |
| Scheduler | Define the market-close schedule and holiday behavior | Run once after a completed IDX session; persist skipped/failed runs and prevent duplicate triggers |
| Persistence | Select the audit store, deployment volume, and retention policy | Use SQLite for the single-service MVP, with a durable volume/database; retain CSV/Markdown/JSON as exports |
| Evaluation window | Define when there is enough evidence to assess AI value | Freeze a minimum date-matched sample/horizon and promotion criterion before comparing policy variants |

### Contract-freeze outputs

- Versioned JSON Schemas for every deterministic and agent-produced object.
- Enumerated states and confidence semantics.
- A documented calculation policy for each numerical field.
- At least three fixture snapshots: bullish setup, conflicting setup, and missing-data setup.
- A decision record for optional data sources and any `UNKNOWN` behavior.

---

## 5. Target Architecture

```text
Telegram / CLI / Scheduler
          │
          ▼
      AnalysisService
      ├── run lifecycle and idempotency
      └── persistence/replay boundary
          │
          ▼
  Deterministic Python Engine
  ├── Market data collector
  ├── Indicator and flow feature engine
  ├── Candidate screener
  ├── Setup classifier
  ├── Risk-plan calculator
  └── Score components
          │ versioned immutable facts
          ▼
    Hermes Agent Runtime
    ├── Market Agent
    ├── Technical Agent
    └── Flow Agent
          │
          ▼
   Python Conflict Detector
          │ significant conflict
          ▼
  Hermes Decision Agent / Challenge Round
          │ constrained proposed status and rationale
          ▼
    Python Decision Policy
    ├── hard-gate validation
    ├── final status and rank
    └── top-three/no-trade enforcement
          │
          ├── canonical result and audit records
          ▼
     Telegram Renderer
```

### Boundary rules

- The Telegram layer does not calculate indicators or call data sources directly.
- Agents do not fetch market data during an agent call; all facts come from the immutable upstream snapshot.
- The Decision Agent receives calculated risk levels, score components, and allowed outcomes; it supplies a constrained proposed status and reasoning, not authoritative numbers or rank.
- Python conflict detection runs outside the LLM and triggers a challenge deterministically.
- Python finalizes status and rank only after validating hard gates, model output, and the unchanged snapshot hash.
- Rendering never changes a decision or numerical value.
- Persistence receives the canonical result, not messages reconstructed from Telegram text.

---

## 6. Data Contracts

Use typed models and generate JSON Schema from those models where the repository's tooling permits. Keep three contract layers separate:

1. **Immutable facts** calculated or sourced by Python.
2. **Constrained interpretations** returned by agents.
3. **Final decisions** produced by Python policy after validation.

All JSON numbers must be finite. Use `null` plus an explicit availability field for missing data; never serialize `NaN` or infinity.

### 6.1 Run envelope

Required fields:

- `schema_version`
- `pipeline_version` (initially `AI_TEAM_DAILY_V1`)
- `run_id` and idempotency key
- `analysis_date`, `as_of`, and timezone-aware `created_at`
- `market_symbol` and data freshness
- `universe_version`, `code_version`, and `config_hash`
- `data_snapshot_hash`
- `triggered_by` and optional `correlation_id`

### 6.2 Immutable facts and provenance

Python-owned fact objects:

- `MarketFacts`
- `TechnicalFacts`
- `FlowFacts`
- `CatalystFacts`
- `RiskLevels`
- `CandidateProvenance`

Each source record includes source identity/URL when applicable, retrieval time, data basis, available dates, freshness, and quality warnings. Calculate a canonical hash before any agent call and verify it again before finalization.

### 6.3 Market interpretation

Agent output:

- `regime`: strict enum
- optional secondary direction/strength fields for nuance
- `confidence_band`: `LOW | MEDIUM | HIGH`
- `swing_environment`
- `reasons[]`
- `risk_flags[]`
- `evidence_refs[]`

Optional breadth and sector data have separate availability fields so absence cannot be mistaken for neutral evidence. A numeric confidence may be stored only after a documented calibration method exists.

### 6.4 Technical interpretation

Agent output:

- `ticker`
- `trend`, `setup`, and `momentum` constrained enums
- `reasons[]`
- `risks[]`
- `evidence_refs[]`
- `missing_facts[]`

Support, resistance, entry zone, invalidation, indicator values, and `technical_score` remain Python-owned facts. The agent explains these facts and may not return replacement values. The Python setup classifier supplies the canonical setup label under `AI_TEAM_DAILY_V1`.

### 6.5 Flow interpretation

Agent output:

- `ticker`
- `flow`: `ACCUMULATION | NEUTRAL | DISTRIBUTION | UNKNOWN`
- `strength`: `STRONG | MEDIUM | WEAK | UNKNOWN`
- `confidence_band`
- `evidence[]`, `risks[]`, and `evidence_refs[]`
- `available_indicators[]` and `missing_indicators[]`

`UNKNOWN` is required when directional evidence is unavailable; it is distinct from evidence-backed `NEUTRAL`. If only OHLCV-derived flow is available, the output describes an indication—not proof of activity by Indonesian market traders colloquially called `bandar`, foreign investors, or brokers.

### 6.6 Catalyst interpretation

Agent output when a validated source is configured:

- `catalyst`: `POSITIVE | NEUTRAL | NEGATIVE | UNKNOWN`
- `impact`: `HIGH | MEDIUM | LOW | UNKNOWN`
- `freshness`
- `reasons[]`
- `items[]`, each with source, publication time, retrieval time, and a short evidence excerpt

When no source is configured, return `catalyst=UNKNOWN`, `impact=UNKNOWN`, `freshness=UNKNOWN`, and a data-unavailable reason. Never use `NEUTRAL` or `LOW` impact to mean “not checked” and never fabricate a headline. The Python score policy records how unknown catalyst/flow components receive points.

### 6.7 Risk plan

Python-owned fields:

- `entry_low`, `entry_high`
- `stop_loss`
- `tp1`, `tp2`
- `maximum_holding_days`
- `risk_per_share`
- `risk_reward_tp1`, `risk_reward_tp2`
- `invalidation`
- `risk_flags[]`

The plan records formulas/rules and IDX tick rounding. It rejects invalid combinations such as a stop above entry, TP1 above TP2, a long target below entry, or percentage bands outside policy.

### 6.8 Candidate decision

Required fields:

- `ticker`
- `agent_proposed_status`: `READY | WAIT | REJECT`
- `final_status`: `READY | WAIT | REJECT`
- `confidence_band`
- Python-owned `score_components`, `score`, and `rank`
- `reasons[]`, `risks[]`, `conflicts[]`, and `evidence_refs[]`
- `challenge_ref` and `risk_plan_ref`

The final result must be reproducible by replaying the same validated agent outputs, immutable facts, configuration, and policy version.

### 6.9 Challenge record

Required fields:

- `challenge_id` and candidate/run reference
- `trigger_rule_version` and conflict type
- `questions[]`
- per-agent raw response, validated response, and evidence references
- `resolved`/`unresolved` conflicts
- `status_effect`
- final Decision Agent resolution and validator result

Challenge replies use a dedicated schema derived from each analyst's interpretation contract, not the full initial-analysis contract.

### 6.10 Run result and invariants

Required fields:

- `run_id`
- `market`
- `recommendations[]`, `wait[]`, and bounded `rejected[]`
- `run_status`: `RUNNING | COMPLETE | PARTIAL | FAILED`
- `decision_outcome`: `PENDING | TOP_3 | NO_TRADE | NOT_EVALUATED`
- `warnings[]`, `created_at`, and `completed_at`

`NO_TRADE` is the machine token; renderers may display the PRD label `NO TRADE`.

Executable invariants:

- `recommendations[]` contains only final `READY` candidates and has zero to three entries.
- `RUNNING` maps to `decision_outcome=PENDING` and has zero final recommendations until finalization.
- `COMPLETE` with zero READY candidates maps to `decision_outcome=NO_TRADE`.
- `COMPLETE` with one to three READY candidates maps to `decision_outcome=TOP_3`.
- `PARTIAL` and `FAILED` runs always map to `decision_outcome=NOT_EVALUATED`, contain zero recommendations, and may list observed candidates only as `WAIT` or `REJECT` with failure reasons.
- Every evaluated candidate appears exactly once across `recommendations`, `wait`, and `rejected`.
- READY ranks are unique and contiguous from 1; WAIT/REJECT ranks are `null`.
- A final numerical value must equal the immutable Python fact it references.
- Empty candidate pools are successful `NO_TRADE` outcomes when mandatory market data is valid.

---

## 7. Implementation Phases

## Phase 0 — Specification and Safety Harness

### Objective

Make the numerical and decision boundaries executable before connecting an LLM.

### Work

1. Record the decisions in Section 4, including long-only scope and the legacy/new policy boundary.
2. Identify the exact Hermes runtime and define a provider-neutral transport contract; do not guess a package named `hermes`.
3. Define versioned models/JSON Schemas, strict enums, and separate facts/interpretations/final-decision contracts.
4. Define a compact agent input envelope and a larger, human-auditable audit envelope.
5. Select configuration format, package/dependency tooling, and versioning rules.
6. Create representative deterministic fixtures without live network calls.
7. Add a canonical JSON/hash helper so stored and model-adjacent values can be checked against the input snapshot.
8. Define run states, candidate states, unknown-component scoring, failure, retry, and fallback behavior.
9. Freeze the current CLI/backtest tests as the legacy compatibility baseline before moving code.

### Deliverables

- Contract schemas and configuration schema
- Confirmed Hermes integration contract
- Fixture snapshots and contract tests
- Legacy/new policy decision record
- Architecture/decision documentation

### Exit criteria

- All required enums and `UNKNOWN` semantics are unambiguous.
- Every numerical output has one named Python owner.
- A fixture round-trips through serialization without changing values.
- Invalid states, missing evidence, and forbidden agent fields are rejected.
- Existing legacy CLI and backtest tests pass unchanged.

---

## Phase 1 — Data, Indicators, and Screener Foundation

### Objective

Produce one reliable, versioned analysis snapshot for IHSG and a bounded IDX candidate pool.

### Work

1. Wrap and reuse the current `screener_core.py` collectors, completed-candle cutoff, universe, breadth, EMA/RSI/MACD/ATR/ADX, volume, support/resistance, and prior-high logic before moving them.
2. Centralize symbol normalization, `^JKSE`, `Asia/Jakarta`, IDX trading-calendar handling, price basis, and `as_of` behavior.
3. Add source/retrieval/freshness/quality metadata and per-ticker failures; never silently substitute stale or mismatched data.
4. Add missing PRD features: Bollinger middle/upper/lower, band width, percent-b, recent high/low series, OBV/slope, MFI, CMF, accumulation/distribution line, and up/down volume.
5. Validate the current flow/catalyst resources before use. Header-only or unvalidated files remain `UNKNOWN`; foreign and broker flow are unavailable in initial v1 fixtures.
6. Implement named deterministic setup classifiers for breakout, pullback, reversal, continuation, range, failed breakout, and breakdown.
7. Expose a reusable full-candidate stage from `_screen_timeframe`; apply liquidity, price, volume, momentum, volatility, and setup screens without the legacy top-three limit.
8. Assign stable preliminary ranks, record total eligible/selected/skipped/failed counts, and only then apply the configurable agent-pool cap.
9. Keep Gemini research discovery outside the eligible recommendation pool.
10. Add look-ahead protections, point-in-time universe auditing, and as-of replay tests.
11. Cache raw/derived data with source/version metadata where safe.

### Deliverables

- Importable data/feature package plus legacy adapter
- Versioned `AnalysisSnapshot` and source manifest
- IHSG and full candidate-pool snapshots
- Data-quality report and source availability matrix
- Fixture/replay dataset for every required indicator and setup

### Exit criteria

- A deterministic snapshot can be generated without Hermes or Telegram.
- Repeated runs over the same fixture/config produce identical values and ordering.
- Missing/invalid candles and insufficient history produce explicit warnings/errors.
- Empty flow/sector files are reported unavailable, not as zero/neutral evidence.
- The pre-agent pool is larger than the final recommendation cap and has a documented bound.
- No future candle or current-universe record can leak into a historical snapshot.

---

## Phase 2 — Deterministic Risk and Ranking Layer

### Objective

Give agents a complete, auditable decision substrate rather than asking them to perform calculations.

### Work

1. Implement long-only eligibility for v1; shorts require a PRD amendment.
2. Calculate active entry/entry zone, structural invalidation, stop, TP1, TP2, risk per share, and reward/risk from support/resistance, ATR, and configured limits.
3. Validate stop/target ordering, 2%–5% stop and 3%–10% target mandates, IDX tick rounding, maximum 10-session hold, and liquidity constraints.
4. Define entry fill, gap, stop, target, and simultaneous TP/SL assumptions for backtesting before agent rollout.
5. Normalize score components to one documented range and add fixed mappings for constrained agent categories.
6. Implement configurable PRD weights: technical, flow, market fit, risk/reward, catalyst, and liquidity; record unknown-component treatment explicitly.
7. Reject invalid/incomplete weight configurations and unavailable component policies at startup.
8. Define deterministic tie-breaking, rank continuity, and top-three/no-trade enforcement.
9. Add deterministic hard gates for eligibility, trigger, risk validity, evidence quality, and unresolved hard conflicts.
10. Keep market fit configurable as a score/variant first; do not silently turn the current diagnostic IHSG result into a hard veto.

### Deliverables

- `AI_TEAM_DAILY_V1` policy and `RiskPlan` calculator
- Score components, hard gates, and configurable deterministic ranker
- Execution-policy specification and boundary tests

### Exit criteria

- Agent prompts never need to recompute an indicator, level, score, or status rule.
- Invalid risk plans fail validation and cannot become `READY`.
- Weight/unknown-component changes require configuration only.
- Same inputs/configuration produce the same status and ranking.
- The policy can return zero eligible candidates without failure.

---

## Phase 2A — Durable Snapshot and Audit Store

### Objective

Create the minimum durable/idempotent boundary before any model executes, while keeping the initial store small.

### Work

1. Add SQLite migrations/repository for runs, source manifests, market/candidate snapshots, agent outputs, challenges, decisions, recommendations, and outcomes.
2. Store queryable columns plus complete versioned JSON payloads.
3. Persist snapshot/config/code/prompt/model hashes and source provenance.
4. Implement run states and idempotent create/upsert behavior before Hermes calls.
5. Keep CSV, Markdown, and JSON exporters for legacy and artifact compatibility.
6. Define a durable deployment volume/database; GitHub Actions' 90-day artifacts are not the system of record.

### Deliverables

- SQLite schema/migrations and repository API
- Run-state/idempotency implementation
- Legacy-compatible exporters
- Backup/restore and persistence test fixtures

### Exit criteria

- A fixture run survives process restart and can be queried without rerunning analysis.
- A repeated trigger returns the existing run rather than duplicating records.
- Legacy exporters still work.
- The snapshot hash stored before the agent phase is reproducible from SQLite.

---

## Phase 2B — Offline Policy Backtest

### Objective

Prove the deterministic new policy and its no-READY behavior before AI interpretation is enabled.

### Work

1. Make the backtest's accepted signal status/policy configurable instead of hardcoding `Ready to Enter`.
2. Reuse causal history, next-session execution, fees/slippage, capacity, duplicate handling, and point-in-time universe behavior.
3. Implement two-target accounting, unfilled entries, WAIT counterfactuals, and diagnostic-only REJECT outcomes.
4. Compare the frozen screener with `AI_TEAM_DAILY_V1` before/without AI over aligned dates.
5. Record all execution assumptions and freeze the evaluation sample/horizon before promotion.

### Deliverables

- Policy-aware backtest adapters
- Two-target and excursion outcome tests
- Deterministic baseline report for `AI_TEAM_DAILY_V1`
- Frozen evaluation protocol

### Exit criteria

- READY, WAIT, REJECT, zero-eligible, unfilled, gap, and same-bar outcomes are deterministic and tested.
- Existing backtest compatibility tests still pass.
- A report shows that no watchlist name is relabeled READY without valid levels/trigger.
- Agent contribution is not claimed from this deterministic-only comparison.

---

## Phase 3 — Agent Contracts and Individual Analysts

### Objective

Run the first three agents independently on the same immutable snapshot.

### Shared agent infrastructure

1. Implement the provider-neutral `AgentTransport` interface and the confirmed Hermes SDK/CLI/HTTP adapter; local tests use a fake transport.
2. Require strict structured output conforming to each interpretation schema and reject forbidden fields such as model-supplied price, entry, stop, target, score, ticker mutation, or rank.
3. Validate every result and recompute the immutable snapshot hash before synthesis/finalization.
4. Send only the necessary snapshot subset, with a stable ID for every fact/evidence item.
5. Require reasons to cite evidence IDs; reject unsupported claims where feasible.
6. Add bounded timeouts/retries and a controlled `PARTIAL`/`FAILED` outcome on exhaustion; never substitute a fabricated recommendation.
7. Persist prompt/schema/model/runtime version, latency, token/usage metadata, raw response, and validation result through the Phase 2A store.
8. Give agents narrow snapshot/submit functions only if Hermes requires tools; do not expose arbitrary shell, filesystem, database, or HTTP tools.
9. Treat news/article text as quoted untrusted data and redact credentials/personal data from logs.

### Market Agent

- Interpret regime, momentum, volatility, breadth, and sector facts.
- Keep unavailable breadth/sector data separate from evidence-backed neutral.
- Use confidence bands and cap confidence when evidence is sparse or stale.

### Technical Agent

- Interpret the Python setup label, structure, momentum, and invalidation facts.
- Explain cited levels without returning or changing their values.
- Flag missing evidence instead of inventing a setup.

### Flow Agent

- Interpret OBV, MFI, CMF, relative volume, and other available flow facts.
- Return `UNKNOWN` for unavailable directional evidence and never treat a header-only file as a data source.
- Prohibit certainty about actual transactions by Indonesian market traders colloquially called `bandar`, foreign investors, or brokers.

### Exit criteria

- Each agent has isolated schema and fixture tests.
- A malicious response that changes a ticker or numerical fact cannot affect the final result.
- Missing optional data cannot produce fabricated evidence or silent neutral scores.
- Three independent assessments can be produced from one snapshot.
- One agent failure is isolated and produces a safe partial/failed run state.

---

## Phase 4 — Decision, Conflict Handling, and Hermes Integration

### Objective

Create one traceable orchestration path in which Hermes interprets evidence and Python enforces the final policy.

### Work

1. Use `AnalysisService` for run lifecycle, snapshot preparation, Hermes invocation, validation, persistence, and finalization.
2. Implement deterministic conflict detection for market, technical, flow, catalyst, risk, and evidence quality; distinguish hard invalidation, material conflict, and minor caution.
3. Run Market once per run and Technical/Flow once per selected candidate, concurrently only where transport limits permit.
4. Give the Decision Agent a fixed evidence packet, Python hard-gate results, and allowed proposed statuses.
5. Require the Decision Agent to preserve Python-owned facts and explain any proposed status change with evidence references.
6. Store `agent_proposed_status`; Python then derives `final_status` and `rank` from immutable facts, validated interpretation, hard gates, and policy.
7. Validate candidate states and run states separately; never use `NO_TRADE` for provider/system failure.
8. Enforce recommendation-only READY, zero-to-three cap, unique contiguous ranks, and no duplicate candidates.
9. Prevent READY when a hard gate, mandatory analysis, required challenge, or snapshot-hash check fails.
10. Make retries idempotent and define a controlled Hermes-outage policy that produces `PARTIAL`/`FAILED`, never a local improvised recommendation.

### Deliverables

- Hermes agent runtime/orchestrator adapter
- Decision Agent plus Python final-decision policy
- Deterministic conflict rules
- End-to-end in-process/shadow pipeline

### Exit criteria

- A fixture run completes and persists without Telegram.
- READY requires mandatory inputs, a valid risk plan/trigger, and no unresolved hard conflict.
- The output has zero to three READY recommendations and satisfies every run invariant in Section 6.10.
- A valid run with no qualified candidates returns `NO_TRADE`.
- Identical facts/configuration and captured validated agent outputs produce identical final status, rank, and render payload.

---

## Phase 5 — Telegram MVP

### Objective

Expose a small, useful command surface without turning Telegram into the system of record.

### Work

1. Add a thin `python-telegram-bot` adapter that calls `AnalysisService`; no Telegram-specific data or scoring path is allowed.
2. Implement `/screen`, `/analyze TICKER`, `/market`, `/status`, and `/why TICKER`; add `/debate TICKER` only after Phase 6.
3. Validate normalized IDX tickers and authorize every sender/chat against configured allowlists.
4. Show queued/running/complete/partial/failed state for long-running commands and prevent overlapping `/screen` runs.
5. Render the canonical stored run result; never parse previous messages or rerun an LLM for `/why`.
6. Split messages safely for Telegram limits and escape user-controlled Markdown/HTML.
7. Add concise market, READY, WAIT/REJECT, risk, freshness, and data-unavailable sections.
8. Include a disclaimer that output is analysis, not execution or guaranteed profit.
9. Redact raw traces, prompts, private data, and secrets from group messages.
10. Deploy the bot as a separate long-lived service backed by the durable store; do not run polling inside the current scheduled GitHub Actions job.
11. Keep operational enablement feature-flagged until the challenge contract in Phase 6 passes.

### Exit criteria

- All MVP commands work in an allowlisted test group from a long-lived service.
- Repeated/concurrent commands use `AnalysisService` and idempotency correctly.
- `NO TRADE`, empty, partial, and failed runs have distinct, clear messages.
- `/why` returns the persisted decision/evidence, not a newly invented explanation.
- Telegram state and rendering do not change canonical results.
- Operational exposure stays disabled until Phase 6 challenge tests pass.

---

## Phase 6 — Challenge/Debate Mechanism

### Objective

Expose and resolve material contradictions without encouraging unsupported agent storytelling.

### Work

1. Trigger a challenge only from a versioned deterministic conflict rule or explicit `/debate TICKER` request.
2. Send each relevant agent a focused question plus conflicting evidence IDs and the allowed claim boundary.
3. Require each response to cite evidence, acknowledge missing data, and use a dedicated challenge-response schema.
4. Validate the response and reject new prices, scores, tickers, or unsupported factual claims.
5. Have the Decision Agent produce a resolution with resolved/unresolved conflicts and an explicit status effect.
6. Persist a complete `ChallengeRecord`: rule version, questions, raw/validated responses, evidence, conflicts, status effect, and final resolution.
7. Treat an unresolved hard conflict as `WAIT` or `REJECT` according to the preconfigured policy, never as an automatic READY.
8. Show the debate in Telegram only when it adds material context.
9. Bound retries, output size, and execution time.

### Exit criteria

- A technical-bullish/flow-distribution fixture triggers a challenge.
- A benign low-confidence difference does not trigger one by default.
- A failed challenge cannot upgrade a candidate to `READY` without the normal gates.
- Telegram can display the concise debate and `/why` can retrieve the full audit trail.

---

## Phase 7 — Backtest Integration and Forward Evaluation

### Objective

Make every recommendation measurable and compare the analyst team against deterministic baselines.

### Work

1. Make accepted signal status/policy configurable in the backtest instead of hardcoding `Ready to Enter`.
2. Persist policy, agent, challenge, prompt, model, and configuration references in each signal record.
3. Extend trades/outcomes with actual fill, TP1/TP2 hit dates, SL hit, actual result, MFE, MAE, holding sessions, gross/cost-adjusted return, and benchmark return.
4. Define entry fill, gap, stop, simultaneous TP/SL, and two-target accounting rules before comparing results.
5. Keep status semantics distinct: READY can create a trade; WAIT records `execution_outcome=NOT_EXECUTED` plus a separately labeled trigger counterfactual; REJECT future return is diagnostic only.
6. Treat unfilled/expired entry-zone recommendations distinctly from rejected or executed trades.
7. Calculate MFE/MAE from actual entry fill over the same evaluation window.
8. Implement PRD signal/risk metrics plus READY/WAIT/REJECT rates, WAIT confirmation, losses avoided by REJECT, confidence-band stability, and overtrading versus the current screener.
9. Isolate agent value with date-matched ablations: no-AI, Technical-only, Technical+Flow, Technical+Flow+Market filter, and full Decision policy using the same eligible pool.
10. Define maximum drawdown on a documented equal-weight evaluation basket; it is an evaluation construct, not production position sizing or portfolio management.
11. Build IHSG, buy-and-hold, seeded random eligible-candidate, current screener, and deterministic `AI_TEAM_DAILY_V1` baselines.
12. Freeze minimum sample count, evaluation horizon, costs, and promotion criterion before selecting a winning variant.
13. Use archived point-in-time news only when publication/retrieval provenance proves availability at decision time; otherwise evaluate catalysts through forward paper tracking.
14. Add a reproducible evaluation command/machine-readable report and document that historical results do not guarantee future performance.

### Exit criteria

- A completed run can be reconstructed from storage without rerunning the LLM.
- READY, WAIT, and REJECT outcomes are computed from documented, testable rules.
- Metrics and ablations can be reproduced from stored recommendations over aligned dates/costs.
- The evaluation basket's drawdown assumptions are explicit and do not introduce production portfolio management.
- Agent-level value is assessed with matched counterfactuals rather than inferred from final P&L alone.
- No AI improvement is claimed until the frozen sample/horizon/promotion criterion is met.

---

## Phase 8 — Scheduling, Operations, and Rollout

### Objective

Operate one reliable post-market daily run and make failures observable/recoverable.

### Work

1. Add a scheduler that calls `AnalysisService` once after a completed IDX session and uses the same idempotency/persistence path as CLI/Telegram.
2. Test market-close gating, IDX holidays, missed-run detection, duplicate triggers, manual catch-up, stale data, and no-trade days.
3. Add stage timeouts, bounded retries/circuit breakers, safe model-call caching, and explicit partial/failed transitions.
4. Emit structured logs/metrics and health checks for market data, Hermes, storage, scheduler, and Telegram; define alert thresholds and ownership.
5. Separate Telegram, Hermes, and legacy Gemini secrets and enforce chat/user allowlists.
6. Validate schema/config/code compatibility before accepting a run.
7. Add manual backfill/replay with explicit `as_of`; never silently reuse current news/universe for historical dates.
8. Write and drill a runbook for stale/partial data, provider outage, malformed output, database recovery, missed run, and rollback to the legacy screener.
9. Keep the existing GitHub Actions screener schedule during rollout; add separate package tests and AI/Telegram deployment automation rather than turning the scheduled job into a bot.

### Deliverables

- Scheduler and deployment workflow/service definition
- Alert thresholds, dashboards/metrics, and on-call ownership
- Secret/allowlist configuration
- Recovery/rollback runbook with a completed drill
- Rollout and shadow-mode promotion checklist

### Dependencies

Phases 2A, 4, 6, and 7. Telegram operational exposure also depends on Phase 5.

### Exit criteria

- A simulated holiday, missed run, duplicate trigger, stale source, Hermes outage, and process restart each produce the documented safe state.
- Alerts fire within the defined threshold and identify the failed stage without leaking secrets.
- A manual replay reconstructs the same stored run/policy state.
- The rollback path returns daily operation to the frozen legacy screener without data loss.

---

## 8. Work Breakdown and Dependency Order

| ID | Work package | Primary output | Prerequisites |
|---|---|---|---|
| WP-00 | Product/policy freeze | Legacy boundary, Hermes runtime, enums/unknown/scoring decisions | PRD approval |
| WP-01 | Package and contract foundation | `src/cacingnaga` contracts/config plus compatibility adapter | WP-00 |
| WP-02 | Unified data snapshot | Timestamped source manifest, `^JKSE`, universe, completed-candle facts | WP-01 |
| WP-03 | Features and candidate pool | Indicators/flow/setup features and full pre-agent pool | WP-02 |
| WP-04 | Risk and final-decision policy | `RiskPlan`, score components, hard gates, deterministic status/rank | WP-01, WP-03 |
| WP-05 | SQLite/idempotency boundary | Durable run/audit store and lifecycle transitions | WP-01, WP-02 |
| WP-06 | Deterministic offline backtest | New-policy outcomes, MFE/MAE, no-READY proof | WP-04, WP-05 |
| WP-07 | Agent transport infrastructure | Fake/Hermes adapters, prompts, parsers, validators | WP-01, WP-05 |
| WP-08 | Individual analyst agents | Market, Technical, and Flow interpretations | WP-03, WP-07 |
| WP-09 | Conflict and Decision pipeline | Deterministic conflicts, proposed/final decisions | WP-04, WP-08 |
| WP-10 | Challenge round | Audited `ChallengeRecord` and safe resolution | WP-09 |
| WP-11 | End-to-end shadow run | Persisted replayable run with zero-to-three output | WP-06, WP-09, WP-10 |
| WP-12 | Evaluation and ablations | Agent metrics, baselines, promotion report | WP-06, WP-11 |
| WP-13 | Telegram MVP | Authorized command/render adapter | WP-05, WP-10, WP-11 |
| WP-14 | Scheduler and production operations | Automatic daily run, monitoring, recovery | WP-12, WP-13 |

This is a topological order: durable persistence precedes model execution, and challenge support precedes operational Telegram exposure.

### Recommended pull-request slices

1. **Contracts and fixtures:** package skeleton, schemas, enums, config, examples, validation tests.
2. **Legacy adapter and data context:** compatibility imports, normalized symbols/timestamps/freshness, source manifest.
3. **Deterministic analytics:** missing indicators, flow features, setup classifier, full candidate pool, risk plans, policy.
4. **Persistence and offline backtest:** SQLite repository, idempotency, exporters, new statuses/outcomes.
5. **Agent infrastructure:** fake/Hermes transport, prompts, parsers, evidence validators, audit metadata.
6. **Individual analysts:** Market, Technical, and Flow agents.
7. **Decision and challenge:** conflict rules, Decision Agent, Python final policy, challenge records.
8. **Shadow pipeline:** complete offline run, replay, numeric-immutability and no-lookahead tests.
9. **Telegram MVP:** commands, authorization, status, canonical rendering.
10. **Evaluation and operations:** ablations/baselines, scheduler, monitoring, security, runbook.

Each slice should be independently testable and avoid bundling unrelated refactors.

### Codebase change map

Create the new pipeline under an importable package and keep legacy scripts as compatibility surfaces:

```text
pyproject.toml
config/ai_team.toml
src/cacingnaga/
├── __init__.py
├── contracts.py
├── config.py
├── legacy_adapter.py
├── service.py
├── reporting.py
├── data/
│   ├── market.py
│   ├── indicators.py
│   ├── flow.py
│   └── catalysts.py
├── screening/
│   ├── candidates.py
│   ├── risk.py
│   └── policy.py
├── agents/
│   ├── orchestrator.py
│   ├── hermes_adapter.py
│   └── prompts/
│       ├── market.md
│       ├── technical.md
│       ├── flow.md
│       └── decision.md
├── storage/
│   └── sqlite.py
└── telegram/
    └── bot.py
scripts/ai_team.py
```

Planned tests:

```text
tests/test_ai_contracts.py
tests/test_flow_indicators.py
tests/test_agent_decision_policy.py
tests/test_hermes_adapter.py
tests/test_storage.py
tests/test_telegram_commands.py
tests/test_ai_backtest.py
```

Change existing files conservatively:

- `scripts/financial_screener.py`: preserve legacy constants/behavior; later add an explicit legacy/AI-team dispatcher only after compatibility tests pass.
- `scripts/screener_ranking.py`: expose a public full-candidate stage and keep `_screen_timeframe` behavior unchanged.
- `scripts/screener_core.py`: wrap data/indicator functions first; move them behind regular package imports only as consumers migrate.
- `tests/backtest_data.py` and `tests/backtest_engine.py`: add policy/status adapters, attribution, two-target accounting, WAIT counterfactuals, and MFE/MAE while preserving executable compatibility.
- `tests/test_final_screener.py`: keep as the frozen legacy contract; put new-policy tests in separate files.
- `requirements.txt`: add/lock only confirmed dependencies; use Pydantic v2, `python-telegram-bot`, and the exact Hermes integration. SQLite is standard library.
- `.github/workflows/financial_screener.yml`: keep the legacy schedule initially; add package tests and separate AI/Telegram deployment automation later.
- `README.md` and `SCREENER_FINAL.md`: document the legacy screener and new versioned pipeline separately.

Do not add Redis, PostgreSQL, a queue framework, vector memory, or a guessed `hermes` package to the MVP.

---

## 9. Testing Strategy

### 9.1 Unit tests

- Indicator, Bollinger, OBV/MFI/CMF, and flow formulas/edge cases
- Symbol normalization, price basis, timezone, and IDX calendar conversion
- Data freshness/quality and empty-resource handling
- Every named setup classifier
- Long-only trigger, risk-plan, tick-rounding, and percentage boundaries
- Score components, unknown-component policy, weights, and tie-breaking
- Conflict matrix, challenge transitions, and unresolved-conflict effects
- Candidate/run status invariants and idempotency

### 9.2 Contract tests

- Valid/invalid examples for every schema
- Enum/confidence-band and finite-number enforcement
- Required evidence, `UNKNOWN` versus `NEUTRAL`, and unknown-component scoring
- Forbidden agent fields (price, entry, stop, target, score, rank) and ticker mutation
- Evidence references resolve to facts present in the immutable snapshot
- Backward compatibility for persisted schema versions
- Run-result invariants in Section 6.10

### 9.3 Integration tests

- Legacy scripts → compatibility adapter → importable package
- Collector → indicators/flow/setup → full candidate pool → immutable snapshot
- Snapshot → SQLite → Hermes/fake agents → validators
- Conflict → challenge → Decision proposal → Python final policy
- Canonical result → storage replay → Telegram/scheduler render
- Duplicate trigger, process restart, malformed output, stale data, partial news, and provider outage

### 9.4 End-to-end tests

Use recorded/replayed fixtures by default. Keep small separately gated live tests for:

- `/screen`, `/analyze`, `/market`, `/status`, `/why`, and `/debate`
- Scheduler post-market trigger, holiday skip, missed-run catch-up, and duplicate suppression
- Durable-store restart and rollback to the legacy workflow

### 9.5 Agent evaluation suite

Maintain fixture-based checks for:

- Unsupported claims and instructions embedded in news
- Numeric/ticker/rank mutation attempts
- Hallucinated news, foreign flow, broker flow, or `bandar` activity
- Overstated confidence with missing evidence
- Correct strict-enum and `UNKNOWN` behavior
- Correct conflict recognition and challenge responses
- Correct proposed/final WAIT/REJECT behavior
- Respect for Python-owned setup, score, and risk levels

Track prompt/runtime versions and compare changes over time. LLM wording itself should not be the only pass/fail oracle.

### 9.6 Backtest/evaluation tests

- No look-ahead data or current-universe survivorship leakage
- Point-in-time news availability and safe forward tracking when archive provenance is absent
- Deterministic entry, two-target exit, gap, stop, and same-bar rules
- Corporate-action/split-aware price handling and explicit price basis
- Missing/unfilled READY entries, WAIT counterfactuals, and diagnostic REJECT outcomes
- Stable date-matched baseline/ablation samples and equal-weight evaluation-basket drawdown
- Frozen sample/horizon/promotion criterion

---

## 10. Configuration Plan

Keep strategy and integration settings outside prompts. At minimum, configure:

- Analysis dates/lookbacks and timezone
- Universe version and source
- Data freshness thresholds
- Liquidity, price, volume, momentum, and volatility screens
- Maximum candidate pool size
- Recognized setup thresholds
- TP, SL, and maximum holding-period boundaries
- Minimum reward/risk and hard eligibility gates
- Ranking weights
- Market-regime and conflict thresholds
- Agent/runtime model, timeouts, retries, and prompt versions
- Optional news source and freshness limits
- Persistence location and retention
- Telegram token/allowed chats (secrets stored outside source control)
- Scheduler time and holiday behavior

Configuration should be validated at startup, versioned, and included in every run's audit metadata.

---

## 11. Reliability, Security, and Audit Requirements

### Reliability

- Every stage reports `success`, `warning`, or `failure` with a reason.
- No stale data is silently labeled current.
- A missing mandatory stage prevents `READY`.
- A run has explicit lifecycle states and an idempotency key.
- Retries cannot duplicate persisted recommendations.
- Empty candidate pools are valid outcomes.

### Security and privacy

- Keep Telegram and model credentials in environment/secret storage, never prompts or source control.
- Authorize command senders/chats.
- Treat news and external text as untrusted data; quote it as evidence and never execute instructions found in it.
- Escape Telegram Markdown/HTML and limit output length.
- Minimize retained raw data and redact secrets from logs.
- Do not expose full prompts, internal traces, or private group content publicly.

### Auditability

Persist:

- Input snapshot hash and source/freshness metadata
- Config and prompt versions
- Runtime/model identifiers
- Raw and validated agent outputs
- Conflict/challenge records
- Deterministic calculations and hard-gate results
- Final decision and render-safe result
- Outcome/evaluation updates

---

## 12. MVP Acceptance Checklist

### Functional

- [ ] The scheduler runs `/screen` logic automatically once after a completed IDX session and records holiday/missed/duplicate outcomes.
- [ ] Market, Technical, Flow, and Decision outputs validate against versioned interpretation schemas.
- [ ] `/analyze TICKER` works independently of the daily screener.
- [ ] `/market`, `/status`, and `/why TICKER` return persisted canonical data.
- [ ] Significant conflicts trigger an auditable challenge before operational Telegram exposure.
- [ ] Telegram sends the final report, including `NO TRADE` when applicable.
- [ ] A run has zero to three READY recommendations; every evaluated candidate appears exactly once across decision buckets.
- [ ] A run never invents data, news, foreign/broker flow, `bandar` activity, or missing evidence.

### Analytical integrity

- [ ] All indicator, setup, score, level, risk, status, and rank values originate in Python or the original source.
- [ ] Agent responses are schema-validated, evidence-checked, and prevented from returning forbidden numerical fields.
- [ ] The immutable fact hash is unchanged from pre-agent snapshot through final result.
- [ ] Historical/replay runs are protected from look-ahead and point-in-time universe/news leakage.
- [ ] READY/WAIT/REJECT outcomes, MFE, MAE, and holding period are reproducible.
- [ ] Matched ablations, agent metrics, evaluation-basket drawdown, and all four required baselines can be generated.

### Operational

- [ ] A complete run can be reconstructed from SQLite after process restart without rerunning the LLM.
- [ ] Duplicate/missed scheduler runs, timeouts, malformed agent output, stale market data, and provider outages have tested behavior.
- [ ] Logs/metrics identify run/stage and alert on defined thresholds without exposing secrets.
- [ ] Configuration/schema compatibility, Telegram authorization, and durable backup/restore are enforced.
- [ ] An operator runbook covers failures, manual replay, and rollback to the legacy screener, and its recovery drill passes.

---

## 13. Key Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Relabeling the current D+10 watchlist as READY | Unsupported execution guidance | Separate/backtest `AI_TEAM_DAILY_V1`; require trigger, entry, SL, targets, and hard gates |
| Breaking dynamic imports/backtest API | CLI and historical baseline regression | Compatibility adapter and frozen legacy tests before code movement |
| Confusing system failure with `NO_TRADE` | Incorrect operational signal | Separate `run_status` from `decision_outcome`; partial/failed runs never recommend |
| Unknown catalyst/flow silently scored as neutral | Artificial ranking confidence | Explicit availability, conservative configured points, and recorded unknown-component policy |
| yfinance instability or delayed IDX data | Wrong/freshness ambiguity | Source metadata, explicit freshness gates, fixture/replay tests, failure instead of stale `READY` |
| IDX ticker/corporate-action inconsistencies | Wrong data or returns | Central symbol mapper, versioned universe, split/dividend-aware tests |
| Look-ahead leakage | Inflated backtest | As-of snapshots, historical data freeze tests, aligned baseline dates |
| LLM variability or prompt drift | Inconsistent decisions | Strict schemas, evidence references, prompt versioning, fixture evaluations |
| Agent invents flow/news | Trust/safety failure | Missing-data fields, source provenance, validation rules, prohibited-claim tests |
| Free news coverage is limited | Weak catalyst layer | Treat as optional and `UNKNOWN`; never block core MVP |
| Telegram message limits and concurrent users | Truncated/confusing output | Canonical renderer, chunking, idempotency, request throttling |
| Hermes/provider outage | Unavailable daily run | Timeouts, circuit breaker, explicit failed/no-trade policy, no local AI improvisation |
| Too many candidates | Cost, latency, noise | Configurable deterministic cap and token-budget estimate |
| Ranking overfits | Poor generalization | Versioned weights, baselines, walk-forward/out-of-sample evaluation |
| Header-only flow/sector resources treated as valid | Fabricated or false evidence | Source-availability validation; default `UNKNOWN` and zero/unknown component policy |
| Adjusted-price basis differs across live/backtest | Invalid levels and returns | Store price basis and use one documented basis end to end |
| Agent output conflicts | Arbitrary final call | Deterministic conflict matrix, evidence-based challenge, preserved hard gates |
| Evaluation overfits or uses incomplete samples | False improvement claim | Frozen walk-forward/holdout sample, matched ablations, promotion criterion |

---

## 14. Explicitly Deferred

Do not add these to the MVP unless the PRD changes:

- Broker order execution or autonomous trading loops
- Automatic position sizing or production portfolio management
- Guaranteed-return claims or certain price prediction
- Replacing backtesting with agent judgment
- Direct claims about transactions by Indonesian market traders colloquially called `bandar`
- Paid data feeds or broker APIs
- AI calculation of raw OHLC indicators
- Dozens of specialist agents or a complex long-term memory system
- Intraday trading support

---

## 15. Definition of Done

The implementation is complete when the frozen legacy screener still passes unchanged; a scheduled post-market `AI_TEAM_DAILY_V1` run can be reproduced from stored inputs/configuration without rerunning the LLM; all four agents produce validated interpretations; material conflicts are challenged; Python produces zero to three READY candidates under the documented hard gates; every numerical fact is traceable; `/screen`, `/analyze`, `/market`, `/status`, `/why`, and `/debate` work through the same service; and outcomes, ablations, baselines, MFE/MAE, and drawdown can be regenerated from stored records. Completion also requires tested failure recovery, durable storage, security controls, rollback, the MVP acceptance checklist, and a documented handoff—not merely a successful demonstration run.
