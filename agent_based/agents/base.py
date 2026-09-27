"""Phase 3 — individual analyst agents over the immutable snapshot.

Boundary rules enforced on every turn (plan §7 Phase 3):

1. Agents receive a *subset* of the snapshot (compact envelope) with stable
   evidence ids for every fact — never the full audit payload.
2. They must answer in strict JSON conforming to a per-agent schema; unknown
   keys and FORBIDDEN_AGENT_FIELDS (price/entry/stop/targets/score/rank/
   final_status/ticker) are rejected via the contract ``from_payload`` guards.
3. Reasons must cite evidence ids present in the envelope (item 5).
4. Timeouts/retries are bounded; exhaustion yields a controlled
   ``PARTIAL``/``FAILED`` outcome — never a fabricated recommendation (item 6).
5. Raw response, validated payload, usage, and latency are returned for the
   Phase 2A store (item 7). Log redaction strips anything that looks like a
   credential/token (item 9).

Layout: shared infrastructure lives here; the sibling stub modules
(``market_agent.py`` etc.) re-export their agent class.
"""
from __future__ import annotations

import re
import time
from collections.abc import Mapping as _ABCMapping
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from cacingnaga.canonical import canonical_hash
from cacingnaga.contracts import FORBIDDEN_AGENT_FIELDS
from cacingnaga.errors import ContractViolation
from cacingnaga.transport import (
    SCHEMA_VERSION,
    AgentRequest,
    AgentResponse,
    AgentTransport,
    TransportConfig,
    extract_json_payload,
)

# ---------------------------------------------------------------------------
# Prompt contract (item 4: only the necessary snapshot subset goes out)
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTIONS = """You are {agent_name} inside CacingNagaPRO, an Indonesian \
IDX swing-trading analyst team. You INTERPRET evidence; you never calculate, \
replace, or add numbers.

Hard rules:
1. Answer with a single JSON object matching the given schema exactly. No \
markdown, no commentary outside the JSON.
2. Never include price, entry, stop, target, score, rank, or final-status \
fields. Those are owned by Python.
3. Every claim in "reasons"/"risks" must cite at least one evidence id from \
the envelope (format Type:hash:field). Unsupported claims will be rejected.
4. Missing data stays UNKNOWN/missing: never fabricate evidence, and never \
treat unavailable data as neutral.
5. Tickers are immutable: echo only the ticker you were given."""


def _format_schema(schema: Mapping[str, Any]) -> str:
    lines = [f'  "{name}": {desc}' for name, desc in schema]
    return "{\n" + ",\n".join(lines) + "\n}"


