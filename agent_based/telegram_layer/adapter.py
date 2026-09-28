"""Phase 5 — python-telegram-bot adapter and service entrypoint (item 10).

The adapter only moves bytes: it registers the pure ``CacingNagaBot``
handlers with PTB, escapes nothing (the renderer already emits safe HTML),
and never touches decisions or scores. Deploy as a **separate long-lived
service** backed by the durable store — never inside the scheduled GitHub
Actions job.

Operational exposure stays disabled until ``TelegramConfig.enabled`` is
deliberately set AND the Phase 6 challenge contract passes.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any

from cacingnaga.config import AIAnalystConfig
from cacingnaga.errors import ContractViolation
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.snapshot import build_snapshot
from cacingnaga.store import AuditStore
from cacingnaga.transport import FakeTransport, TransportConfig

from orchestrator import AnalysisService

from .bot import CacingNagaBot
from .config import TelegramConfig


def build_screen_runner(
    store: AuditStore,
    config: AIAnalystConfig,
    transport: Any,
    transport_config: TransportConfig | None = None,
):
    """Closure that runs (or replays) one full analysis by run key.

    Reuses ``AnalysisService`` directly — the same idempotency and
    persistence path as CLI/scheduler triggers (plan item 1). Live data can
    replace the fixture snapshot without touching this layer: build the
    snapshot from ``download_ihsg``/``download_saham_batch`` and pass it in.
    """
    service = AnalysisService(
        config, store, transport, transport_config=transport_config
    )

    def screen(run_key: str):
        # v1 shadow deployments run on the deterministic fixture snapshot;
        # swap this for live frames via the legacy adapter when promoted.
        snapshot = build_snapshot(market_frame(), default_frames(), config)
        return service.run_full_analysis(snapshot, run_id=run_key, trigger="telegram")

    return screen


def create_bot(config: TelegramConfig, store: AuditStore) -> CacingNagaBot:
    """Assemble the bot with the real screen runner."""
    config.validate()
    ai_config = AIAnalystConfig()
    transport = FakeTransport({})          # Hermes adapter lands in a later phase
    return CacingNagaBot(
        config,
        store,
        screen_runner=build_screen_runner(store, ai_config, transport),
    )


def run_service(config: TelegramConfig) -> None:
    """Long-lived polling service (separate process from any scheduler).

    Refuses to start unless the feature flag is deliberately enabled — the
    Phase 5 item 11 gate. Requires ``python-telegram-bot`` at runtime only.
    """
    config.validate()
    if not config.enabled:
        raise ContractViolation(
            "telegram exposure is disabled (CACINGNAGA_TELEGRAM_ENABLED != 1); "
            "operational enablement awaits the Phase 6 challenge contract"
        )

    try:
        from telegram import Update
        from telegram.ext import (
            Application,
            CommandHandler,
            ContextTypes,
        )
    except ImportError as exc:  # pragma: no cover - environment guard
        raise ContractViolation(
            "python-telegram-bot is not installed; pip install python-telegram-bot"
        ) from exc

    store = AuditStore(config.store_path, thread_safe=True)   # handler threads
    bot = create_bot(config, store)

    async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        text = update.message.text if update.message else ""
        reply, parse_mode = bot.handle(user_id=user_id, chat_id=chat_id, text=text or "")
        if update.message is not None:
            await update.message.reply_text(reply, parse_mode=parse_mode)

    application = Application.builder().token(config.bot_token).build()
    for command in ("screen", "analyze", "debate", "market", "status", "why", "help", "start"):
        application.add_handler(CommandHandler(command, respond))
    application.run_polling(drop_pending_updates=True)


__all__ = ["build_screen_runner", "create_bot", "run_service"]
