from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]  # agent_based/
sys.path.insert(0, str(BASE))

from cacingnaga import exporters
from cacingnaga.canonical import canonical_hash
from cacingnaga.config import AIAnalystConfig, config_payload
from cacingnaga.errors import ContractViolation
from cacingnaga.fixtures import default_frames, market_frame
from cacingnaga.policy import evaluate_snapshot
from cacingnaga.snapshot import build_snapshot
from cacingnaga.store import STORE_SCHEMA_VERSION, AuditStore

CONFIG = AIAnalystConfig()


@pytest.fixture(scope="module")
def snapshot():
    return build_snapshot(market_frame(), default_frames(), CONFIG)


def _snapshot():
    return build_snapshot(market_frame(), default_frames(), CONFIG)


def _snapshot_payload(snapshot):
    """Canonical snapshot payload as stored (self-verifying via hash)."""
    return snapshot.payload()


def _fresh_store(tmp_path, name="store.sqlite3") -> AuditStore:
    return AuditStore(tmp_path / name)


def _run_outcomes(snapshot):
    return evaluate_snapshot(snapshot, CONFIG)


# ---------------------------------------------------------------------------
# Registration is idempotent and persists the pre-agent snapshot
# ---------------------------------------------------------------------------


def test_create_run_is_idempotent_on_repeated_trigger(tmp_path):
    snapshot = _snapshot()
    with _fresh_store(tmp_path) as store:
        first, created1 = store.create_run(
            run_id="run-1", snapshot=snapshot, config=CONFIG,
            snapshot_payload=_snapshot_payload(snapshot),
        )
        again, created2 = store.create_run(
            run_id="run-2", snapshot=snapshot, config=CONFIG,
            snapshot_payload=_snapshot_payload(snapshot),
        )
        assert created1 and not created2
        assert first.run_id == "run-1"           # the ORIGINAL run is returned
        assert again.run_id == first.run_id
        assert len(store.list_runs()) == 1


def test_snapshot_hash_is_reproducible_from_sqlite(tmp_path):
    snapshot = _snapshot()
    with _fresh_store(tmp_path) as store:
        run, _ = store.create_run(
            run_id="run-hash", snapshot=snapshot, config=CONFIG,
            snapshot_payload=_snapshot_payload(snapshot),
        )
        assert store.verify_snapshot_hash(run.data_snapshot_hash)
        stored = store.get_snapshot(run.data_snapshot_hash)
        assert canonical_hash(stored) == run.data_snapshot_hash


def test_snapshot_payload_hash_mismatch_is_rejected(tmp_path):
    snapshot = _snapshot()
    with _fresh_store(tmp_path) as store:
        bad_payload = _snapshot_payload(snapshot)
        bad_payload["as_of"] = "1999-01-01"      # mutate after hashing
        with pytest.raises(ContractViolation):
            store.create_run(
                run_id="run-bad", snapshot=snapshot, config=CONFIG,
                snapshot_payload=bad_payload,
            )


def test_run_survives_process_restart(tmp_path):
    snapshot = _snapshot()
    path = tmp_path / "durable.sqlite3"
    with AuditStore(path) as store:
        run, created = store.create_run(
            run_id="run-durable", snapshot=snapshot, config=CONFIG,
            snapshot_payload=_snapshot_payload(snapshot),
        )
        assert created
    # "Restart": brand-new connection to the same file.
    with AuditStore(path) as reopened:
        restored = reopened.get_run("run-durable")
        assert restored is not None
        assert restored.run_status == "RUNNING"
        assert restored.data_snapshot_hash == run.data_snapshot_hash
        assert reopened.verify_snapshot_hash(run.data_snapshot_hash)


# ---------------------------------------------------------------------------
# Run state machine
# ---------------------------------------------------------------------------


def test_state_machine_enforces_terminal_states(tmp_path):
    snapshot = _snapshot()
    with _fresh_store(tmp_path) as store:
        run, _ = store.create_run(
            run_id="run-fsm", snapshot=snapshot, config=CONFIG,
            snapshot_payload=_snapshot_payload(snapshot),
        )
        assert run.run_status == "RUNNING"
        store.transition_run("run-fsm", "COMPLETE")
        with pytest.raises(ContractViolation):
            store.transition_run("run-fsm", "FAILED")   # terminal state
        with pytest.raises(ContractViolation):
            store.transition_run("run-fsm", "RUNNING")


def test_unknown_status_and_unknown_run_rejected(tmp_path):
    snapshot = _snapshot()
    with _fresh_store(tmp_path) as store:
        with pytest.raises(ContractViolation):
            store.transition_run("missing-run", "COMPLETE")
        store.create_run(
            run_id="run-x", snapshot=snapshot, config=CONFIG,
            snapshot_payload=_snapshot_payload(snapshot),
        )
        with pytest.raises(ContractViolation):
            store.transition_run("run-x", "SUCCEEDED")


# ---------------------------------------------------------------------------
# Decisions + agent outputs (audit trail)
# ---------------------------------------------------------------------------


