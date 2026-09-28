"""Phase 5 — Telegram bot core (plan §7 Phase 5, items 1–7).

``CacingNagaBot`` is a **pure command dispatcher**: it takes parsed command
text, returns ``(text, parse_mode)`` replies, and knows nothing about
``python-telegram-bot``. The PTB adapter (``adapter.py``) only moves messages
in and replies out, so every rule here is testable offline.

Guarantees:

- authorization on every command (sender AND chat allowlists, item 3);
- ``/screen`` is single-flight and idempotent — overlapping triggers reuse
  the running/completed run via the store idempotency key (item 4);
- rendering always goes through the canonical renderer over *stored* runs;
  ``/why`` never re-runs an LLM and never parses previous messages (item 5);
- the canonical results are untouched: this layer only reads and renders.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from cacingnaga.errors import ContractViolation
from cacingnaga.store import AuditStore

from .config import TelegramConfig
from .render import (
    render_debate_message,
    render_help_message,
    render_market_message,
    render_run_message,
    render_status_message,
    render_why_message,
)


def normalize_ticker(raw: str) -> str:
    """Normalize user input to an IDX symbol: upper, bare, ``.JK`` suffix."""
    text = (raw or "").strip().upper()
    if not text:
        raise ContractViolation("ticker is required")
    text = text.lstrip("$")
    if "." in text:
        prefix, _, suffix = text.partition(".")
        if suffix != "JK" or not prefix or not prefix.isalnum():
            raise ContractViolation(f"not a valid IDX ticker: {raw!r}")
        text = prefix
    if not (1 <= len(text) <= 5) or not text.isalnum():
        raise ContractViolation(f"not a valid IDX ticker: {raw!r}")
    return f"{text}.JK"


class _Screens:
    """Single-flight guard for /screen (plan item 4)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()

    def try_acquire(self, key: str) -> bool:
        with self._lock:
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._in_flight.discard(key)


class CacingNagaBot:
    """Authorized command surface over ``AnalysisService`` (no scoring here)."""

    def __init__(
        self,
        config: TelegramConfig,
        store: AuditStore,
        screen_runner: Callable[[str], Any] | None = None,
    ) -> None:
        config.validate()
        self._config = config
        self._store = store
        # screen_runner(run_key) -> AnalysisServiceResult; injected by the
        # adapter so the bot core stays free of orchestration wiring.
        self._screen_runner = screen_runner
        self._screens = _Screens()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def handle(self, *, user_id: int | None, chat_id: int | None, text: str) -> tuple[str, str]:
        """Route one incoming command; returns (reply_text, parse_mode)."""
        if not self._config.is_authorized(user_id, chat_id):
            return ("⛔ Not authorized for this bot.", "HTML")
        text = (text or "").strip()
        if not text.startswith("/"):
            return ("Send a command — try /help.", "HTML")
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()      # /screen@MyBot → /screen
        argument = argument.strip()
        handlers = {
            "/screen": self.cmd_screen,
            "/analyze": self.cmd_analyze,
            "/debate": self.cmd_debate,
            "/market": self.cmd_market,
            "/status": self.cmd_status,
            "/why": self.cmd_why,
            "/help": self.cmd_help,
            "/start": self.cmd_help,
        }
        handler = handlers.get(command)
        if handler is None:
            return (f"Unknown command {command}. Try /help.", "HTML")
        return handler(argument)

    def _latest_run(self):
        runs = self._store.list_runs(limit=50)
        for stored in runs:
            if stored.run_status in ("COMPLETE", "PARTIAL", "FAILED"):
                return stored
        return None

    def _run_result_from_synthesis(self, run_id: str):
        """Rebuild the canonical RunResult from the stored synthesis record.

        This is the /screen, /analyze, /market and /why data path: everything
        is read back from the audit store, never re-computed.
        """
        from orchestrator import _run_result_from_payload

        for output in self._store.get_agent_outputs(run_id):
            # Challenge markers share the service agent name; only the true
            # synthesis carries the run_result payload.
            if (
                output["agent_name"] == "AnalysisService"
                and output["validation_ok"]
                and isinstance(output["validated"], dict)
                and "run_result" in output["validated"]
            ):
                return _run_result_from_payload(output["validated"]["run_result"])
        return None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def cmd_screen(self, argument: str) -> tuple[str, str]:
        if self._screen_runner is None:
            return ("Screening is not wired in this deployment.", "HTML")
        key = "screen"
        if not self._screens.try_acquire(key):
            return (
                "A screening run is already in progress — wait for it to finish "
                "(runs are idempotent; the result will appear in /status).",
                "HTML",
            )
        try:
            result = self._screen_runner("telegram-screen")
            run = result.run_result
            if run.run_status in ("PARTIAL", "FAILED"):
                return (
                    f"Run finished with status {run.run_status}: data/agent "
                    "problems — no recommendations are issued (NOT_EVALUATED).",
                    "HTML",
                )
            return (render_run_message(run), "HTML")
        finally:
            self._screens.release(key)

    def cmd_analyze(self, argument: str) -> tuple[str, str]:
        try:
            ticker = normalize_ticker(argument)
        except ContractViolation as exc:
            return (f"⚠️ {escape_outer(exc)}", "HTML")
        run = self._latest_run()
        if run is None:
            return ("No completed run yet — try /screen first.", "HTML")
        body = render_why_message(
            self._run_result_from_synthesis(run.run_id), ticker
        )
        return (f"[latest run: {run.run_id}]\n\n{body}", "HTML")

    def cmd_market(self, argument: str) -> tuple[str, str]:
        run = self._latest_run()
        if run is None:
            return ("No completed run yet — try /screen first.", "HTML")
        body = render_market_message(self._run_result_from_synthesis(run.run_id))
        return (f"[latest run: {run.run_id}]\n\n{body}", "HTML")

    def cmd_debate(self, argument: str) -> tuple[str, str]:
        """Phase 6 item 8: show the concise stored debate for one ticker."""
        try:
            ticker = normalize_ticker(argument)
        except ContractViolation as exc:
            return (f"⚠️ {escape_outer(exc)}", "HTML")
        run = self._latest_run()
        if run is None:
            return ("No completed run yet — try /screen first.", "HTML")
        body = render_debate_message(
            self._run_result_from_synthesis(run.run_id),
            ticker,
            self._store.get_challenges(run.run_id),
        )
        return (f"[latest run: {run.run_id}]\n\n{body}", "HTML")

    def cmd_status(self, argument: str) -> tuple[str, str]:
        return (render_status_message(self._store.list_runs(limit=8)), "HTML")

    def cmd_why(self, argument: str) -> tuple[str, str]:
        try:
            ticker = normalize_ticker(argument)
        except ContractViolation as exc:
            return (f"⚠️ {escape_outer(exc)}", "HTML")
        run = self._latest_run()
        if run is None:
            return ("No completed run yet — try /screen first.", "HTML")
        body = render_why_message(
            self._run_result_from_synthesis(run.run_id), ticker
        )
        return (f"[latest run: {run.run_id}]\n\n{body}", "HTML")

    def cmd_help(self, argument: str) -> tuple[str, str]:
        return (render_help_message(self._config.enabled), "HTML")


def escape_outer(exc: Exception) -> str:
    """Safe one-line rendering of expected validation errors."""
    from html import escape as _escape

    return _escape(str(exc), quote=False)
