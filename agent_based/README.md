# agent_based/ — CacingNagaPRO AI Analyst Team (Phase 0 → 2B)

Implementation of `Backlog/improvement_v3.md` (PRD) and
`Backlog/improvement_v3_implementation_plan.md`: **Phase 0 — Specification and
Safety Harness**, **Phase 1 — Data, Indicators, and Screener Foundation**,
**Phase 2 — Deterministic Risk and Ranking Layer**, **Phase 2A — Durable
Snapshot and Audit Store**, and **Phase 2B — Offline Policy Backtest**.

Principle: **Python calculates. Agents interpret. Decision Agent challenges.
Backtest judges.** No LLM runs anywhere in these two phases.

## Layout

```
agent_based/
├── orchestrator.py        # (empty placeholder — Hermes orchestration lands in Phase 4)
├── agents/                # (empty placeholders — analyst agents land in Phase 3)
└── cacingnaga/            # the Phase 0/1/2 package
    ├── __init__.py        # public API re-exports
    ├── versioning.py      # SCHEMA_VERSION, PIPELINE_VERSION (AI_TEAM_DAILY_V1)
    ├── errors.py          # CacingNagaError / ContractViolation / SnapshotError
    ├── canonical.py       # canonical JSON + SHA-256 (finite numbers only)
    ├── config.py          # AIAnalystConfig + PolicyConfig (weights/gates/risk mandates)
    ├── contracts.py       # facts / interpretations / decisions + run invariants
    ├── legacy_adapter.py  # loads frozen scripts/screener_ranking.py by path
    ├── snapshot.py        # Phase 1 snapshot: manifest, flow indicators, setups, pool
    ├── risk.py            # Phase 2 RiskPlanCalculator (long-only, IDX ticks, mandates)
    ├── policy.py          # Phase 2 DecisionPolicy (components, gates, rank, finalize_run)
    ├── store.py           # Phase 2A SQLite audit store (idempotent runs, state machine)
    ├── exporters.py       # Phase 2A JSON/CSV/Markdown run exporters
    ├── backtest.py        # Phase 2B offline policy backtest (EXECUTION_POLICY.md rules)
    ├── fixtures.py        # deterministic offline OHLCV fixtures (3 scenarios)
    ├── POLICY_DECISIONS.md# Phase 0 decision record (Section 4 items) + Phase 2 additions
    ├── EXECUTION_POLICY.md# Phase 2 execution assumptions (frozen for Phase 2B backtest)
    └── tests/
        ├── test_contracts.py  # 25 tests (contracts, fixtures, snapshot)
        ├── test_policy.py     # 37 tests (risk plans, gates, triggers, ranking, invariants)
        ├── test_store.py      # 12 tests (idempotency, restart, FSM, backup, exporters)
        └── test_backtest.py   # 17 tests (outcome matrix, costs, replay determinism)
```

## What Phase 0 delivered

- **Three contract layers** in `contracts.py`: immutable facts
  (`MarketFacts`, `TechnicalFacts`, `FlowFacts`, `CatalystFacts`, `RiskLevels`,
  `CandidateProvenance`), constrained agent interpretations
  (`MarketInterpretation`, `TechnicalInterpretation`, `FlowInterpretation`,
  `CatalystInterpretation`), and Python-owned decisions
  (`CandidateDecision`, `RunResult`).
- **Executable invariants**: ≤3 READY recommendations with contiguous ranks,
  `COMPLETE`+0 READY ⇒ `NO_TRADE`, `PARTIAL`/`FAILED` ⇒ `NOT_EVALUATED`,
  duplicates rejected, WAIT/REJECT ranks `null`.
- **Agent boundary enforcement**: `FORBIDDEN_AGENT_FIELDS` (price, entry, stop,
  targets, scores, rank…) and strict `from_payload()` deserialization that
  rejects injected/unknown keys — a hostile model response cannot mutate facts.
- **Canonical JSON + hash** (`canonical.py`): NaN/inf rejected; every snapshot
  is verifiable via `assert_snapshot_integrity`.