def test_decisions_persist_and_match_run_payload(tmp_path):
    snapshot = _snapshot()
    run_result, outcomes = _run_outcomes(snapshot)
    with _fresh_store(tmp_path) as store:
        run, _ = store.create_run(
            run_id=run_result.run_id, snapshot=snapshot, config=CONFIG,
            snapshot_payload=_snapshot_payload(snapshot),
        )
        rows = [
            (d.ticker, d.final_status, d.agent_proposed_status, d.score, d.rank)
            for d in (*run_result.recommendations, *run_result.wait, *run_result.rejected)
        ]
        store.save_decisions(run.run_id, rows)
        stored = store.get_decisions(run.run_id)
        assert {r["ticker"] for r in stored} == {t for t, *_ in rows}
        # Ranks must match the finalized run exactly (rendering never mutates).
        stored_by_ticker = {r["ticker"]: r for r in stored}
        for d in run_result.recommendations:
            assert stored_by_ticker[d.ticker]["rank"] == d.rank
            assert stored_by_ticker[d.ticker]["final_status"] == "READY"


def test_agent_output_roundtrip(tmp_path):
    snapshot = _snapshot()
    with _fresh_store(tmp_path) as store:
        run, _ = store.create_run(
            run_id="run-agents", snapshot=snapshot, config=CONFIG,
            snapshot_payload=_snapshot_payload(snapshot),
        )
        output_id = store.save_agent_output(
            "run-agents",
            agent_name="MarketAgent",
            schema_version="1.0.0",
            raw_response='{"regime": "BULLISH"}',
            validated={"regime": "BULLISH", "confidence_band": "LOW"},
            validation_ok=True,
        )
        outputs = store.get_agent_outputs("run-agents")
        assert len(outputs) == 1
        assert outputs[0]["output_id"] == output_id
        assert outputs[0]["validated"]["regime"] == "BULLISH"
        assert outputs[0]["raw_response"].startswith("{")
        # Unknown runs are rejected.
        with pytest.raises(ContractViolation):
            store.save_agent_output(
                "missing", agent_name="X", schema_version="1"
            )


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------


def test_backup_and_restore_preserve_everything(tmp_path):
    snapshot = _snapshot()
    original_path = tmp_path / "original.sqlite3"
    with AuditStore(original_path) as store:
        run, _ = store.create_run(
            run_id="run-backup", snapshot=snapshot, config=CONFIG,
            snapshot_payload=_snapshot_payload(snapshot),
        )
        store.backup(tmp_path / "backup.sqlite3")

    restored = AuditStore.restore(tmp_path / "backup.sqlite3", tmp_path / "restored.sqlite3")
    try:
        same_run = restored.get_run("run-backup")
        assert same_run is not None
        assert same_run.data_snapshot_hash == run.data_snapshot_hash
        assert restored.verify_snapshot_hash(run.data_snapshot_hash)
        assert len(restored.list_runs()) == 1
    finally:
        restored.close()


def test_newer_schema_is_refused(tmp_path, monkeypatch):
    snapshot = _snapshot()
    path = tmp_path / "future.sqlite3"
    with AuditStore(path) as store:
        store.create_run(
            run_id="run-v", snapshot=snapshot, config=CONFIG,
            snapshot_payload=_snapshot_payload(snapshot),
        )
        store._conn.execute("PRAGMA user_version = 999")
        store._conn.commit()
    with pytest.raises(ContractViolation):
        AuditStore(path)


# ---------------------------------------------------------------------------
# Exporters (legacy-compatible artifacts)
# ---------------------------------------------------------------------------


def test_exporters_render_without_mutating(tmp_path, snapshot):
    run, _ = _run_outcomes(snapshot)
    json_path = exporters.export_run_json(run, tmp_path / "run.json")
    csv_path = exporters.export_decisions_csv(run, tmp_path / "decisions.csv")
    md_path = exporters.export_run_markdown(run, tmp_path / "run.md")
    snap_path = exporters.export_snapshot_json(
        _snapshot_payload(snapshot), tmp_path / "snapshot.json"
    )
    assert json_path.exists() and csv_path.exists() and md_path.exists() and snap_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == run.run_id
    assert payload["decision_outcome"] == run.decision_outcome
    assert len(payload["recommendations"]) == len(run.recommendations)

    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("ticker,final_status,agent_proposed_status")

    markdown = md_path.read_text(encoding="utf-8")
    assert "CACINGNAGAPRO RUN" in markdown
    assert run.decision_outcome in markdown

    stored_snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    assert canonical_hash(stored_snapshot) == snapshot.data_snapshot_hash


def test_format_decisions_table_is_stable(snapshot):
    run, _ = _run_outcomes(snapshot)
    table1 = exporters.format_decisions_table(run)
    table2 = exporters.format_decisions_table(run)
    assert table1 == table2
    has_ready = any(d.final_status == "READY" for d in run.recommendations)
    assert ("READY" in table1) == has_ready or "(no evaluated candidates)" in table1
    assert run.run_id  # canonical identity present in the source run
