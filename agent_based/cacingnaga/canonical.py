"""Canonical JSON encoding and hashing helpers (Phase 0, work item 7).

Every stored value or agent-adjacent value can be checked against the input
snapshot through these helpers. All JSON numbers must be finite; NaN/inf are
rejected instead of being silently serialized as non-standard tokens.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .errors import ContractViolation


def canonicalize(value: Any) -> Any:
    """Return a JSON-canonical deep copy of ``value``.

    * dicts get sorted keys; tuples/sets become lists; NaN/inf raise.
    """
    if isinstance(value, dict):
        return {str(k): canonicalize(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [canonicalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractViolation(f"non-finite number cannot be canonicalized: {value!r}")
        return value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise ContractViolation(f"unsupported type for canonical JSON: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize ``value`` to a deterministic JSON string (sorted keys)."""
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    """SHA-256 over the canonical JSON form; the canonical snapshot identity."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
