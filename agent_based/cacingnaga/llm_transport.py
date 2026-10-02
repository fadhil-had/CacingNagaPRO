"""Live LLM transport — one real model per agent behind the provider boundary.

This module is the concrete ``AgentTransport`` implementation that replaces the
deterministic ``deployment.CacingNagaSmokeTransport``. It keeps every guarantee
the Phase 3 pipeline already relies on:

- **Provider-neutral boundary.** It only implements ``complete(request)``; schema
  validation, forbidden-field rejection, and evidence-citation checks stay in
  ``agents/base.py``, so a hostile or sloppy provider still cannot bypass them.
- **Structured output.** Every turn is requested as strict JSON constrained by a
  per-agent response schema, so the provider cannot invent fields the contracts
  would reject anyway.
- **Auditability.** Each ``AgentResponse`` carries the resolved model, the
  transport tag (``gemini-live``), and any normalization applied, all of which
  land in ``agent_outputs.usage``.

Two deliberate design choices:

1. **Per-agent model routing.** Each agent name maps to its own
   :class:`AgentRoute` (model, temperature, thinking budget, and a role
   persona). Different agents therefore reason independently rather than sharing
   one model's voice, and each can be tuned without touching the others.
2. **Narrow normalization.** Live models routinely return ``"bullish"`` instead
   of ``"BULLISH"`` or a bare string where a list is required. The transport
   repairs *shape and case only* — enum casing and scalar-to-list coercion — and
   records every repair in ``usage["normalized"]``. It never invents content,
   never touches numbers, and never weakens the evidence-citation boundary; a
   payload that still violates a contract fails closed exactly as before.

No LLM in this file computes status, score, price, or rank. Python owns those.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from .contracts import (
    CANDIDATE_STATUSES,
    CHALLENGE_RESOLUTIONS,
    CHALLENGE_STANCES,
    CHALLENGE_STATUS_EFFECTS,
    CONFIDENCE_BANDS,
    FLOW_STATES,
    FLOW_STRENGTHS,
    MARKET_REGIMES,
    MOMENTUMS,
    SETUPS,
    SWING_ENVIRONMENTS,
    TRENDS,
)
from .errors import ContractViolation
from .transport import AgentRequest, AgentResponse, TransportConfig

# Imported from ops for one reason: the secret-separation check and the default
# api_key_env must name the same variable, or the check would cross-compare the
# wrong domains. ops holds the canonical name. Imported lazily-ish (absolute,
# since cacingnaga is not a subpackage of agent_based) to avoid an import cycle.
from ops import AGENT_LLM_TOKEN_ENV

TRANSPORT_TAG = "gemini-live"

#: Free-tier defaults. Google's Free tier bills both input and output at "free
#: of charge" for these models, so a daily run costs nothing — but the daily
#: request cap (RPD) is the real constraint, not the dollar amount. See
#: ``RUNBOOK.md`` §1a for the per-run call budget.
#:
#: The 2.5 generation is deliberately avoided: Google restricts it to projects
#: that already used it, so a fresh account would fail on a brand-new key.
#: These are the current GA models, and both are the recommended picks for new
#: projects.
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Per-agent response schemas (strict JSON the provider is constrained to)
# ---------------------------------------------------------------------------

_STRING = {"type": "string"}
_STRING_LIST = {"type": "array", "items": {"type": "string"}}


def _object(properties: Mapping[str, Any], *, nullable: tuple[str, ...] = ()) -> dict[str, Any]:
    """One strict object schema: no extra keys, every declared field required."""
    fields = dict(properties)
    for name in nullable:
        fields[name] = {"type": ["string", "null"]}
    required = [name for name in fields if name not in nullable]
    return {
        "type": "object",
        "properties": fields,
        "required": required,
        "additionalProperties": False,
    }


def _enum(values: tuple[str, ...]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


RESPONSE_SCHEMAS: dict[str, dict[str, Any]] = {
    "MarketAgent": _object({
        "regime": _enum(MARKET_REGIMES),
        "confidence_band": _enum(CONFIDENCE_BANDS),
        "swing_environment": _enum(SWING_ENVIRONMENTS),
        "secondary_direction": dict(_STRING),
        "reasons": _STRING_LIST,
        "risk_flags": _STRING_LIST,
        "evidence_refs": _STRING_LIST,
    }, nullable=("secondary_direction",)),
    "TechnicalAgent": _object({
        "ticker": _STRING,
        "trend": _enum(TRENDS),
        "setup": _enum(SETUPS),
        "momentum": _enum(MOMENTUMS),
        "confidence_band": _enum(CONFIDENCE_BANDS),
        "reasons": _STRING_LIST,
        "risks": _STRING_LIST,
        "evidence_refs": _STRING_LIST,
        "missing_facts": _STRING_LIST,
    }),
    "FlowAgent": _object({
        "ticker": _STRING,
        "flow": _enum(FLOW_STATES),
        "strength": _enum(FLOW_STRENGTHS),
        "confidence_band": _enum(CONFIDENCE_BANDS),
        "evidence": _STRING_LIST,
        "risks": _STRING_LIST,
        "evidence_refs": _STRING_LIST,
        "available_indicators": _STRING_LIST,
        "missing_indicators": _STRING_LIST,
    }),
    "DecisionAgent": _object({
        "ticker": _STRING,
        "proposed_status": _enum(CANDIDATE_STATUSES),
        "confidence_band": _enum(CONFIDENCE_BANDS),
        "status_change_reason": _STRING,
        "reasons": _STRING_LIST,
        "concerns": _STRING_LIST,
        "evidence_refs": _STRING_LIST,
    }),
    "ChallengeAgent": _object({
        "agent_name": _STRING,
        "ticker": _STRING,
        "conflict_rule_id": _STRING,
        "stance": _enum(CHALLENGE_STANCES),
        "missing_data": _STRING_LIST,
        "reasons": _STRING_LIST,
        "evidence_refs": _STRING_LIST,
    }),
    "DecisionAgentResolution": _object({
        "ticker": _STRING,
        "conflict_rule_id": _STRING,
        "resolution": _enum(CHALLENGE_RESOLUTIONS),
        "status_effect": _enum(CHALLENGE_STATUS_EFFECTS),
        "summary": _STRING,
        "reasons": _STRING_LIST,
        "evidence_refs": _STRING_LIST,
    }),
}


#: Peer-review turns answer the *same* contracts as their first pass.
RESPONSE_SCHEMAS["TechnicalAgentPeerReview"] = RESPONSE_SCHEMAS["TechnicalAgent"]
RESPONSE_SCHEMAS["FlowAgentPeerReview"] = RESPONSE_SCHEMAS["FlowAgent"]


# ---------------------------------------------------------------------------
# Narrow, auditable normalization (shape + case only)
# ---------------------------------------------------------------------------

ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "regime": MARKET_REGIMES,
    "swing_environment": SWING_ENVIRONMENTS,
    "confidence_band": CONFIDENCE_BANDS,
    "trend": TRENDS,
    "setup": SETUPS,
    "momentum": MOMENTUMS,
    "flow": FLOW_STATES,
    "strength": FLOW_STRENGTHS,
    "proposed_status": CANDIDATE_STATUSES,
    "stance": CHALLENGE_STANCES,
    "resolution": CHALLENGE_RESOLUTIONS,
    "status_effect": CHALLENGE_STATUS_EFFECTS,
}

LIST_FIELDS = frozenset({
    "reasons",
    "risks",
    "concerns",
    "evidence",
    "risk_flags",
    "evidence_refs",
    "missing_facts",
    "missing_data",
    "available_indicators",
    "missing_indicators",
})


def normalize_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Repair case/shape drift only; returns ``(payload, repairs)``.

    Enum strings are matched case-insensitively (whitespace and ``-``/``_``
    normalized) against the contract vocabulary; list fields accept a bare
    string or ``None``. Non-string list members are dropped rather than
    stringified. Anything not covered here is passed through untouched so the
    contract validators remain the only authority on content.
    """
    clean = dict(payload)
    repairs: list[str] = []
    for name, value in list(clean.items()):
        allowed = ENUM_FIELDS.get(name)
        if allowed and isinstance(value, str):
            probe = value.strip().upper().replace(" ", "_").replace("-", "_")
            if probe != value and probe in allowed:
                clean[name] = probe
                repairs.append(f"{name}:{value}->{probe}")
            continue
        if name in LIST_FIELDS:
            if value is None:
                clean[name] = []
                repairs.append(f"{name}:null->[]")
                continue
            if isinstance(value, str):
                clean[name] = [value] if value.strip() else []
                repairs.append(f"{name}:str->list")
                continue
            if isinstance(value, (list, tuple)):
                kept = [item for item in value if isinstance(item, str) and item.strip()]
                if len(kept) != len(value):
                    repairs.append(f"{name}:dropped-non-strings")
                clean[name] = kept
    return clean, tuple(repairs)


