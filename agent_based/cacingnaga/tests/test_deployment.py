"""Deployment layer and entrypoint tests (plan §8 code map + §10).

Exit criteria under test:
- the externalized TOML config loads, validates, refuses schema drift and
  shared secrets, and derives a valid AIAnalystConfig,
- the smoke transport (CACINGNAGA_SMOKE_V1) produces payloads that pass the
  real agent validation pipeline and complete a real orchestration run,
- a smoke run is visibly tagged shadow in the audit trail (usage model tag),
- the ``scripts/ai_team.py`` commands run/health behave per contract:
  gates respected, safe skips exit 0, degraded runs exit 2, bad config
  exits 1 without starting anything.

No network, no live LLM: the entrypoint's shadow transport keeps every
scenario deterministic.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
ROOT = BASE.parent
sys.path.insert(0, str(BASE))

from cacingnaga.config import AIAnalystConfig
from cacingnaga.errors import ContractViolation
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.snapshot import build_snapshot, snapshot_to_agent_envelope
from cacingnaga.store import AuditStore

import deployment
from deployment import (
    CacingNagaSmokeTransport,
    DEPLOYMENT_CONFIG_VERSION,
    ai_analyst_config,
    load_deployment_config,
)
from orchestrator import AnalysisService

CONFIG = AIAnalystConfig()
SESSION = "2026-09-18"
AFTER_CLOSE = datetime(2026, 9, 18, 19, 0)


# ---------------------------------------------------------------------------
# Deployment config (plan §10)
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ai_team.toml"
    path.write_text(text, encoding="utf-8")
    return path


FULL_CONFIG = """
[meta]
version = "DEPLOYMENT_CONFIG_1"

[screener]
min_turnover_idr = 1_000_000_000.0
min_price_idr = 100.0
max_atr_pct = 0.04
min_history_bars = 260

[policy]
max_agent_pool_size = 30

[scheduler]
close_hour = 16
close_minute = 0
earliest_run_after_close_minutes = 0
look_back_days = 5
extra_holidays = ["2026-12-31"]

[scheduler.universe]
source = "fixed"
tickers = ["BBCA.JK", "BBRI.JK"]
period = "2y"

[ops]
failure_threshold = 3
cooldown_seconds = 60.0
half_open_probes = 1

[telegram]
enabled = false
bot_token_env = "TELEGRAM_BOT_TOKEN"
allowed_user_ids = [1]
allowed_chat_ids = [2]