- **Decision record**: all 20 Section 4 items resolved or explicitly deferred —
  see `cacingnaga/POLICY_DECISIONS.md`.

## What Phase 1 delivered

- **Versioned `AnalysisSnapshot`** (`snapshot.py`): IHSG + candidate pool in one
  hash-pinned object (`data_snapshot_hash`), with `schema_version` 1.0.0 and
  `pipeline_version` `AI_TEAM_DAILY_V1`.
- **Source manifest** with rows, first/last completed date, price basis, and
  quality warnings per frame (retrieval time kept for audit, excluded from the
  data hash so replays are byte-identical).
- **Missing PRD indicators** (`add_flow_indicators`): Bollinger
  (mid/upper/lower/width/%b), OBV + 20-bar slope, MFI-14, CMF-20,
  A/D line, up/down volume ratio — all causal, with documented degenerate-window
  conventions (e.g. zero-loss MFI window ⇒ 100).
- **Named setup classifier** (`classify_setup`): BREAKOUT,
  FAILED_BREAKOUT, BREAKDOWN, PULLBACK, REVERSAL, TREND_CONTINUATION, RANGE,
  UNKNOWN — deterministic labels the agents may explain but never rename.
- **Full pre-agent pool** (no legacy top-3 limit): deterministic eligibility
  (price, turnover, volatility, history), stable ordering, explicit
  eligible/skipped/failed counts, then a configurable cap
  (`max_agent_pool_size` ≥ 4, default 30).
- **No-lookahead guarantees**: legacy completed-candle cutoff (16:10 WIB
  buffer) applied everywhere; backward-looking windows only; per-ticker
  failures become warnings, never silent substitutions.
- **Compact agent envelope** (`snapshot_to_agent_envelope`) with stable
  evidence ids (`TechnicalFacts:<hash12>:<field>`) — the audit payload stays
  separate (Phase 0 work item 4).

## Usage

```python
from cacingnaga import AIAnalystConfig
from cacingnaga.snapshot import build_snapshot, assert_snapshot_integrity
from cacingnaga.fixtures import market_frame, default_frames
from cacingnaga.policy import evaluate_snapshot

config = AIAnalystConfig()
snapshot = build_snapshot(market_frame(), default_frames(), config)
assert_snapshot_integrity(snapshot)   # re-verifies data_snapshot_hash

run, outcomes = evaluate_snapshot(snapshot, config)   # Phase 2 decision policy
run.validate()                                        # Section 6.10 invariants
print(run.decision_outcome)                           # TOP_3 or NO_TRADE
for d in run.recommendations:
    print(d.rank, d.ticker, d.final_status, d.score, d.risk_plan_ref[:40])
```

Live data comes later via the legacy `download_ihsg`/`download_saham_batch`
functions (already wrapped by the adapter); the snapshot API is network-free by
design so CI and backtests stay offline.

## Tests

```bash
python3 -m pytest agent_based/cacingnaga/tests/ -q   # 91 tests (Phase 0 → 2B)
python3 -m pytest tests/ -q                          # 12 legacy tests (frozen baseline)
```

Both suites are green on Python 3.12+ (verified on 3.14 / pandas 3.0.5).

## What Phase 2 delivered

- **`RiskPlanCalculator`** (`risk.py`): deterministic long-only plans from
  structure + ATR — entry zone (support→price, ceiling-capped), structural
  stop with ATR floor and the 2–5% mandate clamp, TP1/TP2 as R-multiples
  capped at the 3–10% target mandate, IDX tick rounding on every level,
  `risk_per_share` + realized `risk_reward_tp1/tp2`, and a documented
  invalidation rule. Invalid inputs fail closed (`None` + reasons), and only a
  valid plan can back a `READY` (`risk_plan_ref` = version + fact hash).