# ---------------------------------------------------------------------------
# Per-agent routing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRoute:
    """One agent's model assignment — its own AI for its own turn type."""

    model: str = DEFAULT_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    max_output_tokens: int = 1536
    thinking_budget: int = 0
    persona: str = ""


DEFAULT_ROUTES: dict[str, AgentRoute] = {
    "MarketAgent": AgentRoute(
        model="gemini-3.8-flash",
        temperature=0.1,
        max_output_tokens=2048,
        thinking_budget=1024,
        persona=(
            "You read the IHSG regime for Indonesian swing traders. Judge trend "
            "structure, momentum, volatility, and breadth from the envelope "
            "alone. Confine yourself to the market: you never comment on any "
            "individual candidate and you never forecast an index level."
        ),
    ),
    "TechnicalAgent": AgentRoute(
        model="gemini-3.5-flash-lite",
        temperature=0.15,
        max_output_tokens=1536,
        thinking_budget=512,
        persona=(
            "You are a price-structure specialist. Explain the Python setup "
            "label using trend, momentum, support/resistance, and volume "
            "context. State plainly which facts are absent instead of "
            "compensating for them with narrative."
        ),
    ),
    "FlowAgent": AgentRoute(
        model="gemini-3.5-flash-lite",
        temperature=0.1,
        max_output_tokens=1280,
        thinking_budget=256,
        persona=(
            "You read OHLCV-derived distribution only. MFI, CMF, OBV slope, and "
            "relative volume are indications, never proof of activity by "
            "bandar, foreign investors, or brokers. When no directional "
            "indicator exists, return UNKNOWN — do not launder absence into "
            "NEUTRAL."
        ),
    ),
    "DecisionAgent": AgentRoute(
        model="gemini-3.8-flash",
        temperature=0.1,
        max_output_tokens=2048,
        thinking_budget=1024,
        persona=(
            "You weigh the analyst readings against Python's deterministic "
            "assessment and the recorded conflicts. Propose a status only; "
            "Python disposes. Any proposal that differs from python_status "
            "must justify itself with cited evidence."
        ),
    ),
    "ChallengeAgent": AgentRoute(
        model="gemini-3.5-flash-lite",
        temperature=0.2,
        max_output_tokens=1280,
        thinking_budget=512,
        persona=(
            "You are under challenge. Defend, revise, or withdraw your own "
            "recorded reading, and concede any data you do not have. Never "
            "attack the other agent's reasoning or introduce new claims."
        ),
    ),
    "DecisionAgentResolution": AgentRoute(
        model="gemini-3.8-flash",
        temperature=0.0,
        max_output_tokens=1536,
        thinking_budget=1024,
        persona=(
            "You classify a completed debate: did the conflict stand, was a "
            "reading genuinely revised, or did the agents simply disagree? "
            "Choose the classification that the recorded stances actually "
            "support, even when that means UNRESOLVED."
        ),
    ),
}