[storage]
store_path = "output/audit_store.sqlite3"
"""


def test_full_config_loads_and_validates(tmp_path):
    config = load_deployment_config(
        _write_config(tmp_path, FULL_CONFIG), env={}
    )
    assert config.version == DEPLOYMENT_CONFIG_VERSION
    assert config.universe_source == "fixed"
    assert config.universe_fixed == ("BBCA.JK", "BBRI.JK")
    assert "2026-12-31" in config.scheduler.calendar.extra_holidays
    assert not config.telegram_enabled          # feature flag respected
    assert config.scheduler.close_hour == 16
    # Derives a valid run config with the deployed screener thresholds.
    derived = ai_analyst_config(config)
    assert isinstance(derived, AIAnalystConfig)
    derived.validate()


def test_shipped_repo_config_is_valid(tmp_path):
    """The committed config/ai_team.toml must always pass startup gates."""
    shipped = ROOT / "config" / "ai_team.toml"
    if not shipped.exists():
        pytest.skip("config/ai_team.toml not present")
    config = load_deployment_config(shipped, env={})
    ai_analyst_config(config).validate()


def test_config_rejects_unknown_version(tmp_path):
    bad = FULL_CONFIG.replace("DEPLOYMENT_CONFIG_1", "DEPLOYMENT_CONFIG_99")
    with pytest.raises(ContractViolation, match="unsupported deployment config"):
        load_deployment_config(_write_config(tmp_path, bad), env={})


def test_config_rejects_bad_toml(tmp_path):
    path = _write_config(tmp_path, "[meta\nversion = 1")
    with pytest.raises(ContractViolation, match="not valid TOML"):
        load_deployment_config(path, env={})


def test_config_rejects_missing_file(tmp_path):
    with pytest.raises(ContractViolation, match="not found"):
        load_deployment_config(tmp_path / "absent.toml", env={})


def test_config_rejects_invalid_universe_and_period(tmp_path):
    bad = FULL_CONFIG.replace('source = "fixed"\ntickers = ["BBCA.JK", "BBRI.JK"]',
                              'source = "fixed"')
    with pytest.raises(ContractViolation, match="fixed.*requires|tickers"):
        load_deployment_config(_write_config(tmp_path, bad), env={})
    bad2 = FULL_CONFIG.replace('period = "2y"', 'period = "3y"')
    with pytest.raises(ContractViolation, match="period"):
        load_deployment_config(_write_config(tmp_path, bad2), env={})


def test_config_rejects_shared_secrets_at_startup(tmp_path):
    path = _write_config(tmp_path, FULL_CONFIG)
    env = {
        "TELEGRAM_BOT_TOKEN": "shared",
        "AGENT_LLM_API_KEY": "shared",
    }
    with pytest.raises(ContractViolation, match="secret separation"):
        load_deployment_config(path, env=env)


def test_config_payload_never_carries_secret_values(tmp_path):
    config = load_deployment_config(_write_config(tmp_path, FULL_CONFIG), env={})
    payload = json.dumps(config.payload())
    assert "bot_token" not in payload or "TELEGRAM_BOT_TOKEN" in payload
    assert config.telegram_bot_token_env == "TELEGRAM_BOT_TOKEN"  # name only


# ---------------------------------------------------------------------------
# Smoke transport: shadow interpretations through the real pipeline
# ---------------------------------------------------------------------------


def _snapshot():
    return build_snapshot(market_frame(), default_frames(), CONFIG)


def test_smoke_transport_tagged_and_deterministic():
    snapshot = _snapshot()
    env = snapshot_to_agent_envelope(snapshot)
    transport = CacingNagaSmokeTransport()
    from agents.market_agent import build_request as market_request

    request = market_request(env, run_id="smoke-1")
    first = transport.complete(request)
    second = transport.complete(market_request(env, run_id="smoke-1"))
    assert first.usage["model"] == "CACINGNAGA_SMOKE_V1"
    assert first.payload == second.payload      # deterministic


def test_smoke_run_passes_real_pipeline_validation(tmp_path):
    """Shadow payloads survive the full validation + orchestration path."""
    snapshot = _snapshot()
    store = AuditStore(tmp_path / "deploy.sqlite3")
    service = AnalysisService(CONFIG, store, CacingNagaSmokeTransport())
    result = service.run_full_analysis(snapshot, run_id="smoke-run")
    run = result.run_result
    assert run.run_status == "COMPLETE"
    run.validate()
    # Python still owns everything; shadow echoes cannot become numbers.
    assert len(run.recommendations) <= 3
    assert [d.rank for d in run.recommendations] == \
        list(range(1, len(run.recommendations) + 1))


def test_smoke_run_replays_with_zero_calls(tmp_path):
    snapshot = _snapshot()
    store = AuditStore(tmp_path / "deploy.sqlite3")
    transport = CacingNagaSmokeTransport()
    service = AnalysisService(CONFIG, store, transport)
    first = service.run_full_analysis(snapshot, run_id="smoke-replay")
    calls = len(transport.calls)
    assert calls > 0

    replay_transport = CacingNagaSmokeTransport()
    replay_service = AnalysisService(CONFIG, store, replay_transport)
    second = replay_service.run_full_analysis(snapshot, run_id="smoke-replay-2")
    assert second.replayed is True
    assert len(replay_transport.calls) == 0
    assert first.payload()["run_result"] == second.payload()["run_result"]


def test_load_live_snapshot_uses_reexported_legacy_names(monkeypatch):
    """Regression (live smoke 2026-09-28): the legacy download functions are
    re-exported on the ranking module itself — there is no ``screener_core``
    attribute. The deployment layer must call them there."""
    from cacingnaga.legacy_adapter import get_legacy_ranking

    legacy = get_legacy_ranking()
    ihsg, frames = market_frame(), default_frames()
    seen: list[tuple] = []

    def fake_ihsg(period):
        seen.append(("ihsg", period))
        return ihsg

    def fake_batch(pool, period):
        seen.append(("batch", tuple(pool), period))
        return dict(frames)

    monkeypatch.setattr(legacy, "download_ihsg", fake_ihsg)
    monkeypatch.setattr(legacy, "download_saham_batch", fake_batch)

    deploy = deployment.DeploymentConfig(
        universe_source="fixed",
        universe_fixed=tuple(default_frames()),
        data_period="2y",
    )
    deploy.validate()
    snapshot = deployment.load_live_snapshot(deploy, CONFIG)
    assert snapshot.as_of == "2026-09-18"
    assert snapshot.eligible_count == 4
    assert ("ihsg", "2y") in seen
    assert ("batch", tuple(default_frames()), "2y") in seen


def test_load_live_snapshot_refuses_empty_downloads(monkeypatch):
    """A dead provider must fail closed (SnapshotError), not improvise."""
    from cacingnaga.errors import SnapshotError
    from cacingnaga.legacy_adapter import get_legacy_ranking

    legacy = get_legacy_ranking()
    monkeypatch.setattr(legacy, "download_ihsg", lambda period: None)
    deploy = deployment.DeploymentConfig(
        universe_source="fixed",
        universe_fixed=("BBCA.JK",),
        data_period="2y",
    )
    with pytest.raises(SnapshotError, match="IHSG download returned no data"):
        deployment.load_live_snapshot(deploy, CONFIG)


def test_load_live_snapshot_refuses_empty_universe(monkeypatch):
    from cacingnaga.errors import SnapshotError
    from cacingnaga.legacy_adapter import get_legacy_ranking

    legacy = get_legacy_ranking()
    monkeypatch.setattr(legacy, "download_ihsg", lambda period: market_frame())
    monkeypatch.setattr(legacy, "download_saham_batch", lambda pool, period: {})
    deploy = deployment.DeploymentConfig(
        universe_source="fixed",
        universe_fixed=("BBCA.JK",),
        data_period="2y",
    )
    with pytest.raises(SnapshotError, match="no data for the whole universe"):
        deployment.load_live_snapshot(deploy, CONFIG)


def test_smoke_agent_outputs_carry_model_tag(tmp_path):
    snapshot = _snapshot()
    store = AuditStore(tmp_path / "deploy.sqlite3")
    service = AnalysisService(CONFIG, store, CacingNagaSmokeTransport())
    service.run_full_analysis(snapshot, run_id="smoke-tag")
    outputs = store.get_agent_outputs("smoke-tag")
    agent_outputs = [o for o in outputs if o["agent_name"] != "AnalysisService"]
    assert agent_outputs
    for output in agent_outputs:
        usage = json.loads(output["usage_json"]) if isinstance(
            output.get("usage_json"), str
        ) else output.get("usage", {})
        assert usage.get("model") == "CACINGNAGA_SMOKE_V1"


# ---------------------------------------------------------------------------
# Entry point (scripts/ai_team.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def entry(tmp_path_factory):
    """Import scripts/ai_team.py once with an isolated store per call."""
    import importlib.util

    path = ROOT / "scripts" / "ai_team.py"
    spec = importlib.util.spec_from_file_location("ai_team_entry", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _entry_args(entry, command, tmp_path, now=None):
    args = ["--config", str(_write_config(tmp_path, FULL_CONFIG)), command]
    if now is not None:
        args = ["--config", str(_write_config(tmp_path, FULL_CONFIG)),
                "--now", now, command]
    return args


def test_entry_health_reports_ok(entry, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Health opens the store relative to cwd; point it at tmp.
    text = FULL_CONFIG.replace(
        'store_path = "output/audit_store.sqlite3"',
        f'store_path = "{(tmp_path / "h.sqlite3").as_posix()}"',
    )
    rc = entry.main(["--config", str(_write_config(tmp_path, text)), "health"])
    assert rc == 0


def test_entry_run_defers_before_close_and_writes_nothing(entry, tmp_path,
                                                          monkeypatch):
    monkeypatch.chdir(tmp_path)
    text = FULL_CONFIG.replace(
        'store_path = "output/audit_store.sqlite3"',
        f'store_path = "{(tmp_path / "r1.sqlite3").as_posix()}"',
    )
    rc = entry.main([
        "--config", str(_write_config(tmp_path, text)),
        "--now", "2026-09-18T12:00:00", "run",
    ])
    assert rc == 0                                    # deferred is a safe state
    store = AuditStore(tmp_path / "r1.sqlite3")
    assert store.list_runs() == []                    # nothing created


def test_entry_run_before_market_data_fails_cleanly(entry, tmp_path,
                                                    monkeypatch):
    """A due session without usable data must fail closed with exit 1 —
    never fake a run. The network boundary is stubbed so the test stays
    fast and hermetic; the failure contract is main()'s (SnapshotError →
    caught → exit 1), proven against the live path in the smoke test."""
    monkeypatch.chdir(tmp_path)
    text = FULL_CONFIG.replace(
        'store_path = "output/audit_store.sqlite3"',
        f'store_path = "{(tmp_path / "r2.sqlite3").as_posix()}"',
    ).replace('source = "fixed"\ntickers = ["BBCA.JK", "BBRI.JK"]',
              'source = "fixed"\ntickers = ["MISSING.JK"]')
    path = _write_config(tmp_path, text)
    from cacingnaga.legacy_adapter import get_legacy_ranking

    monkeypatch.setattr(get_legacy_ranking(), "download_ihsg", lambda period: None)
    rc = entry.main(["--config", str(path), "--now", "2026-09-18T19:00:00", "run"])
    assert rc == 1
    store = AuditStore(tmp_path / "r2.sqlite3")
    assert store.list_runs() == []          # nothing was started


def test_entry_rejects_bad_config_with_exit_1(entry, tmp_path):
    bad = FULL_CONFIG.replace("DEPLOYMENT_CONFIG_1", "DEPLOYMENT_CONFIG_X")
    path = _write_config(tmp_path, bad)
    rc = entry.main(["--config", str(path), "health"])
    assert rc == 1


def test_entry_screen_builds_snapshot_through_legacy_path(entry, tmp_path,
                                                          monkeypatch):
    """``screen`` is the manual path: dead data → exit 1, no store row."""
    monkeypatch.chdir(tmp_path)
    text = FULL_CONFIG.replace(
        'store_path = "output/audit_store.sqlite3"',
        f'store_path = "{(tmp_path / "r3.sqlite3").as_posix()}"',
    ).replace('source = "fixed"\ntickers = ["BBCA.JK", "BBRI.JK"]',
              'source = "fixed"\ntickers = ["MISSING.JK"]')
    path = _write_config(tmp_path, text)
    from cacingnaga.legacy_adapter import get_legacy_ranking

    monkeypatch.setattr(get_legacy_ranking(), "download_ihsg", lambda period: None)
    rc = entry.main(["--config", str(path), "screen"])
    assert rc == 1
    store = AuditStore(tmp_path / "r3.sqlite3")
    assert store.list_runs() == []
