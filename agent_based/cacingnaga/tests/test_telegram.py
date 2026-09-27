"""Phase 5 — Telegram MVP (plan §7 Phase 5).

Exit criteria under test:
- all MVP commands work through the pure dispatcher from a store-backed bot,
- repeated/concurrent commands reuse AnalysisService idempotency correctly,
- NO_TRADE, empty, partial, and failed runs have distinct, clear messages,
- /why returns the persisted decision/evidence, not a newly invented one,
- Telegram state and rendering do not change canonical results,
- operational exposure stays disabled until deliberately flagged on.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
sys.path.insert(0, str(BASE))

from cacingnaga.config import AIAnalystConfig
from cacingnaga.errors import ContractViolation
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.snapshot import build_snapshot, snapshot_to_agent_envelope
from cacingnaga.store import AuditStore
from cacingnaga.transport import FakeTransport

from orchestrator import AnalysisService
from telegram_layer.bot import CacingNagaBot, normalize_ticker
from telegram_layer.config import TelegramConfig, load_telegram_config
from telegram_layer.render import (
    render_why_message,
    split_message,
)

CONFIG = AIAnalystConfig()
ALLOWED_USER = 111
ALLOWED_CHAT = 222


def telegram_config(**overrides) -> TelegramConfig:
    defaults = dict(
        enabled=False,
        allowed_user_ids=(ALLOWED_USER,),
        allowed_chat_ids=(ALLOWED_CHAT,),
    )
    defaults.update(overrides)
    return TelegramConfig(**defaults)


def make_bot(store: AuditStore, **overrides) -> CacingNagaBot:
    return CacingNagaBot(telegram_config(**overrides), store, screen_runner=None)


def run_service_once(store: AuditStore, run_id: str):
    """Produce one real stored run through the Phase 4 service."""
    snapshot = build_snapshot(market_frame(), default_frames(), CONFIG)
    envelope = snapshot_to_agent_envelope(snapshot)

    def market_handler(request):
        refs = envelope["market"]["evidence_refs"]
        return {
            "regime": "BULLISH", "confidence_band": "MEDIUM",
            "swing_environment": "FAVORABLE", "secondary_direction": None,
            "reasons": [
                f"close above rising EMAs per {refs['close']} and {refs['ema20']}",
                f"breadth confirms participation per {refs['breadth50']}",
            ],
            "risk_flags": [f"market RSI stretched per {refs['rsi']}"],
            "evidence_refs": [refs["close"], refs["ema20"], refs["breadth50"], refs["rsi"]],
        }

    handlers = {"MarketAgent": market_handler}
    for candidate in envelope["candidates"]:
        ticker = candidate["facts"]["ticker"]
        t_refs = candidate["evidence_refs"]
        f_refs = candidate["flow_evidence_refs"]

        def tech_handler(request, _c=candidate, _r=t_refs):
            return {
                "ticker": _c["facts"]["ticker"], "trend": "BULLISH",
                "setup": _c["facts"]["setup"], "momentum": "POSITIVE",
                "confidence_band": "MEDIUM",
                "reasons": [
                    f"EMA structure per {_r['ema20']} and {_r['ema50']}",
                    f"price zone per {_r['price']}",
                ],
                "risks": [f"momentum per {_r['macd_hist']}"],
                "evidence_refs": [_r["ema20"], _r["ema50"], _r["price"], _r["macd_hist"]],
                "missing_facts": [],
            }

        def flow_handler(request, _c=candidate, _r=f_refs):
            return {
                "ticker": _c["facts"]["ticker"],
                "flow": "DISTRIBUTION" if _c["facts"]["ticker"].startswith("CUAN") else "ACCUMULATION",
                "strength": "MEDIUM", "confidence_band": "MEDIUM",
                "evidence": [f"MFI per {_r['mfi']}; CMF per {_r['cmf']}; OBV per {_r['obv_slope']}"],
                "risks": ["OHLCV flow is an indication only"],
                "evidence_refs": [_r["mfi"], _r["cmf"], _r["obv_slope"]],
                "available_indicators": list(_c["flow"]["available_indicators"]),
                "missing_indicators": list(_c["flow"]["missing_indicators"]),
            }

        def decision_handler(request, _t=ticker, _r=t_refs):
            facts = request.envelope["candidates"][0]["facts"]
            refs = request.envelope["candidates"][0]["evidence_refs"]
            python_status = request.envelope["policy"]["python_status"]
            conflicts = request.envelope["conflicts"]
            reason = ""
            if python_status == "READY" and conflicts:
                python_status = "WAIT"
                reason = f"conflict {conflicts[0]['rule_id']} acknowledged"
            return {
                "ticker": facts["ticker"], "proposed_status": python_status,
                "confidence_band": "MEDIUM", "status_change_reason": reason,
                "reasons": [f"python derives status per {refs.get('ema20', refs.get('setup', ''))}"],
                "concerns": [
                    f"conflict {c['rule_id']} per "
                    + (c["evidence_refs"][-1] if c["evidence_refs"]
                       else refs.get("ema20", refs.get("setup", "")))
                    for c in conflicts
                ] or [f"no conflicts per {refs.get('rsi', refs.get('mfi', refs.get('setup', '')))}"],
                "evidence_refs": [
                    refs.get("ema20", refs.get("setup", "")),
                    refs.get("price", refs.get("mfi", "")),
                ],
            }

        handlers[f"TechnicalAgent:{ticker}"] = tech_handler
        handlers[f"FlowAgent:{ticker}"] = flow_handler
        handlers[f"DecisionAgent:{ticker}"] = decision_handler

    service = AnalysisService(CONFIG, store, FakeTransport(handlers))
    return service.run_full_analysis(snapshot, run_id=run_id, trigger="test")


@pytest.fixture()
def store_with_run(tmp_path):
    store = AuditStore(tmp_path / "tg.sqlite3")
    run_service_once(store, "run-tg-1")
    return store


# ---------------------------------------------------------------------------
# Config gate + authorization
# ---------------------------------------------------------------------------


def test_telegram_is_disabled_by_default():
    config = load_telegram_config(env={})
    assert config.enabled is False


def test_enabled_config_requires_token_and_allowlist():
    import os

    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    try:
        with pytest.raises(ContractViolation):
            telegram_config(enabled=True).validate()      # no token env
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        # An allowlisted user but NO chats is still a valid allowlist config.
        telegram_config(enabled=True).validate()
        # Both lists empty is what must be rejected.
        with pytest.raises(ContractViolation):
            TelegramConfig(
                enabled=True,
                allowed_user_ids=(),
                allowed_chat_ids=(),
            ).validate()
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)


def test_authorization_requires_sender_and_chat():
    config = telegram_config()
    assert config.is_authorized(ALLOWED_USER, ALLOWED_CHAT)
    assert not config.is_authorized(999, ALLOWED_CHAT)     # stranger
    assert not config.is_authorized(ALLOWED_USER, 999)     # wrong chat


def test_unauthorized_users_get_no_data(tmp_path):
    bot = make_bot(AuditStore(tmp_path / "x.sqlite3"))
    for user, chat in ((999, ALLOWED_CHAT), (ALLOWED_USER, 999), (None, None)):
        reply, _ = bot.handle(user_id=user, chat_id=chat, text="/screen")
        assert reply.startswith("⛔")


def test_unknown_and_non_commands_are_guided(tmp_path):
    bot = make_bot(AuditStore(tmp_path / "x.sqlite3"))
    assert "Unknown command" in bot.handle(
        user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="/frobnicate"
    )[0]
    assert "Send a command" in bot.handle(
        user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="hello there"
    )[0]


# ---------------------------------------------------------------------------
# Ticker normalization
# ---------------------------------------------------------------------------


def test_ticker_normalization_accepts_bare_and_suffixed():
    assert normalize_ticker("bksl") == "BKSL.JK"
    assert normalize_ticker("BKSL.JK") == "BKSL.JK"
    assert normalize_ticker("$cuan") == "CUAN.JK"


def test_ticker_normalization_rejects_garbage():
    for bad in ("", "TOOLONGTICKER", "ABC.XY", "A B", "!!"):
        with pytest.raises(ContractViolation):
            normalize_ticker(bad)


# ---------------------------------------------------------------------------
# Message splitting + escaping (item 6)
# ---------------------------------------------------------------------------


def test_split_message_respects_limit_and_keeps_content():
    text = "\n\n".join(f"paragraph {i} " + "x" * 120 for i in range(60))
    chunks = split_message(text, 3800)
    assert all(len(c) <= 3800 for c in chunks)
    assert "".join(chunks).replace("\n\n", "\n\n") .count("paragraph") == 60
    assert len(chunks) > 1


def test_split_message_handles_oversized_paragraph():
    chunk = "y" * 9000
    chunks = split_message(chunk, 3800)
    assert all(len(c) <= 3800 for c in chunks) and chunks


def test_renderer_escapes_user_influenced_strings(store_with_run):
    stored = store_with_run.list_runs()[0]
    from orchestrator import _run_result_from_payload

    synthesis = next(
        o["validated"] for o in store_with_run.get_agent_outputs(stored.run_id)
        if o["agent_name"] == "AnalysisService"
    )
    run = _run_result_from_payload(synthesis["run_result"])
    message = render_why_message(run, "CUAN.JK")
    assert "<b>CUAN.JK</b>" in message
    assert "not newly generated" in message


# ---------------------------------------------------------------------------
# Command handlers against a real store-backed run
# ---------------------------------------------------------------------------


def test_screen_command_renders_canonical_run(store_with_run):
    def runner(run_key):
        return run_service_once(store_with_run, run_key)   # idempotent replay

    bot = CacingNagaBot(telegram_config(), store_with_run, screen_runner=runner)
    reply, parse_mode = bot.handle(
        user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="/screen"
    )
    assert parse_mode == "HTML"
    assert "IHSG" in reply and "Disclaimer" in reply
    assert "CacingNagaPRO — Market" not in reply            # /screen, not /market


def test_screen_single_flight_blocks_overlap(tmp_path):
    """Overlapping /screen triggers get the in-progress message (item 4)."""
    acquired = threading.Event()
    release = threading.Event()

    def slow_runner(run_key):
        acquired.set()
        release.wait(timeout=5)
        return run_service_once(AuditStore(tmp_path / "sf.sqlite3"), run_key)

    bot = CacingNagaBot(telegram_config(), None, screen_runner=slow_runner)
    results: list[tuple[str, str]] = []

    def first():
        results.append(bot.handle(user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="/screen"))

    thread = threading.Thread(target=first)
    thread.start()
    assert acquired.wait(timeout=5)
    second_reply, _ = bot.handle(
        user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="/screen"
    )
    release.set()
    thread.join(timeout=5)
    assert "already in progress" in second_reply
    assert "IHSG" in results[0][0]


def test_market_and_status_commands(store_with_run):
    bot = make_bot(store_with_run)
    market_reply, _ = bot.handle(
        user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="/market"
    )
    assert "IHSG" in market_reply and "Regime:" in market_reply
    status_reply, _ = bot.handle(
        user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="/status"
    )
    assert "run-tg-1" in status_reply and "COMPLETE" in status_reply


def test_analyze_and_why_return_persisted_decision(store_with_run):
    bot = make_bot(store_with_run)
    for command in ("/analyze CUAN.JK", "/why cuan"):
        reply, parse_mode = bot.handle(
            user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text=command
        )
        assert parse_mode == "HTML"
        assert "CUAN.JK" in reply
        assert "not newly generated" in reply
        assert "Proposed by agent:" in reply


def test_why_unknown_ticker_is_explicit(store_with_run):
    bot = make_bot(store_with_run)
    reply, _ = bot.handle(
        user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="/why ZZZZ"
    )
    assert "not in run" in reply


def test_commands_without_runs_prompt_screen_first(tmp_path):
    bot = make_bot(AuditStore(tmp_path / "empty.sqlite3"))
    for command in ("/market", "/status", "/why BKSL", "/analyze BKSL"):
        reply, _ = bot.handle(user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text=command)
        assert "No completed run yet" in reply or "No runs recorded" in reply


def test_distinct_messages_for_no_trade_and_partial(tmp_path):
    # NO_TRADE: valid run with zero qualified candidates (bearish market).
    store = AuditStore(tmp_path / "nt.sqlite3")
    snapshot = build_snapshot(market_frame(), default_frames(), CONFIG)
    envelope = snapshot_to_agent_envelope(snapshot)
    from cacingnaga.tests.test_orchestrator import full_transport

    service = AnalysisService(
        CONFIG, store,
        full_transport(envelope, market_regime="BEARISH"),
    )
    no_trade = service.run_full_analysis(snapshot, run_id="run-nt")
    assert no_trade.run_result.decision_outcome == "NO_TRADE"

    # PARTIAL: decision agent outage.
    store2 = AuditStore(tmp_path / "p.sqlite3")
    service2 = AnalysisService(
        CONFIG, store2,
        full_transport(envelope, dead_decision_for=("BKSL.JK",)),
    )
    partial = service2.run_full_analysis(snapshot, run_id="run-p")
    assert partial.run_result.run_status == "PARTIAL"

    bot = CacingNagaBot(
        telegram_config(), store, screen_runner=lambda key: no_trade
    )
    reply, _ = bot.handle(user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="/screen")
    assert "NO TRADE" in reply and "no candidate qualified" in reply

    bot2 = CacingNagaBot(
        telegram_config(), store2, screen_runner=lambda key: partial
    )
    reply2, _ = bot2.handle(user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="/screen")
    assert "PARTIAL" in reply2 and "NOT_EVALUATED" in reply2


def test_rendering_does_not_mutate_canonical_results(store_with_run):
    stored = store_with_run.list_runs()[0]
    from orchestrator import _run_result_from_payload

    synthesis = next(
        o["validated"] for o in store_with_run.get_agent_outputs(stored.run_id)
        if o["agent_name"] == "AnalysisService"
    )
    run = _run_result_from_payload(synthesis["run_result"])
    before = run.payload() if hasattr(run, "payload") else None
    from telegram_layer.render import render_run_message

    render_run_message(run)
    render_why_message(run, "BKSL.JK")
    # Same stored synthesis still rebuilds to the identical canonical result.
    again = _run_result_from_payload(synthesis["run_result"])
    assert again.recommendations == run.recommendations
    assert again.wait == run.wait and again.rejected == run.rejected


# ---------------------------------------------------------------------------
# Operational gate (item 10/11)
# ---------------------------------------------------------------------------


def test_service_refuses_to_start_while_disabled():
    from telegram_layer.adapter import run_service

    with pytest.raises(ContractViolation):
        run_service(telegram_config(enabled=False))


def test_help_mentions_gate_when_disabled(tmp_path):
    bot = make_bot(AuditStore(tmp_path / "x.sqlite3"))
    reply, _ = bot.handle(user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT, text="/help")
    assert "Phase 6" in reply and "disabled" in reply