DEFAULT_ROUTES["TechnicalAgentPeerReview"] = replace(
    DEFAULT_ROUTES["TechnicalAgent"],
    model="gemini-3.8-flash",
    temperature=0.1,
    thinking_budget=1024,
)
DEFAULT_ROUTES["FlowAgentPeerReview"] = replace(
    DEFAULT_ROUTES["FlowAgent"],
    model="gemini-3.8-flash",
    temperature=0.1,
    thinking_budget=1024,
)


# ---------------------------------------------------------------------------
# Transport configuration + construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMTransportConfig:
    """Provider settings; secrets stay in the environment (only names here)."""

    api_key_env: str = AGENT_LLM_TOKEN_ENV
    provider: str = "gemini"
    default_model: str = DEFAULT_MODEL
    default_temperature: float = DEFAULT_TEMPERATURE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_output_tokens: int = 1536
    models: Mapping[str, str] = field(default_factory=dict)
    routes: Mapping[str, AgentRoute] = field(default_factory=dict)

    def validate(self) -> None:
        if self.provider != "gemini":
            raise ContractViolation(
                f"unsupported llm provider {self.provider!r} (expected 'gemini')"
            )
        if not self.api_key_env.strip():
            raise ContractViolation("llm.api_key_env must name an environment variable")
        if self.timeout_seconds <= 0:
            raise ContractViolation("llm.timeout_seconds must be positive")
        if self.max_attempts < 1 or self.max_attempts > 5:
            raise ContractViolation("llm.max_attempts must be within [1, 5]")
        if not 0.0 <= self.default_temperature <= 2.0:
            raise ContractViolation("llm.default_temperature must be within [0, 2]")
        for name, route in self.routes.items():
            if name not in DEFAULT_ROUTES:
                raise ContractViolation(
                    f"llm.routes has an entry for unknown agent {name!r}; known "
                    f"agents: {sorted(DEFAULT_ROUTES)}"
                )
            if not route.model.strip():
                raise ContractViolation(f"llm.routes[{name}].model must be non-empty")

    def route_for(self, agent_name: str) -> AgentRoute:
        """Resolve an agent's route.

        Precedence: an explicit ``routes`` entry, then the built-in default route
        with a ``models`` override applied, then a generic default built from
        ``default_model``. A ``models`` override changes only the model id, so
        the per-agent temperature, token budget, and persona are preserved.
        """
        explicit = self.routes.get(agent_name)
        if explicit is not None:
            return explicit
        builtin = DEFAULT_ROUTES.get(agent_name)
        override = self.models.get(agent_name)
        if builtin is not None:
            if override and override != builtin.model:
                return replace(builtin, model=override)
            return builtin
        return AgentRoute(
            model=override or self.default_model,
            temperature=self.default_temperature,
            max_output_tokens=self.max_output_tokens,
        )

    def transport_config(self) -> TransportConfig:
        return TransportConfig(
            timeout_seconds=self.timeout_seconds,
            max_attempts=self.max_attempts,
            retry_backoff_seconds=1.0,
        )

    def payload(self) -> dict[str, Any]:
        """JSON-safe audit view: model assignments only, never the key value."""
        return {
            "provider": self.provider,
            "api_key_env": self.api_key_env,
            "default_model": self.default_model,
            "default_temperature": self.default_temperature,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "models": dict(self.models),
            "routes": {
                name: {
                    "model": route.model,
                    "temperature": route.temperature,
                    "max_output_tokens": route.max_output_tokens,
                    "thinking_budget": route.thinking_budget,
                }
                for name, route in sorted(self.routes.items())
            },
        }


