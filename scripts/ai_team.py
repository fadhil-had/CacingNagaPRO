"""CacingNagaPRO AI Analyst Team — deployment entrypoint (plan §8 code map).

Production wiring for the ``AI_TEAM_DAILY_V1`` pipeline. Every subcommand
funnels through the same ``AnalysisService`` idempotency/persistence path:

- ``run``      — the scheduled post-market job: evaluate all scheduler gates
                 (trading day, market close, duplicates, missed runs, stale
                 data), then execute one full analysis when due.
- ``screen``   — one manual shadow run, bypassing the scheduler gates but
                 not the pipeline's own contract checks.
- ``health``   — structured, secret-free health report for market data,
                 Hermes, storage, scheduler, and Telegram.
- ``telegram`` — the long-lived Telegram service (feature-flag gated).

Shadow mode: the Hermes runtime is deliberately unconfirmed (decision #16),
so the entrypoint runs on ``CacingNagaSmokeTransport`` — a deterministic
transport whose every record is tagged ``CACINGNAGA_SMOKE_V1`` in the audit
trail. A real transport later drops in without touching this file.

Usage (repository root)::

    python3 scripts/ai_team.py run
    python3 scripts/ai_team.py screen
    python3 scripts/ai_team.py health
    python3 scripts/ai_team.py telegram
    python3 scripts/ai_team.py run --config config/ai_team.toml

Exit codes: 0 = ran / replayed / safe skip; 1 = configuration or contract
failure; 2 = run degraded (PARTIAL/FAILED) — operators page per RUNBOOK.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "agent_based"))

from cacingnaga.errors import CacingNagaError  # noqa: E402
from cacingnaga.store import AuditStore  # noqa: E402

from deployment import (  # noqa: E402
    CacingNagaSmokeTransport,
    ai_analyst_config,
    load_deployment_config,
    load_live_snapshot,
)
from ops import CachedBreakerTransport, CircuitBreakerPolicy, render_health_report, run_health_checks  # noqa: E402
from orchestrator import AnalysisService  # noqa: E402
from scheduler import Scheduler  # noqa: E402


def _build_service(store: AuditStore, config, deployment):
    """One service per run: smoke transport wrapped by the ops bounds."""
    inner = CacingNagaSmokeTransport()
    breaker_policy = CircuitBreakerPolicy(
        failure_threshold=deployment.failure_threshold,
        cooldown_seconds=deployment.cooldown_seconds,
        half_open_probes=deployment.half_open_probes,
    )
    transport = CachedBreakerTransport(
        inner, breaker_policy=breaker_policy
    )
    return AnalysisService(config, store, transport)


def _open_store(deployment) -> AuditStore:
    path = Path(deployment.store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return AuditStore(path)


def cmd_run(deployment, config, *, now=None) -> int:
    """Scheduled post-market job (gates decide; the pipeline executes)."""
    store = _open_store(deployment)
    scheduler = Scheduler(
        config,
        store,
        lambda analysis_date, snapshot: _build_service(store, config, deployment),
        scheduler_config=deployment.scheduler,
        clock=(lambda _n=None: now) if now is not None else None,
    )

    decision = scheduler.should_run()
    print(f"[ai_team] gate decision: {json.dumps(decision.payload(), sort_keys=True)}")
    if not decision.ran:
        # Safe states (holiday, deferred, duplicate, missed-run report,
        # stale data) are documented outcomes — never failures.
        return 0

    print("[ai_team] building point-in-time snapshot through the legacy loaders…")
    snapshot = load_live_snapshot(deployment, config)
    outcome = scheduler.run_once(snapshot)
    result = outcome.service_result
    if result is None:
        print("[ai_team] run skipped at a later gate "
              f"({outcome.decision.action})")
        return 0

    run = result.run_result
    print(
        f"[ai_team] run {outcome.run_id}: {run.run_status} / {run.decision_outcome}"
        f"{' (replayed)' if outcome.replayed else ''}"
    )
    for rec in run.recommendations:
        print(
            f"  {rec.rank}. {rec.ticker} {rec.final_status} "
            f"score={rec.score:.3f}"
            + (f" challenge={rec.challenge_ref}" if rec.challenge_ref else "")
        )
    if run.run_status in ("PARTIAL", "FAILED"):
        print("[ai_team] degraded run — see RUNBOOK.md §2 (owner: on-call)")
        return 2
    return 0


def cmd_screen(deployment, config) -> int:
    """One manual shadow run (no scheduler gates; pipeline gates still apply)."""
    store = _open_store(deployment)
    snapshot = load_live_snapshot(deployment, config)
    service = _build_service(store, config, deployment)
    result = service.run_full_analysis(
        snapshot,
        run_id=f"manual-{snapshot.analysis_date}",
        trigger="manual",
    )
    run = result.run_result
    print(
        f"[ai_team] manual run {run.run_id}: {run.run_status} / {run.decision_outcome}"
    )
    for rec in run.recommendations:
        print(f"  {rec.rank}. {rec.ticker} {rec.final_status} score={rec.score:.3f}")
    return 0 if run.run_status == "COMPLETE" else 2


def cmd_health(deployment, config, *, now=None) -> int:
    """Secret-free health report across the five operational surfaces."""
    store = _open_store(deployment)
    scheduler = Scheduler(
        config,
        store,
        lambda _d, _s: None,
        scheduler_config=deployment.scheduler,
        clock=(lambda _n=None: now) if now is not None else None,
    )
    decision = scheduler.should_run()
    health = run_health_checks(
        store=store,
        scheduler_decision=decision.payload(),
    )
    print(render_health_report(health))
    return 1 if health.overall == "FAIL" else 0


def cmd_telegram(deployment, config) -> int:
    """Start the long-lived Telegram service (Phase 5 gate enforced there)."""
    from telegram_layer.adapter import run_service
    from telegram_layer.config import TelegramConfig

    tg = TelegramConfig(
        enabled=deployment.telegram_enabled,
        bot_token_env=deployment.telegram_bot_token_env,
        allowed_user_ids=deployment.telegram_allowed_user_ids,
        allowed_chat_ids=deployment.telegram_allowed_chat_ids,
        store_path=deployment.store_path,
    )
    run_service(tg)
    return 0


COMMANDS = {
    "run": cmd_run,
    "screen": cmd_screen,
    "health": cmd_health,
    "telegram": cmd_telegram,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CacingNagaPRO AI Analyst Team deployment entrypoint"
    )
    parser.add_argument(
        "command", choices=sorted(COMMANDS),
        help="run: scheduled post-market job; screen: manual shadow run; "
             "health: ops report; telegram: long-lived bot service",
    )
    parser.add_argument(
        "--config", default="config/ai_team.toml",
        help="deployment configuration file (default: config/ai_team.toml)",
    )
    parser.add_argument(
        "--now", default=None,
        help="override the wall clock (ISO datetime, WIB) — drills/testing only",
    )
    args = parser.parse_args(argv)

    try:
        deployment = load_deployment_config(args.config)
        config = ai_analyst_config(deployment)
    except CacingNagaError as exc:
        print(f"[ai_team] configuration rejected: {exc}", file=sys.stderr)
        return 1

    now = None
    if args.now:
        from datetime import datetime

        now = datetime.fromisoformat(args.now)

    command = COMMANDS[args.command]
    try:
        if args.now is not None and args.command in ("run", "health"):
            return command(deployment, config, now=now)
        return command(deployment, config)
    except CacingNagaError as exc:
        print(f"[ai_team] {args.command} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
