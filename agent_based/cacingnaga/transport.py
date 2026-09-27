"""Phase 3 — provider-neutral agent transport (plan §7 Phase 3, item 1; §4 item 16).

The Hermes runtime is deliberately unconfirmed, so nothing here imports or
assumes an SDK. Everything speaks in two shapes:

- ``AgentRequest``  — the *prompt contract*: compact envelope subset, JSON
  schema, system instructions, and an evidence-citation requirement.
- ``AgentResponse`` — the *result envelope*: raw text, extracted JSON payload,
  usage metadata, and latency.

A transport only turns a request into a response; all schema validation,
forbidden-field rejection, and persistence happen in the agents/runner (never
inside a transport), so a hostile provider cannot bypass the boundary.

``FakeTransport`` is the offline workhorse for tests: it maps agent identity to
a callable returning either a payload dict or an ``AgentResponse``. A real
Hermes/CLI/HTTP adapter can later implement ``AgentTransport`` without any
change to the agents above this layer (decision #16).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .errors import ContractViolation

# JSON extraction: the model may wrap the object in markdown fences.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class AgentRequest:
    """Everything an agent turn needs; no tool/shell/HTTP access implied."""

    agent_name: str                     # MarketAgent | TechnicalAgent | FlowAgent
    schema_version: str = SCHEMA_VERSION
    ticker: str | None = None           # per-candidate agents only
    envelope: Mapping[str, Any] = field(default_factory=dict)   # snapshot subset
    schema: Mapping[str, Any] = field(default_factory=dict)     # expected JSON shape
    system_instructions: str = ""
    user_prompt: str = ""
    run_id: str = ""

    def payload(self) -> dict[str, Any]:
        """Canonical audit payload of the prompt (hash-friendly)."""
        return {
            "agent_name": self.agent_name,
            "schema_version": self.schema_version,
            "ticker": self.ticker,
            "envelope": dict(self.envelope),
            "schema": dict(self.schema),
            "system_instructions": self.system_instructions,
            "user_prompt": self.user_prompt,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class AgentResponse:
    """Raw provider result; not yet validated or trusted."""

    raw_text: str = ""
    payload: dict[str, Any] | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)  # tokens, model, ...
    latency_ms: int = 0

    def payload_json(self) -> str:
        return json.dumps(self.payload) if self.payload is not None else self.raw_text


@runtime_checkable
class AgentTransport(Protocol):
    """Provider-neutral boundary. Implementations: fake, Hermes SDK, CLI, HTTP."""

    def complete(self, request: AgentRequest) -> AgentResponse: ...


class FakeTransport:
    """Deterministic offline transport for tests and shadow runs.

    Handlers map an agent name (optionally ``"AgentName:TICKER"``) to a
    callable ``(AgentRequest) -> dict | AgentResponse``. Unmapped agents raise
    ``ContractViolation`` so a test never silently talks to nothing.
    """

    def __init__(
        self,
        handlers: Mapping[str, Callable[[AgentRequest], Any]] | None = None,
        *,
        latency_ms: int = 0,
    ) -> None:
        self._handlers = dict(handlers or {})
        self.latency_ms = latency_ms
        self.calls: list[AgentRequest] = []          # test observability

    def register(self, name: str, handler: Callable[[AgentRequest], Any]) -> None:
        self._handlers[name] = handler

    def complete(self, request: AgentRequest) -> AgentResponse:
        self.calls.append(request)
        key = f"{request.agent_name}:{request.ticker}" if request.ticker else request.agent_name
        handler = self._handlers.get(key) or self._handlers.get(request.agent_name)
        if handler is None:
            raise ContractViolation(f"FakeTransport has no handler for {key!r}")
        start = time.perf_counter_ns()
        result = handler(request)
        if isinstance(result, AgentResponse):
            if self.latency_ms and not result.latency_ms:
                result = AgentResponse(
                    raw_text=result.raw_text, payload=result.payload,
                    usage=result.usage, latency_ms=self.latency_ms,
                )
            return result
        payload = json.loads(json.dumps(result))     # plain-dict handler
        return AgentResponse(
            raw_text=json.dumps(payload, sort_keys=True), payload=payload,
            latency_ms=self.latency_ms or (time.perf_counter_ns() - start) // 1_000_000,
        )


def extract_json_payload(raw_text: str) -> dict[str, Any]:
    """Extract the first JSON object from a provider response.

    Accepts fenced or bare objects; raises ``ContractViolation`` on anything
    that is not a JSON object — never guesses a partial answer.
    """
    match = _JSON_BLOCK.search(raw_text or "")
    if match is None:
        raise ContractViolation("agent response contains no JSON object")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ContractViolation(f"agent response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractViolation("agent response JSON must be an object")
    return payload


@dataclass(frozen=True)
class TransportConfig:
    """Bounded execution: no unbounded agent turn (plan §7 Phase 3, item 6)."""

    timeout_seconds: float = 30.0
    max_attempts: int = 2              # 1 try + 1 retry on transient errors
    retry_backoff_seconds: float = 0.5

    def validate(self) -> None:
        from .errors import ContractViolation as _CV
        if self.timeout_seconds <= 0:
            raise _CV("transport.timeout_seconds must be positive")
        if self.max_attempts < 1 or self.max_attempts > 5:
            raise _CV("transport.max_attempts must be within [1, 5]")
        if self.retry_backoff_seconds < 0:
            raise _CV("transport.retry_backoff_seconds must be non-negative")
