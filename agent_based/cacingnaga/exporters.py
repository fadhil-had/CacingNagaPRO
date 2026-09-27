"""Phase 2A — legacy-compatible exporters (plan §7 Phase 2A work item 5).

Render canonical stored artifacts (``RunResult``/decisions/snapshot) to JSON,
CSV, and Markdown. Rendering never changes a decision or numerical value
(plan §5 boundary rules) — these functions only format what Python decided.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .contracts import RunResult
from .errors import ContractViolation

_DECISION_COLUMNS = (
    "ticker", "final_status", "agent_proposed_status", "confidence_band",
    "score", "rank", "risk_plan_ref",
)


def _decision_rows(run: RunResult) -> list[dict[str, Any]]:
    rows = []
    for bucket, decisions in (
        ("RECOMMENDATION", run.recommendations),
        ("WAIT", run.wait),
        ("REJECTED", run.rejected),
    ):
        for d in decisions:
            rows.append(
                {
                    "ticker": d.ticker,
                    "final_status": d.final_status,
                    "agent_proposed_status": d.agent_proposed_status,
                    "confidence_band": d.confidence_band,
                    "score": d.score,
                    "rank": d.rank,
                    "risk_plan_ref": d.risk_plan_ref,
                    "outcome": bucket,
                }
            )
    return rows


def export_run_json(run: RunResult, path: str | Path) -> Path:
    """Full canonical run payload as JSON (audit-grade)."""
    from .canonical import canonical_json

    payload = {
        "run_id": run.run_id,
        "analysis_date": run.analysis_date,
        "as_of": run.as_of,
        "run_status": run.run_status,
        "decision_outcome": run.decision_outcome,
        "market": run.market.payload(),
        "market_facts": run.market_facts.payload(),
        "recommendations": [d.payload() for d in run.recommendations],
        "wait": [d.payload() for d in run.wait],
        "rejected": [d.payload() for d in run.rejected],
        "warnings": list(run.warnings),
        "data_snapshot_hash": run.data_snapshot_hash,
        "config_hash": run.config_hash,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    return path


def export_decisions_csv(run: RunResult, path: str | Path) -> Path:
    """Flat decision table (CSV), one row per evaluated candidate."""
    rows = _decision_rows(run)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_DECISION_COLUMNS + ("outcome",))
        writer.writeheader()
        writer.writerows(rows)
    return path


def export_run_markdown(run: RunResult, path: str | Path) -> Path:
    """Human-readable Markdown report (renderer, not decision-maker)."""
    from .contracts import format_rupiah_like

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        f"### 🧠 CACINGNAGAPRO RUN — {run.analysis_date}",
        "",
        f"- Run: `{run.run_id}`",
        f"- Status: **{run.run_status}** | Outcome: **{run.decision_outcome}**",
        f"- Market (facts): close {format_rupiah_like(run.market_facts.close)} "
        f"@ {run.market_facts.as_of}",
        f"- Snapshot: `{run.data_snapshot_hash[:12]}` | Config: `{run.config_hash[:12]}`",
        "",
    ]
    if run.recommendations:
        lines += ["🏆 Recommendations", ""]
        for d in run.recommendations:
            lines.append(f"{d.rank}. **{d.ticker}** — {d.final_status} "
                         f"(score {d.score:.3f}, {d.confidence_band})")
            for reason in d.reasons:
                lines.append(f"   - {reason}")
            lines.append("")
    else:
        lines += ["🏆 Recommendations", "", "_None — NO TRADE._", ""]
    if run.wait:
        lines += ["⏳ WAIT", ""]
        for d in run.wait:
            gate_note = f" — {d.conflicts[0]}" if d.conflicts else ""
            lines.append(f"- {d.ticker} (score {d.score:.3f}){gate_note}")
        lines.append("")
    if run.rejected:
        lines += ["❌ REJECTED", ""]
        for d in run.rejected:
            gate_note = f" — {d.conflicts[0]}" if d.conflicts else ""
            lines.append(f"- {d.ticker} (score {d.score:.3f}){gate_note}")
        lines.append("")
    if run.warnings:
        lines += ["⚠️ Warnings", ""]
        for warning in run.warnings:
            lines.append(f"- {warning}")
        lines.append("")
    lines.append("_Analysis output, not execution advice; past results do not "
                 "guarantee future performance._")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def export_snapshot_json(snapshot_payload: dict[str, Any], path: str | Path) -> Path:
    """Persist the immutable snapshot payload (pre-agent audit artifact)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def format_decisions_table(run: RunResult) -> str:
    """Compact ASCII table for CLI/Telegram-style rendering (no mutation)."""
    rows = _decision_rows(run)
    if not rows:
        return "(no evaluated candidates)"
    headers = ("ticker", "status", "score", "rank", "outcome")
    lines = [" | ".join(headers), "-" * 46]
    for row in rows:
        lines.append(
            f"{row['ticker']:9.9} | {row['final_status']:6.6} | {row['score']:.3f} | "
            f"{row['rank'] if row['rank'] is not None else '-':>2} | {row['outcome']}"
        )
    return "\n".join(lines)


__all__ = [
    "export_decisions_csv",
    "export_run_json",
    "export_run_markdown",
    "export_snapshot_json",
    "format_decisions_table",
]