def build_llm_config(
    *,
    api_key_env: str = AGENT_LLM_TOKEN_ENV,
    provider: str = "gemini",
    default_model: str = DEFAULT_MODEL,
    default_temperature: float = DEFAULT_TEMPERATURE,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    models: Mapping[str, str] | None = None,
    routes: Mapping[str, AgentRoute] | None = None,
) -> LLMTransportConfig:
    config = LLMTransportConfig(
        api_key_env=api_key_env,
        provider=provider,
        default_model=default_model,
        default_temperature=default_temperature,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        models=dict(models or {}),
        routes=dict(routes or {}),
    )
    config.validate()
    return config


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


class GeminiLLMTransport:
    """Live ``AgentTransport`` over ``google-genai`` with per-agent routing.

    Every turn is issued as a strict-JSON request constrained by the agent's
    response schema. The transport performs **no** domain validation: a payload
    that breaks a contract still fails closed in ``agents/base.py``.
    """

    TRANSPORT_TAG = TRANSPORT_TAG

    def __init__(
        self,
        config: LLMTransportConfig,
        *,
        client: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        config.validate()
        env = dict(os.environ) if env is None else dict(env)
        api_key = (env.get(config.api_key_env) or "").strip()
        if not api_key:
            raise ContractViolation(
                f"live LLM transport requires ${config.api_key_env}; export it or "
                "run with --transport smoke"
            )
        self._api_key = api_key
        self._client = client if client is not None else self._build_client(api_key)
        self.calls: list[AgentRequest] = []

    # -- provider plumbing ---------------------------------------------------

    @staticmethod
    def _build_client(api_key: str) -> Any:
        try:
            from google import genai
        except ImportError as exc:
            raise ContractViolation(
                "the live LLM transport needs the google-genai package "
                "(pip install google-genai), or run with --transport smoke"
            ) from exc
        return genai.Client(api_key=api_key)

    def _generate(self, route: AgentRoute, request: AgentRequest, system: str) -> Any:
        from google.genai import types

        schema = RESPONSE_SCHEMAS.get(request.agent_name)
        if schema is None:
            raise ContractViolation(
                f"no response schema registered for agent {request.agent_name!r}"
            )
        gen_config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=route.temperature,
            max_output_tokens=route.max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=schema,
            http_options=types.HttpOptions(
                timeout=int(self.config.timeout_seconds * 1000)
            ),
            thinking_config=types.ThinkingConfig(
                thinking_budget=route.thinking_budget
            ),
        )
        return self._client.models.generate_content(
            model=route.model,
            contents=request.user_prompt,
            config=gen_config,
        )

    # -- AgentTransport ------------------------------------------------------

    def complete(self, request: AgentRequest) -> AgentResponse:
        """One live agent turn. Raises on provider failure; never improvises."""
        self.calls.append(request)
        route = self.config.route_for(request.agent_name)
        system = request.system_instructions
        if route.persona:
            system = f"{system}\n\nRole focus:\n{route.persona}"

        started = time.perf_counter_ns()
        try:
            response = self._generate(route, request, system)
        except Exception as exc:  # provider/transport errors stay bounded upstream
            raise ContractViolation(
                f"{request.agent_name} provider call failed: "
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc

        raw_text = (getattr(response, "text", "") or "").strip()
        if not raw_text:
            raise ContractViolation(
                f"{request.agent_name} provider returned an empty response "
                f"(finish_reason={getattr(response, 'finish_reason', 'unknown')})"
            )
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ContractViolation(
                f"{request.agent_name} response is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ContractViolation(
                f"{request.agent_name} response JSON must be an object"
            )

        payload, repairs = normalize_payload(payload)
        usage: dict[str, Any] = {
            "model": route.model,
            "transport": self.TRANSPORT_TAG,
            "provider": self.config.provider,
            "agent": request.agent_name,
            "temperature": route.temperature,
        }
        if repairs:
            usage["normalized"] = list(repairs)
        tokens = getattr(response, "usage_metadata", None)
        if tokens is not None:
            for source, target in (
                ("prompt_token_count", "prompt_tokens"),
                ("candidates_token_count", "completion_tokens"),
                ("total_token_count", "total_tokens"),
            ):
                value = getattr(tokens, source, None)
                if isinstance(value, int):
                    usage[target] = value

        return AgentResponse(
            raw_text=raw_text,
            payload=payload,
            usage=usage,
            latency_ms=(time.perf_counter_ns() - started) // 1_000_000,
        )


def build_llm_transport(
    config: LLMTransportConfig | None = None,
    *,
    client: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> GeminiLLMTransport:
    """Convenience factory used by the deployment entrypoint and tests."""
    return GeminiLLMTransport(config or build_llm_config(), client=client, env=env)


__all__ = [
    "DEFAULT_ROUTES",
    "ENUM_FIELDS",
    "LIST_FIELDS",
    "RESPONSE_SCHEMAS",
    "TRANSPORT_TAG",
    "AgentRoute",
    "GeminiLLMTransport",
    "LLMTransportConfig",
    "build_llm_config",
    "build_llm_transport",
    "normalize_payload",
]
