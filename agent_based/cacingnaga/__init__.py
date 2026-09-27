"""CacingNagaPRO AI Analyst Team — deterministic-first analysis package.

Implements the Phase 0/1 foundation of the improvement_v3 implementation plan:
versioned contracts, configuration, canonical hashing, a frozen-legacy adapter,
and a versioned deterministic analysis snapshot (market + candidate pool).

Python calculates. Agents interpret. Decision Agent challenges. Backtest judges.
"""
from .canonical import canonical_hash, canonical_json
from .config import AIAnalystConfig, ScreenerConfig, config_payload, load_config
from .errors import CacingNagaError, ContractViolation, SnapshotError
from .versioning import PIPELINE_VERSION, SCHEMA_VERSION

__all__ = [
    "AIAnalystConfig",
    "CacingNagaError",
    "ContractViolation",
    "PIPELINE_VERSION",
    "SCHEMA_VERSION",
    "ScreenerConfig",
    "SnapshotError",
    "canonical_hash",
    "canonical_json",
    "config_payload",
    "load_config",
]
