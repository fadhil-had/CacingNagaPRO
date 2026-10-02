"""Phase 3/4 — analyst agents plus the Phase 4 Decision Agent.

Three independent, schema-validated agents over one immutable snapshot:

- :mod:`market_agent`    — regime/momentum/volatility/breadth interpretation
- :mod:`technical_agent` — setup/structure/momentum interpretation per candidate
- :mod:`flow_agent`      — OBV/MFI/CMF/volume interpretation per candidate
- :mod:`decision_agent`  — status proposal over the fixed evidence packet (P4)
- :mod:`merge`           — Python final-decision merge (conflicts, veto) (P4)

All turns go through the provider-neutral :class:`cacingnaga.transport.AgentTransport`
boundary; validation, forbidden-field rejection, and evidence-citation checks
live in :mod:`base` so no transport can bypass them.
"""
from .base import (
    AgentOutcome,
    AgentResult,
    redact,
    run_with_retries,
    validate_payload,
)
from .decision_agent import AGENT_NAME as DECISION_AGENT
from .flow_agent import AGENT_NAME as FLOW_AGENT
from .flow_agent import assert_no_bandar_certainty, build_interpretation as build_flow
from .market_agent import AGENT_NAME as MARKET_AGENT
from .market_agent import assert_market_scope, build_interpretation as build_market
from .merge import AgentDecisionRecord, merge_decision
from .peer_review import (
    FLOW_REVIEW_AGENT,
    PEER_ROUND_VERSION,
    TECHNICAL_REVIEW_AGENT,
    consult_candidate,
)
from .technical_agent import AGENT_NAME as TECHNICAL_AGENT
from .technical_agent import assert_ticker_matches

__all__ = [
    "DECISION_AGENT",
    "FLOW_AGENT",
    "FLOW_REVIEW_AGENT",
    "MARKET_AGENT",
    "PEER_ROUND_VERSION",
    "TECHNICAL_AGENT",
    "TECHNICAL_REVIEW_AGENT",
    "AgentDecisionRecord",
    "AgentOutcome",
    "AgentResult",
    "assert_market_scope",
    "assert_no_bandar_certainty",
    "assert_ticker_matches",
    "build_flow",
    "build_market",
    "consult_candidate",
    "merge_decision",
    "redact",
    "run_with_retries",
    "validate_payload",
]
