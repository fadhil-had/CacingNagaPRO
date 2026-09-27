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
| 16 | Hermes runtime | **Not yet confirmed** — no package guessed. Phase 3 implements a provider-neutral `AgentTransport`; these phases are Hermes-free by design. | deferred (Phase 3 / WP-07) |
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
