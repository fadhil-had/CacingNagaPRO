"""Phase 5 — Telegram configuration (plan §7 Phase 5).

The bot is a thin adapter over ``AnalysisService``: no Telegram-specific data
or scoring path exists anywhere. Operational exposure stays **feature-flagged
off** until the Phase 6 challenge contract passes (plan item 11); the config
gate is the enforcement point.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from cacingnaga.errors import ContractViolation


@dataclass(frozen=True)
class TelegramConfig:
    """Deployment configuration for the Telegram MVP service."""

    # Phase 5 item 11: operational enablement is feature-flagged until the
    # Phase 6 challenge contract passes. Default OFF.
    enabled: bool = False
    # The token never lives in code: read from the environment by the adapter.
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    # Plan item 3: authorize every sender AND chat against allowlists.
    allowed_user_ids: tuple[int, ...] = ()
    allowed_chat_ids: tuple[int, ...] = ()
    # Telegram hard limit is 4096 chars per message; we keep headroom for
    # entity overhead so a split never silently fails.
    max_message_chars: int = 3800
    max_rejected_display: int = 10
    store_path: str = "output/audit_store.sqlite3"

    @property
    def bot_token(self) -> str:
        return os.environ.get(self.bot_token_env, "")

    def validate(self) -> None:
        if self.max_message_chars < 200 or self.max_message_chars > 4096:
            raise ContractViolation(
                "telegram.max_message_chars must be within [200, 4096]"
            )
        if self.enabled:
            if not self.bot_token:
                raise ContractViolation(
                    f"enabled telegram bot requires env {self.bot_token_env}"
                )
            if not self.allowed_user_ids and not self.allowed_chat_ids:
                raise ContractViolation(
                    "enabled telegram bot requires a sender/chat allowlist"
                )
            if self.max_rejected_display < 1:
                raise ContractViolation("max_rejected_display must be positive")

    def is_authorized(self, user_id: int | None, chat_id: int | None) -> bool:
        """Every sender AND chat must pass its allowlist (plan item 3)."""
        user_ok = not self.allowed_user_ids or (user_id in self.allowed_user_ids)
        chat_ok = not self.allowed_chat_ids or (chat_id in self.allowed_chat_ids)
        if self.allowed_user_ids and self.allowed_chat_ids:
            return user_ok and chat_ok
        return user_ok or chat_ok


def load_telegram_config(env: dict[str, str] | None = None) -> TelegramConfig:
    """Build config from environment variables (safe defaults: disabled)."""
    env = dict(os.environ if env is None else env)

    def ids(name: str) -> tuple[int, ...]:
        raw = env.get(name, "")
        return tuple(int(x) for x in raw.replace(" ", "").split(",") if x)

    config = TelegramConfig(
        enabled=env.get("CACINGNAGA_TELEGRAM_ENABLED", "0") == "1",
        allowed_user_ids=ids("CACINGNAGA_TELEGRAM_USERS"),
        allowed_chat_ids=ids("CACINGNAGA_TELEGRAM_CHATS"),
        store_path=env.get("CACINGNAGA_STORE_PATH", "output/audit_store.sqlite3"),
    )
    config.validate()
    return config