def build_agent_prompt(
    agent_name: str,
    schema: tuple[tuple[str, str], ...],
    envelope: Mapping[str, Any],
    *,
    ticker: str | None = None,
    run_id: str = "",
) -> AgentRequest:
    """Assemble the prompt contract for one agent turn."""
    schema_dict: dict[str, Any] = {"fields": [name for name, _ in schema]}
    user = (
        f"Interpret the evidence below for {ticker or 'the market'}.\n"
        f"Respond with exactly this JSON shape:\n{_format_schema(schema)}\n\n"
        f"EVIDENCE ENVELOPE (immutable facts; cite evidence ids verbatim):\n"
        f"```json\n{json_dumps(envelope)}\n```"
    )
    return AgentRequest(
        agent_name=agent_name,
        ticker=ticker,
        envelope=envelope,
        schema=schema_dict,
        system_instructions=SYSTEM_INSTRUCTIONS.format(agent_name=agent_name),
        user_prompt=user,
        run_id=run_id,
    )


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Validation pipeline (items 2, 3, 5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentResult:
    """One completed, validated agent turn (audit-ready)."""

    agent_name: str
    ticker: str | None
    interpretation: Any                          # validated contract instance
    evidence_refs: tuple[str, ...] = ()
    raw_response: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    schema_version: str = SCHEMA_VERSION
    prompt_hash: str = ""                       # canonical hash of the request

    def payload(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "ticker": self.ticker,
            "interpretation": self.interpretation.payload(),
            "evidence_refs": list(self.evidence_refs),
            "schema_version": self.schema_version,
            "usage": dict(self.usage),
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class AgentOutcome:
    """Terminal state of one agent turn: VALIDATED, PARTIAL, or FAILED.

    ``error``/``attempts`` feed the run-level PARTIAL/FAILED logic (item 6).
    """

    agent_name: str
    ticker: str | None
    status: str                                  # VALIDATED | PARTIAL | FAILED
    result: AgentResult | None = None
    error: str = ""
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status == "VALIDATED"


def _collect_evidence_ids(envelope: Mapping[str, Any]) -> set[str]:
    """Every evidence id present anywhere in the envelope slice.

    Walks the whole slice recursively: agents may cite any id the slice
    legitimately contains (fact refs, reading payloads, conflict evidence),
    and nothing else — slices never carry other candidates' facts.
    """
    ids: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if _CITE.fullmatch(value):
                ids.add(value)
        elif isinstance(value, _ABCMapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(envelope)
    return ids


_CITE = re.compile(r"[A-Za-z]+Facts:[0-9a-f]{12}:[a-zA-Z0-9_]+")


def _payload_id_strings(value: Any) -> tuple[str, ...]:
    """Exact evidence-id-shaped strings anywhere in a payload (refs fields)."""
    found: list[str] = []
    if isinstance(value, str):
        if _CITE.fullmatch(value):
            found.append(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            found.extend(_payload_id_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_payload_id_strings(item))
    return tuple(found)


def _check_evidence_citations(
    texts: tuple[str, ...], allowed: set[str], *, min_citations: int
) -> tuple[str, ...]:
    """Extract evidence-id citations; reject unsupported claims (item 5)."""
    cited: list[str] = []
    for text in texts:
        cited.extend(m.group(0) for m in _CITE.finditer(text))
    if not cited and min_citations > 0:
        raise ContractViolation(
            "agent response cites no evidence ids; unsupported claims are rejected"
        )
    unknown = sorted(set(cited) - allowed)
    if unknown:
        raise ContractViolation(f"agent cites evidence ids absent from the envelope: {unknown}")
    return tuple(dict.fromkeys(cited))          # dedupe, preserve order


_FORBIDDEN_ENUM_KEYS = ("regime", "trend", "setup", "flow", "strength")


def _reject_forbidden_fields(payload: Mapping[str, Any]) -> None:
    """Top-level guard: FORBIDDEN_AGENT_FIELDS can never enter a response."""
    injected = sorted(set(payload) & FORBIDDEN_AGENT_FIELDS)
    if injected:
        raise ContractViolation(f"forbidden agent fields rejected: {injected}")


def validate_payload(
    payload: Mapping[str, Any],
    envelope: Mapping[str, Any],
    builder: Callable[[Mapping[str, Any]], Any],
) -> tuple[Any, tuple[str, ...]]:
    """Validate one response payload against its contract; returns
    (interpretation, evidence_refs).

    Order matters: forbidden-field rejection first, then schema/enum
    validation via the contract ``from_payload`` builder, then evidence
    citation checks against the envelope.
    """
    _reject_forbidden_fields(payload)
    interpretation = builder(payload)
    allowed = _collect_evidence_ids(envelope)
    texts = (
        tuple(getattr(interpretation, "reasons", ()) or ())
        + tuple(getattr(interpretation, "evidence", ()) or ())
    )
    refs = _check_evidence_citations(
        texts + _payload_id_strings(interpretation.payload()), allowed, min_citations=0
    )
    return interpretation, refs


def run_with_retries(
    transport: AgentTransport,
    request: AgentRequest,
    builder: Callable[[Mapping[str, Any]], Any],
    *,
    envelope: Mapping[str, Any],
    config: TransportConfig | None = None,
    allow_empty_citations: bool = False,
) -> AgentOutcome:
    """Run one agent turn with bounded retries; never fabricates a result.

    Validation failure or transport failure consumes an attempt. On
    exhaustion the outcome is PARTIAL/FAILED — the caller (runner) decides
    the run-level state; no local improvised recommendation is returned.
    """
    cfg = config or TransportConfig()
    cfg.validate()
    deadline = time.monotonic() + cfg.timeout_seconds
    last_error = ""
    attempts = 0
    while attempts < cfg.max_attempts:
        attempts += 1
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ContractViolation("agent turn exceeded its timeout budget")
            response: AgentResponse = transport.complete(request)
            payload = (
                response.payload
                if response.payload is not None
                else extract_json_payload(response.raw_text)
            )
            _reject_forbidden_fields(payload)
            interpretation = builder(payload)
            allowed = _collect_evidence_ids(envelope)
            texts = (
                tuple(getattr(interpretation, "reasons", ()) or ())
                + tuple(getattr(interpretation, "evidence", ()) or ())
                + tuple(getattr(interpretation, "risks", ()) or ())
            )
            # Claims in prose AND exact ids in refs fields must both be
            # backed by the envelope (a fabricated evidence id is an
            # unsupported claim about evidence that does not exist).
            checked = texts + _payload_id_strings(interpretation.payload())
            if not allow_empty_citations:
                refs = _check_evidence_citations(checked, allowed, min_citations=1)
            else:
                refs = _check_evidence_citations(checked, allowed, min_citations=0)
            return AgentOutcome(
                agent_name=request.agent_name,
                ticker=request.ticker,
                status="VALIDATED",
                result=AgentResult(
                    agent_name=request.agent_name,
                    ticker=request.ticker,
                    interpretation=interpretation,
                    evidence_refs=refs,
                    raw_response=response.payload_json(),
                    usage=dict(response.usage),
                    latency_ms=response.latency_ms,
                    prompt_hash=canonical_hash(request.payload()),
                ),
                attempts=attempts,
            )
        except Exception as exc:                 # noqa: BLE001 — boundary: any failure is bounded
            last_error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                break
            if attempts < cfg.max_attempts and cfg.retry_backoff_seconds:
                time.sleep(cfg.retry_backoff_seconds)
    return AgentOutcome(
        agent_name=request.agent_name,
        ticker=request.ticker,
        status="FAILED" if attempts >= cfg.max_attempts else "PARTIAL",
        error=last_error or "timeout budget exhausted",
        attempts=attempts,
    )


# ---------------------------------------------------------------------------
# Log redaction (item 9)
# ---------------------------------------------------------------------------

_REDACTIONS = (
    re.compile(r"(?i)\b(sk-[a-z0-9]{8,})\b"),
    re.compile(r"(?i)\b(api[_-]?key[\"\']?\s*[:=]\s*[\"\']?\S+)"),
    re.compile(r"(?i)\b(bearer\s+\S+)"),
)


def redact(text: str) -> str:
    """Strip credential-shaped strings before anything reaches a log."""
    out = text or ""
    for pattern in _REDACTIONS:
        out = pattern.sub("[REDACTED]", out)
    return out
