"""Frozen-legacy adapter (Phase 0 work item 9, plan §3).

Loads ``scripts/screener_ranking.py`` (which chains ``screener_core.py``) by
file path so the new package never mutates the legacy screener. The legacy
modules stay the compatibility surface; this adapter is the only bridge.

Also registers the *full-candidate stage hook* required by Phase 1 work item 7:
the legacy ``ranking_candidates`` returns only the top-three watchlist, so we
wrap it to expose the complete candidate list to the new pipeline while the
legacy behavior remains untouched for backtests/CLI.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parents[2]  # repo root (agent_based/cacingnaga/*)
_SCRIPTS = _ROOT / "scripts"


def load_legacy_ranking() -> ModuleType:
    """Import scripts/screener_ranking.py (and its screener_core base) by path."""
    path = _SCRIPTS / "screener_ranking.py"
    spec = importlib.util.spec_from_file_location("cacingnaga_legacy_ranking", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"unable to load legacy screener at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_LEGACY_CACHE: ModuleType | None = None


def get_legacy_ranking() -> ModuleType:
    """Cached loader: one exec per process (module exec is expensive)."""
    global _LEGACY_CACHE
    if _LEGACY_CACHE is None:
        _LEGACY_CACHE = load_legacy_ranking()
    return _LEGACY_CACHE


def register_full_pool_hook(module: ModuleType) -> None:
    """Patch ``ranking_candidates`` to expose the full pool with a stable order.

    The legacy top-three behavior is preserved for the legacy caller by keeping
    the same signature/return contract; the new pipeline reads ``full_pool``
    attributes it attaches to the result tuple consumers instead.
    """

    original = module.ranking_candidates

    def ranking_candidates_full(candidates: list[dict], limit: int = 3, allow_wait: bool = True):
        # Compute once on the full pool so nothing is silently dropped.
        strong = [c for c in candidates if c.get("status") == module.STATUS_READY]
        watch = [c for c in candidates if c.get("status") == module.STATUS_WAIT]
        key = lambda c: (  # noqa: E731 - same ordering key as legacy
            c.get("quality_score", 0),
            c.get("rs_percentile", 0),
            int(c.get("adx_ok", False)),
            int(c.get("breakout_ok", False)),
            c.get("vol_z", 0) or 0,
            c.get("turnover20", 0) or 0,
        )
        strong = sorted(strong, key=key, reverse=True)
        watch = sorted(watch, key=key, reverse=True)
        # Record the full pool on the module for the new pipeline to consume.
        module.FULL_POOL = {"ready": strong, "wait": watch}
        if strong:
            return strong[:limit], module.STATUS_READY
        if allow_wait:
            return watch[:limit], module.STATUS_WAIT
        return [], module.STATUS_WAIT

    ranking_candidates_full.__name__ = "ranking_candidates"
    module.ranking_candidates = ranking_candidates_full
