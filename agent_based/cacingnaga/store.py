"""Phase 2A — durable snapshot and audit store (WP-05, plan §7 Phase 2A).

SQLite is the system of record for AI runs (GitHub Actions artifacts are not).
The store is created and verified *before* any agent call: runs are idempotent
by key, the snapshot payload + hash are persisted before the agent phase and
stay reproducible from the database, and the run state machine
(``RUNNING → COMPLETE | PARTIAL | FAILED``) is enforced in code and schema.

Every table keeps queryable columns plus a complete versioned JSON payload;
raw LLM-style responses live in ``agent_outputs`` for the Phase 3 audit trail.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_hash, canonical_json
from .config import AIAnalystConfig, config_payload
from .errors import ContractViolation
from .versioning import PIPELINE_VERSION, SCHEMA_VERSION

STORE_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,
    pipeline_version    TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    analysis_date       TEXT NOT NULL,
    as_of               TEXT NOT NULL,
    run_status          TEXT NOT NULL CHECK (run_status IN ('RUNNING','COMPLETE','PARTIAL','FAILED')),
    decision_outcome    TEXT NOT NULL CHECK (decision_outcome IN ('PENDING','TOP_3','NO_TRADE','NOT_EVALUATED')),
    data_snapshot_hash  TEXT NOT NULL,
    config_hash         TEXT NOT NULL,
    trigger             TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    completed_at        TEXT,
    payload_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    data_snapshot_hash  TEXT PRIMARY KEY,
    as_of               TEXT NOT NULL,
    analysis_date       TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_manifest (
    hash                TEXT PRIMARY KEY,
    data_snapshot_hash  TEXT NOT NULL REFERENCES snapshots(data_snapshot_hash),
    name                TEXT NOT NULL,
    source              TEXT NOT NULL,
    retrieval_time      TEXT NOT NULL,
    rows                INTEGER NOT NULL,
    first_date          TEXT NOT NULL,
    last_completed_date TEXT NOT NULL,
    price_basis         TEXT NOT NULL,
    quality_warnings    TEXT NOT NULL,
    payload_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    ticker              TEXT NOT NULL,
    data_snapshot_hash  TEXT NOT NULL REFERENCES snapshots(data_snapshot_hash),
    preliminary_rank    INTEGER NOT NULL,
    setup               TEXT NOT NULL,
    price               REAL,
    payload_json        TEXT NOT NULL,
    flow_json           TEXT NOT NULL,
    PRIMARY KEY (data_snapshot_hash, ticker)
);

CREATE TABLE IF NOT EXISTS decisions (
    run_id              TEXT NOT NULL REFERENCES runs(run_id),
    ticker              TEXT NOT NULL,
    final_status        TEXT NOT NULL CHECK (final_status IN ('READY','WAIT','REJECT')),
    agent_proposed_status TEXT NOT NULL,
    score               REAL NOT NULL,
    rank                INTEGER,
    outcome             TEXT NOT NULL CHECK (outcome IN ('RECOMMENDATION','WAIT','REJECTED')),
    payload_json        TEXT NOT NULL,
    PRIMARY KEY (run_id, ticker)
);

CREATE TABLE IF NOT EXISTS agent_outputs (
    output_id           TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(run_id),
    agent_name          TEXT NOT NULL,
    ticker              TEXT,
    schema_version      TEXT NOT NULL,
    raw_response        TEXT,
    validated_json      TEXT,
    validation_ok       INTEGER NOT NULL CHECK (validation_ok IN (0,1)),
    created_at          TEXT NOT NULL,
    payload_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS challenges (
    challenge_id        TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(run_id),
    ticker              TEXT NOT NULL,
    conflict_type       TEXT NOT NULL,
    status_effect       TEXT NOT NULL,
    payload_json        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_as_of      ON runs(as_of);
CREATE INDEX IF NOT EXISTS idx_runs_status     ON runs(run_status);
CREATE INDEX IF NOT EXISTS idx_candidates_hash ON candidates(data_snapshot_hash);
CREATE INDEX IF NOT EXISTS idx_decisions_run   ON decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_run       ON agent_outputs(run_id);
"""