- **`DecisionPolicy`** (`policy.py`): six normalized components (technical,
  flow, market_fit, risk_reward, catalyst, liquidity) with documented ranges;
  PRD §14 weights via `ScoreWeights` (config-only, must sum to 1.0);
  deterministic hard gates (plan validity, technical/composite floors,
  relative volume, ADX, RSI band, uptrend structure, RR floor); status
  derivation (READY / WAIT for recoverable structures and near-misses /
  REJECT otherwise); deterministic rank (score desc → component tie-breaks →
  ticker asc); `finalize_run` assembling invariant-checked `RunResult`s with
  `config_hash` and `data_snapshot_hash` for replay.
- **Unknown-component handling**: unavailable flow/market facts score 0.0 with
  an explicit audit note (or `reject` mode disqualifies READY); catalyst is
  0.0 in v1; weights are never renormalized silently.
- **`EXECUTION_POLICY.md`**: frozen entry/gap/simultaneous-TP/SL assumptions
  and the outcome taxonomy for the Phase 2B backtest adapters.

## What Phase 2A delivered

- **`store.py` (`AuditStore`)**: SQLite system of record with a versioned
  schema (`PRAGMA user_version` migrations, WAL, foreign keys). Runs are
  **idempotent** — the idempotency key hashes pipeline version, analysis
  date, `data_snapshot_hash`, and `config_hash`, so a repeated trigger returns
  the original run instead of duplicating rows.
- **Persist-before-agents**: the snapshot payload, source manifest, and
  candidate rows are stored with the `RUNNING` run; `create_run` refuses any
  payload that does not hash to `snapshot.data_snapshot_hash`, and
  `verify_snapshot_hash` recomputes it from the stored JSON — the Phase 3
  boundary ("recompute the immutable snapshot hash before finalization") is
  now checkable against the database.
- **Run state machine**: `RUNNING → COMPLETE | PARTIAL | FAILED` enforced in
  code and schema; terminal states are immutable.
- **Audit trail tables**: `decisions` (upsert per run+ticker with exact ranks),
  `agent_outputs` (raw + validated responses, ready for Phase 3),
  `challenges` (Phase 6), `source_manifest`, and `candidates` with
  preliminary ranks.
- **Backup/restore**: consistent online `backup()` and schema-verified
  `restore()` (exit criterion: a run survives process restart — tested).
- **`exporters.py`**: canonical run → JSON (audit), CSV (decision table), and
  Markdown (report); rendering never mutates decisions or numbers.

## What Phase 2B delivered

- **`backtest.py`**: walk-forward replay of the full pipeline —
  point-in-time snapshots (`as_of` cutoff, so a replayed historical date sees
  exactly what a live run would have seen; equivalence tested against
  truncated frames), deterministic policy, and simulated outcomes under the
  frozen `EXECUTION_POLICY.md` assumptions.
- **Execution rules with tests**: next-session zone entry (no chase),
  unfilled-entry expiry, gap-through-stop fills at the open, stop-before-
  target pessimism, §2.4 same-bar TP1+TP2 → TP1-only (a full TP2 requires an
  *earlier* TP1 bar), two-target 50/50 accounting, 10-session time stop,
  MFE/MAE from the actual fill, costs matching the legacy benchmark engine
  (0.2% fee + 0.1% slippage per side).
- **Counterfactual semantics**: WAIT = `NOT_TAKEN` (trigger counterfactual),
  REJECT = `DIAGNOSTIC_REJECT` — both excluded from executed-P&L metrics.
- **Trigger hard gate** (also Phase 2 completion): READY now requires a
  volume-confirmed breakout or EMA20 reclaim, closing the
  "watchlist relabeled READY" loophole the plan warns about.
- **`BASELINE_REPORT.md`**: deterministic replay report over the synthetic
  fixtures (mechanics validation only — explicitly not a market-performance
  claim, and no AI value is claimed anywhere).

## Next phases (not started)

- **Phase 3**: `AgentTransport` + Market/Technical/Flow agents (fake transport
  for tests; Hermes adapter only after the runtime is confirmed).
- **Phase 4+**: Decision Agent, conflicts/challenge, Telegram MVP.
