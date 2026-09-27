"""Shared runtime exceptions for the CacingNagaPRO pipeline."""
from __future__ import annotations


class CacingNagaError(Exception):
    """Base class for all pipeline errors."""


class ContractViolation(CacingNagaError):
    """Raised when a schema validation or boundary rule fails."""


class SnapshotError(CacingNagaError):
    """Raised when the immutable analysis snapshot cannot be produced/validated."""
