# AI_TEAM_DAILY_V1 — Execution Policy Specification (Phase 2)

Status: **Frozen for Phase 2B backtest adapters** · Date: 2026-09-25
Scope: how a `READY` decision converts into a simulated/observed trade.
Nothing here performs execution; this document fixes the assumptions so the
offline backtest (Phase 2B) and later forward evaluation measure the *same*
policy. The legacy backtest engine stays untouched and remains the benchmark.

## 1. What creates a position

| Item | Rule |
|---|---|
| Signal source | `recommendations[]` of a finalized `RunResult` (final status `READY` only). `WAIT`/`REJECT` never open positions. |
| Signal date | `as_of` of the snapshot (last completed IDX candle). |
| Entry attempt | Next IDX session (T+1) only, inside the `entry_low`–`entry_high` zone. |
| Entry fill | Limit at the first session price ≤ `entry_high` (long-only); if the session opens above `entry_high`, the order is **not filled** (no chase). |
| Entry expiry | Unfilled after `entry_window = 3` sessions → `NOT_EXECUTED` (unfilled entry), distinct from REJECT and from an executed trade. |
| Stop / targets | Working from the fill date onward: `stop_loss`, `tp1`, `tp2` from the candidate's `RiskLevels` (never recomputed). |
| Time stop | `maximum_holding_days` (10) sessions from fill → exit at close. |

## 2. Intrabar ambiguity — always pessimistic

When one daily bar could satisfy more than one level (gap or wide range):

1. **Stop before target** (same bar): assume `stop_loss` hit first.
2. **Gap through stop** (open < stop): fill at the open, not at `stop_loss`.
3. **Gap through TP1/TP2** (open > target): fill at the open.
4. **Simultaneous TP1 and TP2**: assume TP1 only (partial at TP1, remainder rides
   unless TP2/stop/time-stop resolves it later).

## 3. Two-target accounting (PRD §12)

- 50% of the position exits at `tp1`, 50% at `tp2` (or stop / time stop).
- `result` = volume-weighted return net of fees + slippage (reuse legacy
  fee/slippage parameters for comparability).
- `risk_reward_tp1` / `risk_reward_tp2` from the stored `RiskLevels` are the
  *planned* ratios; realized MFE/MAE are measured from the actual fill price
  over the holding window (per plan §7 Phase 7 work item 7).

## 4. Outcome taxonomy (distinct, testable states)

| State | Meaning |
|---|---|
| `EXECUTED_TP2` / `EXECUTED_TP1_THEN_STOP` / `EXECUTED_TP1_THEN_TIME` … | Filled, two-target path resolved |
| `EXECUTED_STOP` | Filled, stop hit (first touch per §2.1) |
| `EXECUTED_TIME_STOP` | Filled, exited at close on day 10 |
| `NOT_EXECUTED` | Entry never reached the zone within 3 sessions |
| `NOT_TAKEN` | `WAIT` candidate whose counterfactual trigger is tracked separately (diagnostic only, never P&L) |
| `DIAGNOSTIC_REJECT` | `REJECT` future return tracked for "losses avoided" metrics only |

`WAIT` counterfactuals and `REJECT` diagnostics never enter win-rate or
return metrics; they exist to answer "was the Technical Agent right?" later.

## 5. Invariants the backtest must hold

- Only `READY` decisions create positions (mirrors `RunResult` invariants).
- All levels come from the persisted `RiskLevels`; the backtest never
  recalculates entry/stop/targets.
- Same stored run + same execution policy ⇒ identical trade list (replayable).
- A `PARTIAL`/`FAILED` run produces zero trades.
- IDX tick rounding and `auto_adjusted` price basis are assumed on both sides.
