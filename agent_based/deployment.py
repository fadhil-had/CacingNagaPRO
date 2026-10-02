"""Deployment layer — wire the pipeline to a real post-market run (plan §8/§10).

``DeploymentConfig`` externalizes strategy/integration settings from
``config/ai_team.toml`` (plan §10: configuration is validated at startup and
lands in audit metadata; secrets stay in the environment, plan §11).

``load_live_snapshot`` builds the immutable snapshot from real IDX data
through the frozen-legacy loaders only (``screener_core.download_ihsg`` /
``download_saham_batch`` / ``ambil_semua_ticker_dari_excel``): the deployment
layer adds **no** new data path of its own, so freshness behavior, symbol
normalization, and the price basis stay exactly the legacy benchmark's.

``CacingNagaSmokeTransport`` is a *deliberately labeled* deterministic
shadow transport (plan §4 item 16: the Hermes runtime is unconfirmed, so
nothing here imports or guesses an SDK). Every payload it returns carries
``model: "CACINGNAGA_SMOKE_V1"`` so a shadow run can never be mistaken for
a live-provider run in the audit trail. It exists so the deployment
entrypoint can be exercised end to end — including the market-close,
stale-data, idempotency, and replay paths — before a real transport lands.

No LLM computes status, score, levels, or rank anywhere in this file.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from cacingnaga.config import AIAnalystConfig, ScreenerConfig
from cacingnaga.errors import ContractViolation, SnapshotError
from cacingnaga.legacy_adapter import get_legacy_ranking
from cacingnaga.llm_transport import LLMTransportConfig, build_llm_config
from cacingnaga.snapshot import AnalysisSnapshot, build_snapshot
from ops import AGENT_LLM_TOKEN_ENV
from cacingnaga.transport import AgentRequest, AgentResponse, TransportConfig

from scheduler import IDXCalendar, SchedulerConfig

DEPLOYMENT_CONFIG_VERSION = "DEPLOYMENT_CONFIG_1"
DEFAULT_CONFIG_PATH = Path("config/ai_team.toml")

_ID_RE = re.compile(r"[A-Za-z]+Facts:[0-9a-f]{12}:[a-zA-Z0-9_]+")


# ---------------------------------------------------------------------------
# Externalized configuration (plan §10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeploymentConfig:
    """Validated view of ``config/ai_team.toml`` + the deployment environment."""

    version: str = DEPLOYMENT_CONFIG_VERSION
    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    max_agent_pool_size: int = 30
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    # Universe settings (live runs).
    universe_source: str = "excel"             # excel | fixed
    universe_excel_path: str = "resource/daftar-saham.xlsx"
    universe_fixed: tuple[str, ...] = ()
    data_period: str = "2y"
    # Ops bounds (CircuitBreakerPolicy wiring for the real transport later).
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    half_open_probes: int = 1
    # Telegram (names of env vars only; values never live here).
    telegram_enabled: bool = False
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_allowed_user_ids: tuple[int, ...] = ()
    telegram_allowed_chat_ids: tuple[int, ...] = ()
    store_path: str = "output/audit_store.sqlite3"
    #: Phase 9 live-provider + cross-agent consultation settings. The smoke
    #: transport remains the default until the agent LLM key is exported.
    llm_enabled: bool = False
    llm_api_key_env: str = AGENT_LLM_TOKEN_ENV
    llm_provider: str = "gemini"
    llm_default_model: str = "gemini-2.5-flash"
    llm_default_temperature: float = 0.2
    llm_timeout_seconds: float = 60.0
    llm_max_attempts: int = 3
    #: agent name -> model id, overriding the built-in per-agent routes.
    llm_models: Mapping[str, str] = field(default_factory=dict)
    #: Run one Technical/Flow consultation round per candidate.
    peer_review: bool = False

    def validate(self) -> None:
        from cacingnaga.errors import ContractViolation as _CV

        if self.version != DEPLOYMENT_CONFIG_VERSION:
            raise _CV(
                f"unsupported deployment config version: {self.version} "
                f"(expected {DEPLOYMENT_CONFIG_VERSION})"
            )
        if self.universe_source not in ("excel", "fixed"):
            raise _CV("scheduler.universe.source must be 'excel' or 'fixed'")
        if self.universe_source == "fixed" and not self.universe_fixed:
            raise _CV("universe source 'fixed' requires scheduler.universe.tickers")
        if self.data_period not in ("1y", "2y", "5y", "10y"):
            raise _CV("scheduler.universe.period must be one of 1y/2y/5y/10y")
        if self.max_agent_pool_size < 4:
            raise _CV("policy.max_agent_pool_size must exceed the top-3 cap")
        if self.failure_threshold < 1 or self.cooldown_seconds < 0:
            raise _CV("ops circuit-breaker bounds are invalid")
        if self.half_open_probes < 1:
            raise _CV("ops.half_open_probes must be at least 1")
        if self.llm_max_attempts < 1 or self.llm_max_attempts > 5:
            raise _CV("llm.max_attempts must be within [1, 5]")
        if self.llm_timeout_seconds <= 0:
            raise _CV("llm.timeout_seconds must be positive")
        if not 0.0 <= self.llm_default_temperature <= 2.0:
            raise _CV("llm.default_temperature must be within [0, 2]")
        self.scheduler.validate()

    def payload(self) -> dict[str, Any]:
        """JSON-safe audit payload (no secret *values*, only env-var names)."""
        cal = self.scheduler.calendar
        return {
            "version": self.version,
            "screener": {
                "min_turnover_idr": self.screener.min_turnover_idr,
                "min_price_idr": self.screener.min_price_idr,
                "max_atr_pct": self.screener.max_atr_pct,
                "min_history_bars": self.screener.min_history_bars,
            },
            "max_agent_pool_size": self.max_agent_pool_size,
            "scheduler": {
                "close_hour": self.scheduler.close_hour,
                "close_minute": self.scheduler.close_minute,
                "earliest_run_after_close_minutes":
                    self.scheduler.earliest_run_after_close_minutes,
                "look_back_days": self.scheduler.look_back_days,
                "extra_holidays": sorted(cal.extra_holidays),
            },
            "universe": {
                "source": self.universe_source,
                "excel_path": self.universe_excel_path,
                "fixed": list(self.universe_fixed),
                "period": self.data_period,
            },
            "ops": {
                "failure_threshold": self.failure_threshold,
                "cooldown_seconds": self.cooldown_seconds,
                "half_open_probes": self.half_open_probes,
            },
            "telegram": {
                "enabled": self.telegram_enabled,
                "bot_token_env": self.telegram_bot_token_env,
                "allowed_user_ids": list(self.telegram_allowed_user_ids),
                "allowed_chat_ids": list(self.telegram_allowed_chat_ids),
            },
            "llm": self.llm_config().payload(),
            "peer_review": self.peer_review,
            "store_path": self.store_path,
        }

    def llm_config(self) -> LLMTransportConfig:
        """Live-provider settings (model routing only — never a secret value)."""
        return build_llm_config(
            api_key_env=self.llm_api_key_env,
            provider=self.llm_provider,
            default_model=self.llm_default_model,
            default_temperature=self.llm_default_temperature,
            timeout_seconds=self.llm_timeout_seconds,
            max_attempts=self.llm_max_attempts,
            models=self.llm_models,
        )


def load_deployment_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    env: Mapping[str, str] | None = None,
) -> DeploymentConfig:
    """Load and validate ``ai_team.toml`` (startup gate, plan §10)."""
    import os

    env = dict(os.environ) if env is None else dict(env)
    file = Path(path)
    if not file.exists():
        raise ContractViolation(f"deployment config not found: {file}")
    try:
        raw = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ContractViolation(f"deployment config is not valid TOML: {exc}") from exc

    screener_raw = raw.get("screener", {})
    policy_raw = raw.get("policy", {})
    sched_raw = raw.get("scheduler", {})
    universe_raw = sched_raw.get("universe", {})
    ops_raw = raw.get("ops", {})
    tg_raw = raw.get("telegram", {})
    storage_raw = raw.get("storage", {})
    llm_raw = raw.get("llm", {})

    calendar = IDXCalendar(
        extra_holidays=frozenset(sched_raw.get("extra_holidays", []))
    )
    scheduler_cfg = SchedulerConfig(
        close_hour=int(sched_raw.get("close_hour", 16)),
        close_minute=int(sched_raw.get("close_minute", 0)),
        earliest_run_after_close_minutes=int(
            sched_raw.get("earliest_run_after_close_minutes", 0)
        ),
        look_back_days=int(sched_raw.get("look_back_days", 5)),
        calendar=calendar,
    )
    config = DeploymentConfig(
        version=str(raw.get("meta", {}).get("version", DEPLOYMENT_CONFIG_VERSION)),
        screener=ScreenerConfig(
            min_turnover_idr=float(screener_raw.get("min_turnover_idr", 1_000_000_000.0)),
            min_price_idr=float(screener_raw.get("min_price_idr", 100.0)),
            max_atr_pct=float(screener_raw.get("max_atr_pct", 0.04)),
            min_history_bars=int(screener_raw.get("min_history_bars", 260)),
        ),
        max_agent_pool_size=int(policy_raw.get("max_agent_pool_size", 30)),
        scheduler=scheduler_cfg,
        universe_source=str(universe_raw.get("source", "excel")),
        universe_excel_path=str(universe_raw.get("excel_path", "resource/daftar-saham.xlsx")),
        universe_fixed=tuple(str(t).upper() for t in universe_raw.get("tickers", []) or ()),
        data_period=str(universe_raw.get("period", "2y")),
        failure_threshold=int(ops_raw.get("failure_threshold", 3)),
        cooldown_seconds=float(ops_raw.get("cooldown_seconds", 60.0)),
        half_open_probes=int(ops_raw.get("half_open_probes", 1)),
        telegram_enabled=bool(tg_raw.get("enabled", False)),
        telegram_bot_token_env=str(tg_raw.get("bot_token_env", "TELEGRAM_BOT_TOKEN")),
        telegram_allowed_user_ids=tuple(int(i) for i in tg_raw.get("allowed_user_ids", []) or ()),
        telegram_allowed_chat_ids=tuple(int(i) for i in tg_raw.get("allowed_chat_ids", []) or ()),
        store_path=str(storage_raw.get("store_path", "output/audit_store.sqlite3")),
        llm_enabled=bool(llm_raw.get("enabled", False)),
        llm_api_key_env=str(llm_raw.get("api_key_env", AGENT_LLM_TOKEN_ENV)),
        llm_provider=str(llm_raw.get("provider", "gemini")),
        llm_default_model=str(llm_raw.get("default_model", "gemini-2.5-flash")),
        llm_default_temperature=float(llm_raw.get("default_temperature", 0.2)),
        llm_timeout_seconds=float(llm_raw.get("timeout_seconds", 60.0)),
        llm_max_attempts=int(llm_raw.get("max_attempts", 3)),
        llm_models={
            str(name): str(model)
            for name, model in (llm_raw.get("models", {}) or {}).items()
        },
        peer_review=bool(raw.get("agents", {}).get("peer_review", False)),
    )
    config.validate()

    # Secret separation is enforced at startup (plan §11): a shared value
    # across domains fails closed before anything runs.
    from ops import validate_secret_separation

    token_env = config.telegram_bot_token_env
    # Keyed by the three secret-domain names ``validate_secret_separation``
    # inspects, so a renamed agent-LLM env var is still cross-checked.
    from ops import LEGACY_GEMINI_ENV, TELEGRAM_TOKEN_ENV

    env_subset = {
        TELEGRAM_TOKEN_ENV: env.get(token_env, ""),
        AGENT_LLM_TOKEN_ENV: env.get(config.llm_api_key_env, ""),
        LEGACY_GEMINI_ENV: env.get("LEGACY_GEMINI_API_KEY", ""),
    }
    validate_secret_separation(env_subset)
    return config


def ai_analyst_config(deployment: DeploymentConfig) -> AIAnalystConfig:
    """Derive the immutable run config from the deployment settings."""
    return AIAnalystConfig(
        screener=deployment.screener,
        max_agent_pool_size=deployment.max_agent_pool_size,
    )


# ---------------------------------------------------------------------------
# Live snapshot through the frozen-legacy data path (plan §8 change map)
# ---------------------------------------------------------------------------


def load_live_snapshot(
    deployment: DeploymentConfig,
    config: AIAnalystConfig,
    *,
    period: str | None = None,
) -> AnalysisSnapshot:
    """Build the point-in-time snapshot from real IDX data.

    Downloads go through the frozen-legacy loaders — never through new
    scraping code — so freshness, symbol normalization, and the adjusted
    price basis match the benchmark screener exactly (plan §8). The
    snapshot's completed-candle cutoff then labels every candle ``as_of``;
    the scheduler's stale-data gate uses that against the session.
    """
    legacy = get_legacy_ranking()   # re-exports all screener_core names
    period = period or deployment.data_period

    if deployment.universe_source == "excel":
        pool = legacy.ambil_semua_ticker_dari_excel(deployment.universe_excel_path)
        if not pool:
            raise SnapshotError(
                f"universe file produced no tickers: {deployment.universe_excel_path}"
            )
    else:
        pool = list(deployment.universe_fixed)

    ihsg = legacy.download_ihsg(period)
    if ihsg is None or ihsg.empty:
        raise SnapshotError("IHSG download returned no data")
    frames = legacy.download_saham_batch(pool, period)
    if not frames:
        raise SnapshotError("stock download returned no data for the whole universe")
    return build_snapshot(ihsg, frames, config)


# ---------------------------------------------------------------------------
# Shadow-mode transport (Hermes unconfirmed — decision #16)
# ---------------------------------------------------------------------------


class CacingNagaSmokeTransport:
    """Deterministic shadow transport for deployment smoke runs.

    Produces schema-valid interpretations that **echo the Python facts**
    (conservative readings, LOW/MEDIUM confidence) and always carry
    ``model: "CACINGNAGA_SMOKE_V1"`` in the usage metadata. It never
    invents numbers, news, or flow claims: every reason string quotes the
    evidence id it is derived from, so the evidence-citation validation
    stays meaningful even in shadow mode.
    """

    MODEL_TAG = "CACINGNAGA_SMOKE_V1"

    def __init__(self, *, latency_ms: int = 0) -> None:
        self.latency_ms = latency_ms
        self.calls: list[AgentRequest] = []

    def complete(self, request: AgentRequest) -> AgentResponse:
        self.calls.append(request)
        env = request.envelope
        if request.agent_name == "MarketAgent":
            payload = self._market(env)
        elif request.agent_name == "TechnicalAgent":
            payload = self._technical(env)
        elif request.agent_name == "FlowAgent":
            payload = self._flow(env)
        elif request.agent_name == "DecisionAgent":
            payload = self._decision(env)
        elif request.agent_name == "ChallengeAgent":
            payload = self._challenge(request)
        elif request.agent_name == "DecisionAgentResolution":
            payload = self._resolution(env)
        elif request.agent_name in (
            "TechnicalAgentPeerReview",
            "FlowAgentPeerReview",
        ):
            # Consultation turn: echo the agent's own first-pass reading, so
            # shadow mode exercises the round without inventing a revision.
            payload = self._peer_echo(request)
        else:
            raise ContractViolation(
                f"smoke transport has no handler for {request.agent_name!r}"
            )
        return AgentResponse(
            raw_text=json.dumps(payload, sort_keys=True),
            payload=payload,
            usage={"model": self.MODEL_TAG, "transport": "shadow-smoke"},
            latency_ms=self.latency_ms,
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _first_ref(refs: Mapping[str, Any]) -> str:
        for value in refs.values():
            if isinstance(value, str) and _ID_RE.match(value):
                return value
        return "snapshot"

    def _market(self, env: Mapping[str, Any]) -> dict[str, Any]:
        market = env["market"]
        refs = market["evidence_refs"]
        close_ref = refs.get("close") or self._first_ref(refs)
        return {
            "regime": "NEUTRAL",
            "confidence_band": "LOW",
            "swing_environment": "NEUTRAL",
            "secondary_direction": None,
            "reasons": [
                f"shadow run: regime not interpreted without a live model; "
                f"close fact per {close_ref}"
            ],
            "risk_flags": [
                "CACINGNAGA_SMOKE_V1: no live provider attached; "
                "interpretations echo Python facts"
            ],
            "evidence_refs": [close_ref],
        }

    def _peer_echo(self, request: AgentRequest) -> dict[str, Any]:
        """Consultation turn: confirm the agent's own first-pass reading.

        Shadow mode has nothing to add to its own reading, so it confirms it
        and records that no revision occurred. Returning a *different* reading
        would be inventing interpretation, which shadow mode must never do.
        """
        env = request.envelope
        first = env.get("your_first_reading")
        peers = env.get("peer_readings") or {}
        if not isinstance(first, dict) or not first:
            raise ContractViolation(
                f"{request.agent_name}: consultation turn without a first-pass "
                "reading to confirm"
            )
        payload = {
            key: list(value) if isinstance(value, (list, tuple)) else value
            for key, value in first.items()
        }
        # Note the exchange without changing the reading itself.
        notes = [
            f"CACINGNAGA_SMOKE_V1: consultation confirms the first-pass "
            f"reading; no live model attached"
        ]
        if peers:
            notes.append(
                f"CACINGNAGA_SMOKE_V1: consulted "
                f"{', '.join(sorted(peers))}; peer claims not adjudicated in "
                f"shadow mode"
            )
        # Contract free-text lists, in the order a reading normally carries
        # them. ``first`` comes from ``payload()``, so its sequences are tuples.
        for key in ("reasons", "risks", "evidence", "risk_flags", "concerns"):
            value = payload.get(key)
            if isinstance(value, list):
                payload[key] = value + [notes[0]]
                break
        else:
            raise ContractViolation(
                f"{request.agent_name}: first-pass reading has no free-text "
                "field to record the consultation"
            )
        return payload

    def _technical(self, env: Mapping[str, Any]) -> dict[str, Any]:
        candidate = env["candidates"][0]
        facts = candidate["facts"]
        refs = candidate.get("evidence_refs", {})
        setup = facts.get("setup") or "UNKNOWN"
        price_ref = refs.get("price") or self._first_ref(refs)
        return {
            "ticker": facts["ticker"],
            "trend": "NEUTRAL",
            "setup": setup,
            "momentum": "NEUTRAL",
            "confidence_band": "LOW",
            "reasons": [
                f"shadow run: Python classifier labels {setup} per {price_ref}"
            ],
            "risks": [
                "CACINGNAGA_SMOKE_V1: trend/momentum not interpreted; "
                "policy decides from Python facts"
            ],
            "evidence_refs": [price_ref],
            "missing_facts": [],
        }

    def _flow(self, env: Mapping[str, Any]) -> dict[str, Any]:
        candidate = env["candidates"][0]
        facts = candidate["flow"]
        refs = candidate.get("evidence_refs", {})
        available = tuple(facts.get("available_indicators") or ())
        missing = tuple(facts.get("missing_indicators") or ())
        mfi_ref = refs.get("mfi") or self._first_ref(refs)
        if available:
            flow, strength = "NEUTRAL", "WEAK"
            evidence = (
                f"shadow run: flow indicators available ({', '.join(available)}) "
                f"but not interpreted; MFI fact per {mfi_ref}",
            )
        else:
            flow, strength = "UNKNOWN", "UNKNOWN"
            evidence = (f"shadow run: no flow indicators available per {mfi_ref}",)
        return {
            "ticker": facts["ticker"],
            "flow": flow,
            "strength": strength,
            "confidence_band": "LOW",
            "evidence": list(evidence),
            "risks": ["CACINGNAGA_SMOKE_V1: flow not interpreted by a model"],
            "evidence_refs": [mfi_ref],
            "available_indicators": list(available),
            "missing_indicators": list(missing),
        }

    def _decision(self, env: Mapping[str, Any]) -> dict[str, Any]:
        candidate = env["candidates"][0]
        facts = candidate["facts"]
        refs = candidate.get("evidence_refs", {})
        python_status = env["policy"]["python_status"]
        conflicts = env["conflicts"]
        price_ref = refs.get("price") or self._first_ref(refs)
        conflict_refs = [
            c["evidence_refs"][-1]
            for c in conflicts
            if c.get("evidence_refs")
        ]
        return {
            "ticker": facts["ticker"],
            "proposed_status": python_status,
            "confidence_band": "LOW",
            "status_change_reason": "",
            "reasons": [
                f"shadow run: accepts Python-derived {python_status} per {price_ref}"
            ],
            "concerns": [
                f"shadow run: conflict {c['rule_id']} on record per {ref}"
                for c, ref in zip(conflicts, conflict_refs)
            ]
            or [f"shadow run: no conflict on record per {price_ref}"],
            "evidence_refs": [price_ref],
        }

    def _challenge(self, request: AgentRequest) -> dict[str, Any]:
        env = request.envelope
        agent_name = env["agent_name"]
        rule = env["conflict_rule_id"]
        ref = self._first_ref(env)
        return {
            "agent_name": agent_name,
            "ticker": request.ticker or "",
            "conflict_rule_id": rule,
            "stance": "SUPPORT",
            "missing_data": [],
            "reasons": [
                f"shadow run: {agent_name} maintains its recorded reading "
                f"for {rule} per {ref}"
            ],
            "evidence_refs": [ref],
        }

    def _resolution(self, env: Mapping[str, Any]) -> dict[str, Any]:
        conflict = env["conflict"]
        rule = conflict["rule_id"]
        ticker = conflict["ticker"]
        ref = (
            conflict["evidence_refs"][-1]
            if conflict.get("evidence_refs") else self._first_ref(env)
        )
        return {
            "ticker": ticker,
            "conflict_rule_id": rule,
            "resolution": "CONFIRMED",
            "status_effect": "CAP_AT_WAIT",
            "summary": f"shadow run: conflict {rule} stands for {ticker}",
            "reasons": [f"no live model to overturn the record per {ref}"],
            "evidence_refs": [ref],
        }
