"""Live LLM transport tests (Phase 9).

No network: a stub client records the request and returns a canned response, so
every assertion is about *our* behaviour — per-agent routing, strict-schema
requests, usage tagging, and the failure modes.

Exit criteria under test:
- every agent name resolves to its own model/temperature, with ``models``
  overriding only the model id;
- a turn is issued as a strict-JSON request constrained by that agent's schema,
  and the persona reaches the system instruction;
- the response is tagged with the real model and ``gemini-live`` so audit can
  never mistake it for the smoke transport;
- normalization repairs case/shape only and records what it repaired;
- provider errors, empty text, and non-JSON all fail closed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from cacingnaga.errors import ContractViolation
from cacingnaga.llm_transport import (
    DEFAULT_ROUTES,
    RESPONSE_SCHEMAS,
    AgentRoute,
    GeminiLLMTransport,
    LLMTransportConfig,
    build_llm_config,
    build_llm_transport,
    normalize_payload,
)
from cacingnaga.transport import AgentRequest, TransportConfig

ENV = {"AGENT_LLM_API_KEY": "test-key"}


# ---------------------------------------------------------------------------
# Stub provider
# ---------------------------------------------------------------------------


class _StubModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({
            "model": model,
            "contents": contents,
            "config": config,
        })
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _StubClient:
    def __init__(self, responses):
        self.models = _StubModels(responses)


def _reply(payload, *, text=None):
    body = text if text is not None else json.dumps(payload)
    return SimpleNamespace(
        text=body,
        finish_reason="STOP",
        usage_metadata=SimpleNamespace(
            prompt_token_count=100, candidates_token_count=20, total_token_count=120
        ),
    )


def _request(agent_name="TechnicalAgent", **kwargs):
    return AgentRequest(
        agent_name=agent_name,
        schema={"fields": ["ticker", "trend"]},
        envelope={"candidates": [{"facts": {"ticker": "BBCA.JK"}}]},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Configuration and routing
# ---------------------------------------------------------------------------


def test_every_agent_has_its_own_route():
    """No two agents silently share a model: each role is a distinct voice."""
    routes = DEFAULT_ROUTES
    for agent in (
        "MarketAgent",
        "TechnicalAgent",
        "FlowAgent",
        "DecisionAgent",
        "ChallengeAgent",
        "DecisionAgentResolution",
    ):
        assert agent in routes, f"{agent} has no route"
        assert routes[agent].model.strip()
    # The three analyst roles must not be one model answering three prompts.
    analysts = {routes[a].model for a in ("MarketAgent", "TechnicalAgent", "FlowAgent")}
    assert len(analysts) > 1, "analyst agents collapse to a single model"

    # Peer-review turns are routed too, or consultation would fail closed.
    for agent in ("TechnicalAgentPeerReview", "FlowAgentPeerReview"):
        assert agent in routes


def test_models_override_only_the_model_id():
    config = build_llm_config(models={"TechnicalAgent": "custom-model"})
    route = config.route_for("TechnicalAgent")
    assert route.model == "custom-model"
    # Temperature/budget/persona stay per-agent; only the id moves.
    assert route.temperature == DEFAULT_ROUTES["TechnicalAgent"].temperature
    assert route.persona == DEFAULT_ROUTES["TechnicalAgent"].persona


def test_explicit_routes_win_over_models():
    config = LLMTransportConfig(
        routes={"TechnicalAgent": AgentRoute(model="route-model")},
        models={"TechnicalAgent": "model-id"},
    )
    assert config.route_for("TechnicalAgent").model == "route-model"


def test_unknown_agent_falls_back_to_default():
    config = build_llm_config(default_model="fallback-model")
    assert config.route_for("SomethingElse").model == "fallback-model"


def test_config_rejects_unknown_agent_route():
    with pytest.raises(ContractViolation, match="unknown agent"):
        build_llm_config(routes={"NotAnAgent": AgentRoute(model="m")})


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"provider": "openai"}, "unsupported llm provider"),
        ({"api_key_env": "  "}, "api_key_env"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"max_attempts": 0}, "max_attempts"),
        ({"max_attempts": 99}, "max_attempts"),
        ({"default_temperature": 5.0}, "default_temperature"),
    ],
)
def test_config_fails_closed_on_bad_bounds(kwargs, match):
    with pytest.raises(ContractViolation, match=match):
        build_llm_config(**kwargs)


def test_config_payload_carries_no_secret_value():
    config = build_llm_config()
    payload = json.dumps(config.payload())
    assert "test-key" not in payload
    assert config.payload()["api_key_env"] == "AGENT_LLM_API_KEY"


# ---------------------------------------------------------------------------
# Missing credentials and the request contract
# ---------------------------------------------------------------------------


def test_missing_api_key_fails_closed_with_actionable_message():
    with pytest.raises(ContractViolation, match=r"\$AGENT_LLM_API_KEY"):
        build_llm_transport(build_llm_config(), env={})


def test_blank_api_key_is_treated_as_absent():
    with pytest.raises(ContractViolation, match="requires"):
        build_llm_transport(build_llm_config(), env={"AGENT_LLM_API_KEY": "   "})


def test_turn_is_strict_json_constrained_by_the_agent_schema():
    client = _StubClient([_reply({"ticker": "BBCA.JK"})])
    transport = build_llm_transport(client=client, env=ENV)
    transport.complete(_request("TechnicalAgent"))

    config = client.models.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == RESPONSE_SCHEMAS["TechnicalAgent"]
    assert config.response_json_schema["additionalProperties"] is False
    # A strict schema must pin the enum vocabulary, not accept free text.
    trend = config.response_json_schema["properties"]["trend"]
    assert set(trend["enum"]) == {"BULLISH", "NEUTRAL", "BEARISH"}


def test_each_agent_is_issued_a_differently_configured_turn():
    """Two agents ⇒ two different models, and the routed model is used."""
    client = _StubClient([_reply({}), _reply({})])
    transport = build_llm_transport(client=client, env=ENV)
    transport.complete(_request("MarketAgent"))
    transport.complete(_request("FlowAgent"))

    models = [call["model"] for call in client.models.calls]
    assert models == [
        DEFAULT_ROUTES["MarketAgent"].model,
        DEFAULT_ROUTES["FlowAgent"].model,
    ]
    assert models[0] != models[1]
    assert client.models.calls[0]["config"].temperature == \
        DEFAULT_ROUTES["MarketAgent"].temperature


def test_persona_reaches_the_system_instruction():
    client = _StubClient([_reply({})])
    transport = build_llm_transport(client=client, env=ENV)
    transport.complete(_request("FlowAgent"))

    system = client.models.calls[0]["config"].system_instruction
    assert "CONSULTATION" not in system
    assert DEFAULT_ROUTES["FlowAgent"].persona[:40] in system
    assert "never proof" in system          # flow-agent certainty ban is present


def test_prompt_never_carries_the_api_key():
    client = _StubClient([_reply({})])
    transport = build_llm_transport(client=client, env=ENV)
    transport.complete(_request())
    assert "test-key" not in client.models.calls[0]["contents"]
    assert "test-key" not in str(client.models.calls[0]["config"].system_instruction)


def test_unregistered_agent_has_no_schema_and_is_refused():
    client = _StubClient([_reply({})])
    transport = build_llm_transport(client=client, env=ENV)
    with pytest.raises(ContractViolation, match="no response schema"):
        transport.complete(_request("MysteryAgent"))
    assert client.models.calls == []          # nothing was spent on the provider


# ---------------------------------------------------------------------------
# Responses, usage, normalization
# ---------------------------------------------------------------------------


def test_response_is_tagged_with_the_real_model_and_transport():
    client = _StubClient([_reply({"ticker": "BBCA.JK"})])
    transport = build_llm_transport(client=client, env=ENV)
    response = transport.complete(_request("TechnicalAgent"))

    assert response.payload == {"ticker": "BBCA.JK"}
    assert response.usage["model"] == DEFAULT_ROUTES["TechnicalAgent"].model
    assert response.usage["transport"] == "gemini-live"
    assert response.usage["agent"] == "TechnicalAgent"
    assert response.usage["prompt_tokens"] == 100
    assert response.usage["total_tokens"] == 120
    # The smoke tag must never appear on a live record.
    assert "CACINGNAGA_SMOKE_V1" not in json.dumps(response.usage)
    assert response.latency_ms >= 0


def test_calls_are_recorded_for_observability():
    client = _StubClient([_reply({})])
    transport = build_llm_transport(client=client, env=ENV)
    transport.complete(_request())
    assert [r.agent_name for r in transport.calls] == ["TechnicalAgent"]


def test_live_response_passes_the_real_agent_validation_pipeline():
    """A live turn is held to the same contract as any other provider."""
    from cacingnaga.fixtures import default_frames, market_frame
    from cacingnaga.config import AIAnalystConfig
    from cacingnaga.snapshot import build_snapshot, snapshot_to_agent_envelope
    from agents.technical_agent import run as run_technical

    snapshot = build_snapshot(market_frame(), default_frames(), AIAnalystConfig())
    envelope = snapshot_to_agent_envelope(snapshot)
    candidate = envelope["candidates"][0]
    refs = candidate["evidence_refs"]

    client = _StubClient([_reply({
        "ticker": candidate["facts"]["ticker"],
        "trend": "bullish",                     # lowercase: needs normalization
        "setup": candidate["facts"]["setup"],
        "momentum": "positive",
        "confidence_band": "medium",
        "reasons": f"ema20 above ema50 per {refs['ema20']}",
        "risks": [],
        "evidence_refs": [refs["ema20"]],
        "missing_facts": [],
    })])
    transport = build_llm_transport(client=client, env=ENV)
    outcome = run_technical(transport, candidate, config=TransportConfig(max_attempts=1))

    assert outcome.ok, outcome.error
    assert outcome.result.interpretation.trend == "BULLISH"
    assert "trend:bullish->BULLISH" in outcome.result.usage["normalized"]


def test_normalization_repairs_case_and_shape_only():
    payload = {
        "regime": "bull-market",                # not a contract token
        "confidence_band": "low",
        "reasons": "single string where a list is expected",
        "risk_flags": None,
        "evidence_refs": ["a", 7, "  ", "b"],
    }
    clean, repairs = normalize_payload(payload)

    # An unrecognized enum token is NOT guessed into a valid one: it passes
    # through so the contract validator rejects it and the turn fails closed.
    assert clean["regime"] == "bull-market"
    assert clean["confidence_band"] == "LOW"
    assert clean["reasons"] == ["single string where a list is expected"]
    assert clean["risk_flags"] == []
    assert clean["evidence_refs"] == ["a", "b"]   # non-strings dropped, not cast
    assert any("dropped-non-strings" in r for r in repairs)


def test_normalization_never_rewrites_numbers_or_unknown_fields():
    """Repair is shape-only: a model cannot smuggle a status or price in."""
    payload = {
        "price": 4200,
        "score": 0.99,
        "rank": 1,
        "final_status": "READY",
        "proposed_status": "ready",               # a real enum, safely cased
    }
    clean, _ = normalize_payload(payload)
    assert clean["price"] == 4200
    assert clean["score"] == 0.99
    assert clean["rank"] == 1
    assert clean["final_status"] == "READY"      # not an enum: untouched
    assert clean["proposed_status"] == "READY"


def test_normalization_leaves_a_clean_payload_untouched():
    payload = {
        "ticker": "BBCA.JK",
        "flow": "DISTRIBUTION",
        "evidence": ["a"],
        "evidence_refs": ["a"],
        "available_indicators": ["mfi"],
        "missing_indicators": [],
    }
    clean, repairs = normalize_payload(payload)
    assert clean == payload
    assert repairs == ()


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_provider_error_is_wrapped_and_never_leaks_the_key():
    client = _StubClient([RuntimeError("quota exceeded for key test-key")])
    transport = build_llm_transport(client=client, env=ENV)
    with pytest.raises(ContractViolation) as excinfo:
        transport.complete(_request())
    # The message is wrapped for the audit trail, but the key is redacted by
    # the caller's redact(); the transport itself must not echo it in usage.
    assert "provider call failed" in str(excinfo.value)


def test_empty_provider_text_fails_closed():
    client = _StubClient([_reply({}, text="")])
    transport = build_llm_transport(client=client, env=ENV)
    with pytest.raises(ContractViolation, match="empty response"):
        transport.complete(_request())


def test_non_json_provider_text_fails_closed():
    client = _StubClient([_reply({}, text="I think the trend is bullish")])
    transport = build_llm_transport(client=client, env=ENV)
    with pytest.raises(ContractViolation, match="not valid JSON"):
        transport.complete(_request())


def test_json_array_response_is_refused():
    """A list is not a reading; it must not be coerced into a dict."""
    client = _StubClient([_reply({}, text="[1, 2, 3]")])
    transport = build_llm_transport(client=client, env=ENV)
    with pytest.raises(ContractViolation, match="must be an object"):
        transport.complete(_request())


def test_transport_config_is_derived_for_the_ops_wrapper():
    config = build_llm_config(max_attempts=4, timeout_seconds=12.0)
    transport_config = config.transport_config()
    assert transport_config.max_attempts == 4
    assert transport_config.timeout_seconds == 12.0
    transport_config.validate()


def test_transport_works_wrapped_in_the_circuit_breaker():
    """The live transport must satisfy the existing ops wrapper unchanged."""
    from ops import CachedBreakerTransport

    client = _StubClient([_reply({}), _reply({})])
    inner = build_llm_transport(client=client, env=ENV)
    transport = CachedBreakerTransport(
        inner, transport_config=TransportConfig(max_attempts=2, retry_backoff_seconds=0.0)
    )
    transport.complete(_request())
    transport.complete(_request())
    # Identical request ⇒ one provider call served from cache.
    assert len(client.models.calls) == 1
    assert transport.breaker_state == "CLOSED"


def test_gemini_transport_tag_is_stable():
    """The audit tag is a contract; renaming it would orphan past runs."""
    assert GeminiLLMTransport.TRANSPORT_TAG == "gemini-live"
