"""Phase 5 — canonical Telegram rendering (plan §7 Phase 5, items 5–9).

The renderer displays the **stored canonical run result**; it never mutates
decisions, never parses previous messages, and never re-runs an LLM. All
user-controlled strings are HTML-escaped (Telegram ``parse_mode=HTML``),
messages are split safely below the 4096-char limit, and group-visible text
carries no raw traces, prompts, or secrets (redaction + no-prompt rule).
"""
from __future__ import annotations

import html
from typing import Any

from agents.base import redact
from cacingnaga.contracts import RunResult, format_rupiah_like


def escape(text: Any) -> str:
    """HTML-escape anything that may contain user-controlled content."""
    return html.escape(str(text if text is not None else ""), quote=False)


def split_message(text: str, limit: int) -> list[str]:
    """Split on paragraph boundaries under ``limit`` without orphan tags.

    Falls back to hard character cuts when a single paragraph exceeds the
    limit; every chunk is guaranteed non-empty and within ``limit``.
    """
    if len(text) <= limit:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:            # oversized paragraph
            cut = paragraph.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(paragraph[:cut])
            paragraph = paragraph[cut:].lstrip("\n")
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _market_section(run: RunResult) -> list[str]:
    facts = run.market_facts
    market = run.market
    lines = [
        "<b>IHSG</b> " + escape(facts.as_of),
        f"Close: {format_rupiah_like(facts.close)} | RSI: "
        f"{facts.rsi if facts.rsi is not None else '-'}",
        f"Regime: {escape(market.regime)} ({escape(market.confidence_band)}) | "
        f"Swing env: {escape(market.swing_environment)}",
    ]
    if not facts.breadth_available:
        lines.append("Breadth: data unavailable (UNKNOWN, not neutral)")
    else:
        lines.append(f"Breadth&gt;50: {facts.breadth50 if facts.breadth50 is not None else '-'}")
    return lines


def _recommendation_section(run: RunResult) -> list[str]:
    if run.decision_outcome == "NO_TRADE" and not run.recommendations:
        return [
            "<b>NO TRADE</b> — no candidate qualified today. "
            "Wait for a better structure; do not force entries."
        ]
    lines = ["<b>READY (recommendations)</b>"]
    for decision in run.recommendations:
        lines.append(
            f"#{decision.rank} <b>{escape(decision.ticker)}</b> "
            f"score {decision.score:.2f} ({escape(decision.confidence_band)})"
        )
    return lines


def _wait_reject_section(run: RunResult) -> list[str]:
    lines: list[str] = []
    if run.wait:
        lines.append("<b>WAIT (watchlist)</b>")
        lines.extend(
            f"• {escape(d.ticker)} — {escape((d.reasons or ('-',))[-1][:120])}"
            for d in run.wait[:10]
        )
    if run.rejected:
        lines.append(f"<b>REJECT</b> ({len(run.rejected)} candidates)")
        for d in run.rejected[:10]:
            first_reason = (d.reasons or ("-",))[0]
            lines.append(f"• {escape(d.ticker)} — {escape(first_reason[:100])}")
    return lines


def _risk_section(run: RunResult) -> list[str]:
    """Risk levels live on the stored decisions only as refs; the canonical
    numbers come from the stored synthesis payload when present."""
    lines: list[str] = []
    for decision in run.recommendations:
        lines.append(
            f"<b>{escape(decision.ticker)}</b> plan: {escape(decision.risk_plan_ref[:44])}"
            + (f" …" if len(decision.risk_plan_ref) > 44 else "")
        )
    if run.warnings:
        lines.append("Data warnings: " + escape("; ".join(run.warnings[:3])))
    return lines


def render_run_message(run: RunResult) -> str:
    """Canonical single-message render of a stored run result."""
    sections = [
        "\n\n".join(_market_section(run)),
        "\n".join(_recommendation_section(run)),
    ]
    wait_reject = "\n".join(_wait_reject_section(run))
    if wait_reject:
        sections.append(wait_reject)
    risk = "\n".join(_risk_section(run))
    if risk:
        sections.append(risk)
    sections.append(
        "Status: " + escape(run.run_status) + " / " + escape(run.decision_outcome)
        + f"\nRun: {escape(run.run_id)} | snapshot {escape(run.data_snapshot_hash[:12])}"
    )
    sections.append(
        "<i>Disclaimer: analysis output only — not execution advice and not a "
        "guarantee of profit. Trade at your own risk.</i>"
    )
    return "\n\n".join(section for section in sections if section)


def render_market_message(run: RunResult) -> str:
    """`/market` view: the market section plus regime interpretation only."""
    head = "<b>CacingNagaPRO — Market</b>\n\n"
    return head + "\n".join(_market_section(run.market_facts and run) or [])


def render_why_message(run: RunResult, ticker: str) -> str:
    """`/why` view: the persisted decision/evidence for one ticker.

    Reads ONLY the stored ``RunResult`` (exit criterion: not a newly invented
    explanation). Unknown tickers produce an explicit not-found message.
    """
    from agents.base import redact

    needle = ticker.strip().upper()
    for bucket, label in (
        (run.recommendations, "READY"),
        (run.wait, "WAIT"),
        (run.rejected, "REJECT"),
    ):
        for decision in bucket:
            if decision.ticker.upper() != needle:
                continue
            lines = [
                f"<b>{escape(decision.ticker)}</b> — {label} "
                f"(score {decision.score:.2f}, {escape(decision.confidence_band)})"
                + (f" rank {decision.rank}" if decision.rank else ""),
            ]
            if decision.risk_plan_ref:
                lines.append("Risk plan: " + escape(decision.risk_plan_ref))
            if decision.reasons:
                lines.append("<b>Reasons</b>")
                lines.extend("• " + escape(redact(r)) for r in decision.reasons[:6])
            if decision.risks:
                lines.append("<b>Risks</b>")
                lines.extend("• " + escape(redact(r)) for r in decision.risks[:4])
            if decision.conflicts:
                lines.append("<b>Conflicts</b>")
                lines.extend("• " + escape(redact(c)) for c in decision.conflicts[:4])
            lines.append(
                f"Proposed by agent: {escape(decision.agent_proposed_status)} | "
                f"final: {escape(decision.final_status)}"
            )
            lines.append(
                "<i>Explanation rendered from the stored run — not newly generated.</i>"
            )
            return "\n".join(lines)
    return (
        f"{escape(ticker)} is not in run {escape(run.run_id)} "
        "(evaluated candidates only)."
    )


def render_status_message(runs: list[Any]) -> str:
    """`/status` view: recent runs from the store (no re-execution)."""
    if not runs:
        return "No runs recorded yet."
    lines = ["<b>Recent runs</b>"]
    for stored in runs[:8]:
        lines.append(
            f"• {escape(stored.run_id)} — {escape(stored.run_status)} / "
            f"{escape(stored.decision_outcome)} @ {escape(stored.analysis_date)}"
        )
    return "\n".join(lines)


def render_help_message(enabled: bool) -> str:
    lines = [
        "<b>CacingNagaPRO commands</b>",
        "/screen — run today's analysis (single-flight)",
        "/analyze TICKER — deep view for one ticker",
        "/market — IHSG regime snapshot",
        "/status — recent runs",
        "/why TICKER — persisted reasoning for a decision",
    ]
    if not enabled:
        lines.append(
            "\n⚠️ Operational exposure is disabled until the Phase 6 challenge "
            "contract passes (feature flag off)."
        )
    lines.append(
        "<i>Analysis only — not execution advice, no guaranteed profit.</i>"
    )
    return "\n".join(lines)