_ALLOWED_TRANSITIONS = {
    "RUNNING": {"COMPLETE", "PARTIAL", "FAILED"},
    "COMPLETE": set(),
    "PARTIAL": set(),
    "FAILED": set(),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StoredRun:
    """Row-level view of a stored run (payload carries the full RunResult)."""

    run_id: str
    idempotency_key: str
    run_status: str
    decision_outcome: str
    analysis_date: str
    as_of: str
    data_snapshot_hash: str
    config_hash: str
    payload: dict[str, Any]


class _SerialConnection:
    """Proxy that serializes sqlite3 connection use across threads.

    Used only when ``AuditStore(thread_safe=True)``: one shared connection is
    guarded by a lock so handler threads (e.g. the Telegram service) can use
    the store concurrently without ``check_same_thread`` errors or torn
    transactions.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self._conn = conn
        self._lock = lock

    def execute(self, *args: Any, **kwargs: Any):
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def backup(self, *args: Any, **kwargs: Any):
        with self._lock:
            return self._conn.backup(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class AuditStore:
    """Durable, idempotent run/audit store on SQLite (system of record)."""

    def __init__(self, path: str | Path = "output/audit_store.sqlite3", *,
                 thread_safe: bool = False) -> None:
        """Open the store.

        ``thread_safe=True`` serializes access with a lock so one connection
        can be shared across handler threads (the Telegram service does
        this); the default keeps the historical single-thread behavior.
        """
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=not thread_safe)
        if thread_safe:
            conn = _SerialConnection(conn, threading.Lock())
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # -- schema ---------------------------------------------------------------

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > STORE_SCHEMA_VERSION:
            raise ContractViolation(
                f"audit store schema {version} is newer than supported {STORE_SCHEMA_VERSION}"
            )
        if version < STORE_SCHEMA_VERSION:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {STORE_SCHEMA_VERSION}")
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(STORE_SCHEMA_VERSION),),
        )
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('pipeline_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (PIPELINE_VERSION,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AuditStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- runs -----------------------------------------------------------------

    @staticmethod
    def idempotency_key(snapshot_hash: str, config_hash: str, analysis_date: str) -> str:
        """Stable trigger identity: same snapshot+config+date ⇒ same run."""
        return canonical_hash(
            {
                "pipeline_version": PIPELINE_VERSION,
                "analysis_date": analysis_date,
                "data_snapshot_hash": snapshot_hash,
                "config_hash": config_hash,
            }
        )

    def create_run(
        self,
        *,
        run_id: str,
        snapshot,
        config: AIAnalystConfig,
        snapshot_payload: dict[str, Any],
        trigger: str = "manual",
    ) -> tuple[StoredRun, bool]:
        """Register a RUNNING run *before* any agent call (idempotent).

        Returns ``(stored_run, created)``; a repeated trigger returns the
        existing row with ``created=False`` instead of duplicating records.
        """
        config.validate()
        config_hash = canonical_hash(config_payload(config))
        # The stored payload must BE the canonical snapshot payload: hash it
        # here so reproducibility from SQLite holds by construction.
        if canonical_hash(snapshot_payload) != snapshot.data_snapshot_hash:
            raise ContractViolation(
                "snapshot_payload does not hash to snapshot.data_snapshot_hash"
            )
        key = self.idempotency_key(
            snapshot.data_snapshot_hash, config_hash, snapshot.analysis_date
        )

        existing = self._conn.execute(
            "SELECT * FROM runs WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing is not None:
            return self._row_to_run(existing), False

        now = _utc_now()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "run_id": run_id,
            "idempotency_key": key,
            "analysis_date": snapshot.analysis_date,
            "as_of": snapshot.as_of,
            "run_status": "RUNNING",
            "decision_outcome": "PENDING",
            "trigger": trigger,
            "config": config_payload(config),
            "warnings": list(snapshot.warnings),
        }
        try:
            self._conn.execute(
                "INSERT INTO runs (run_id, idempotency_key, pipeline_version, schema_version,"
                " analysis_date, as_of, run_status, decision_outcome, data_snapshot_hash,"
                " config_hash, trigger, created_at, updated_at, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', 'PENDING', ?, ?, ?, ?, ?, ?)",
                (
                    run_id, key, PIPELINE_VERSION, SCHEMA_VERSION,
                    snapshot.analysis_date, snapshot.as_of,
                    snapshot.data_snapshot_hash, config_hash, trigger, now, now,
                    canonical_json(payload),
                ),
            )
            # Snapshot + manifest + candidates persist with the run, before
            # the agent phase (exit criterion: hash reproducible from SQLite).
            self._store_snapshot(snapshot, snapshot_payload, now)
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            row = self._conn.execute(
                "SELECT * FROM runs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is None:
                raise
            return self._row_to_run(row), False
        return self.get_run(run_id), True  # type: ignore[return-value]

    def _store_snapshot(self, snapshot, snapshot_payload: dict[str, Any], now: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO snapshots (data_snapshot_hash, as_of, analysis_date,"
            " payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                snapshot.data_snapshot_hash, snapshot.as_of, snapshot.analysis_date,
                canonical_json(snapshot_payload), now,
            ),
        )
        for record in snapshot.manifest:
            record_payload = record.payload()
            self._conn.execute(
                "INSERT OR IGNORE INTO source_manifest (hash, data_snapshot_hash, name,"
                " source, retrieval_time, rows, first_date, last_completed_date,"
                " price_basis, quality_warnings, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    canonical_hash(record_payload), snapshot.data_snapshot_hash,
                    record.name, record.source, record.retrieval_time, record.rows,
                    record.first_date, record.last_completed_date, record.price_basis,
                    canonical_json(record.quality_warnings), canonical_json(record_payload),
                ),
            )
        for rank, (facts, flow) in enumerate(
            zip(snapshot.candidates, snapshot.flows), start=1
        ):
            self._conn.execute(
                "INSERT OR IGNORE INTO candidates (ticker, data_snapshot_hash,"
                " preliminary_rank, setup, price, payload_json, flow_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    facts.ticker, snapshot.data_snapshot_hash, rank, facts.setup,
                    facts.price, canonical_json(facts.payload()),
                    canonical_json(flow.payload()),
                ),
            )

    def _row_to_run(self, row: sqlite3.Row) -> StoredRun:
        return StoredRun(
            run_id=row["run_id"],
            idempotency_key=row["idempotency_key"],
            run_status=row["run_status"],
            decision_outcome=row["decision_outcome"],
            analysis_date=row["analysis_date"],
            as_of=row["as_of"],
            data_snapshot_hash=row["data_snapshot_hash"],
            config_hash=row["config_hash"],
            payload=json.loads(row["payload_json"]),
        )

    def get_run(self, run_id: str) -> StoredRun | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._row_to_run(row) if row is not None else None

    def get_run_by_idempotency_key(self, key: str) -> StoredRun | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return self._row_to_run(row) if row is not None else None

    def list_runs(self, status: str | None = None, limit: int = 100) -> list[StoredRun]:
        if status is not None and status not in _ALLOWED_TRANSITIONS:
            raise ContractViolation(f"unknown run status filter: {status}")
        query = "SELECT * FROM runs"
        params: tuple = ()
        if status is not None:
            query += " WHERE run_status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC LIMIT ?"
        rows = self._conn.execute(query, (*params, limit)).fetchall()
        return [self._row_to_run(row) for row in rows]

    def transition_run(self, run_id: str, new_status: str) -> StoredRun:
        """Enforce ``RUNNING → COMPLETE | PARTIAL | FAILED`` (terminal states)."""
        if new_status not in _ALLOWED_TRANSITIONS:
            raise ContractViolation(f"unknown run status: {new_status}")
        run = self.get_run(run_id)
        if run is None:
            raise ContractViolation(f"unknown run: {run_id}")
        if new_status not in _ALLOWED_TRANSITIONS[run.run_status]:
            raise ContractViolation(
                f"illegal transition {run.run_status} -> {new_status} for run {run_id}"
            )
        now = _utc_now()
        payload = dict(run.payload)
        payload["run_status"] = new_status
        self._conn.execute(
            "UPDATE runs SET run_status = ?, updated_at = ?, completed_at = ?,"
            " payload_json = ? WHERE run_id = ?",
            (new_status, now, now, canonical_json(payload), run_id),
        )
        self._conn.commit()
        return self.get_run(run_id)  # type: ignore[return-value]

    # -- decisions ------------------------------------------------------------

    def save_decisions(
        self,
        run_id: str,
        decisions: Iterable[tuple[str, str, str, float, int | None]],
        *,
        challenge_refs: dict[str, str] | None = None,
    ) -> None:
        """Store (ticker, final_status, agent_proposed_status, score, rank) rows.

        ``challenge_refs`` optionally maps ticker → Phase 6 debate record id,
        persisted inside the row payload for the audit trail.
        """
        run = self.get_run(run_id)
        if run is None:
            raise ContractViolation(f"unknown run: {run_id}")
        now = _utc_now()
        for ticker, final_status, proposed, score, rank in decisions:
            outcome = {"READY": "RECOMMENDATION", "WAIT": "WAIT", "REJECT": "REJECTED"}[final_status]
            payload = {
                "run_id": run_id,
                "ticker": ticker,
                "final_status": final_status,
                "agent_proposed_status": proposed,
                "score": score,
                "rank": rank,
                "challenge_ref": (challenge_refs or {}).get(ticker),
                "recorded_at": now,
            }
            self._conn.execute(
                "INSERT INTO decisions (run_id, ticker, final_status, agent_proposed_status,"
                " score, rank, outcome, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, ticker) DO UPDATE SET final_status=excluded.final_status,"
                " agent_proposed_status=excluded.agent_proposed_status, score=excluded.score,"
                " rank=excluded.rank, outcome=excluded.outcome, payload_json=excluded.payload_json",
                (run_id, ticker, final_status, proposed, score, rank, outcome,
                 canonical_json(payload)),
            )
        self._conn.commit()

    def get_decisions(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE run_id = ? ORDER BY rank IS NULL, rank, ticker",
            (run_id,),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    # -- agent outputs / challenges (Phase 3+ audit trail) --------------------

    def save_agent_output(
        self,
        run_id: str,
        *,
        agent_name: str,
        schema_version: str,
        ticker: str | None = None,
        raw_response: str | None = None,
        validated: dict[str, Any] | None = None,
        validation_ok: bool = True,
        prompt_hash: str = "",
        latency_ms: int = 0,
        usage: dict[str, Any] | None = None,
        validation_error: str = "",
    ) -> str:
        run = self.get_run(run_id)
        if run is None:
            raise ContractViolation(f"unknown run: {run_id}")
        now = _utc_now()
        output_id = canonical_hash(
            {"run_id": run_id, "agent": agent_name, "ticker": ticker, "at": now}
        )
        payload = {
            "output_id": output_id,
            "run_id": run_id,
            "agent_name": agent_name,
            "ticker": ticker,
            "schema_version": schema_version,
            "validation_ok": validation_ok,
            "prompt_hash": prompt_hash,
            "latency_ms": latency_ms,
            "usage": dict(usage or {}),
            "validation_error": validation_error,
            "recorded_at": now,
        }
        self._conn.execute(
            "INSERT INTO agent_outputs (output_id, run_id, agent_name, ticker,"
            " schema_version, raw_response, validated_json, validation_ok, created_at,"
            " payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                output_id, run_id, agent_name, ticker, schema_version, raw_response,
                canonical_json(validated) if validated is not None else None,
                1 if validation_ok else 0, now, canonical_json(payload),
            ),
        )
        self._conn.commit()
        return output_id

    # -- challenges (Phase 6: complete ChallengeRecord persistence) ------------

    def save_challenge(
        self,
        run_id: str,
        *,
        challenge_id: str,
        ticker: str,
        conflict_type: str,
        status_effect: str,
        record: dict[str, Any],
    ) -> None:
        """Persist one complete ``ChallengeRecord`` (item 6).

        The canonical record payload carries rule versions, the packet
        (questions + evidence ids), validated turn views, the resolution, the
        final status effect, and the agent_outputs ids holding the raw
        responses. Re-saving the same challenge_id replaces the row.
        """
        run = self.get_run(run_id)
        if run is None:
            raise ContractViolation(f"unknown run: {run_id}")
        self._conn.execute(
            "INSERT OR REPLACE INTO challenges (challenge_id, run_id, ticker,"
            " conflict_type, status_effect, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                challenge_id, run_id, ticker, conflict_type, status_effect,
                canonical_json(record),
            ),
        )
        self._conn.commit()

    def get_challenge(self, challenge_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload_json FROM challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_challenges(self, run_id: str) -> list[dict[str, Any]]:
        """All ChallengeRecords for one run (ordered by ticker, rule)."""
        rows = self._conn.execute(
            "SELECT payload_json FROM challenges WHERE run_id = ?"
            " ORDER BY ticker, conflict_type",
            (run_id,),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get_agent_outputs(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM agent_outputs WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [
            {
                "output_id": row["output_id"],
                "agent_name": row["agent_name"],
                "ticker": row["ticker"],
                "validation_ok": bool(row["validation_ok"]),
                "validated": json.loads(row["validated_json"]) if row["validated_json"] else None,
                "raw_response": row["raw_response"],
                "created_at": row["created_at"],
                "prompt_hash": (json.loads(row["payload_json"]) or {}).get("prompt_hash", "") if row["payload_json"] else "",
                "latency_ms": (json.loads(row["payload_json"]) or {}).get("latency_ms", 0) if row["payload_json"] else 0,
                "usage": (json.loads(row["payload_json"]) or {}).get("usage", {}) if row["payload_json"] else {},
                "validation_error": (json.loads(row["payload_json"]) or {}).get("validation_error", "") if row["payload_json"] else "",
            }
            for row in rows
        ]

    # -- snapshot verification --------------------------------------------------

    def verify_snapshot_hash(self, data_snapshot_hash: str) -> bool:
        """Recompute the canonical hash of the *stored* snapshot payload."""
        row = self._conn.execute(
            "SELECT payload_json FROM snapshots WHERE data_snapshot_hash = ?",
            (data_snapshot_hash,),
        ).fetchone()
        if row is None:
            return False
        payload = json.loads(row["payload_json"])
        return canonical_hash(payload) == data_snapshot_hash

    def get_snapshot(self, data_snapshot_hash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload_json FROM snapshots WHERE data_snapshot_hash = ?",
            (data_snapshot_hash,),
        ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    # -- backup / restore -------------------------------------------------------

    def backup(self, target: str | Path) -> None:
        """Consistent online backup (survives process restart / migration)."""
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(str(target_path))
        try:
            with destination:
                self._conn.backup(destination)
        finally:
            destination.close()

    @classmethod
    def restore(cls, source: str | Path, target: str | Path) -> "AuditStore":
        """Copy a backup file to ``target`` and open it (schema-verified)."""
        source_path, target_path = Path(source), Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(source_path.read_bytes())
        return cls(target_path)
