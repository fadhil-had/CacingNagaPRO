"""Phase 5 — Telegram MVP package (plan §7 Phase 5).

Named ``telegram_layer`` (not ``telegram``) deliberately: importing as
``telegram`` would shadow the installed ``python-telegram-bot`` package.

Thin, authorized command surface over ``AnalysisService``:

- :mod:`config`  — feature flag + allowlists (exposure gated until Phase 6)
- :mod:`bot`     — pure command dispatcher (auth, single-flight, store reads)
- :mod:`render`  — canonical HTML rendering over stored results (split/escape)
- :mod:`adapter` — python-telegram-bot wiring + long-lived service entrypoint

No Telegram-specific data or scoring path exists anywhere: the bot reads the
audit store and renders the canonical stored result.
"""
from .bot import CacingNagaBot, normalize_ticker
from .config import TelegramConfig, load_telegram_config
from .render import (
    render_help_message,
    render_market_message,
    render_run_message,
    render_status_message,
    render_why_message,
    split_message,
)

__all__ = [
    "CacingNagaBot",
    "TelegramConfig",
    "load_telegram_config",
    "normalize_ticker",
    "render_help_message",
    "render_market_message",
    "render_run_message",
    "render_status_message",
    "render_why_message",
    "split_message",
]
