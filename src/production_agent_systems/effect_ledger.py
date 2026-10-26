from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .effect_models import (
    EffectRecord,
    EffectStatus,
    OperationConflictError,
    _json_dumps,
    _utc_now,
)


class SQLiteEffectLedger:
    """SQLite-backed effect ledger with transactional claims and event history.

    Use a file-backed database. A durable ledger does not make a remote provider
    transactional; it records the local contract and recovery evidence.
    """

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError(
                "SQLiteEffectLedger requires a file-backed database; use a temporary file in tests"
            )
        database_path = Path(self.database).expanduser().resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database = str(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS effect_operations (
                    operation_id TEXT PRIMARY KEY,
                    contract_name TEXT NOT NULL,
                    contract_version TEXT NOT NULL DEFAULT '1',
                    request_fingerprint TEXT NOT NULL,
                    provider_idempotency_key TEXT,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    provider_operation_id TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    owner_token TEXT,
                    lease_until REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effect_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES effect_operations(operation_id),
                    event_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_effect_events_operation
                    ON effect_events(operation_id, sequence);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(effect_operations)").fetchall()
            }
            if "contract_version" not in columns:
                connection.execute(
                    "ALTER TABLE effect_operations ADD COLUMN contract_version TEXT NOT NULL DEFAULT '1'"
                )
        finally:
            connection.close()

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        operation_id: str,
        event_type: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO effect_events(operation_id, event_type, data_json, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (operation_id, event_type, _json_dumps(dict(data or {})), _utc_now()),
        )

    @staticmethod
    def _to_record(row: sqlite3.Row | None) -> EffectRecord | None:
        if row is None:
            return None
        result_json = row["result_json"]
        return EffectRecord(
            operation_id=row["operation_id"],
            contract_name=row["contract_name"],
            contract_version=row["contract_version"],
            request_fingerprint=row["request_fingerprint"],
            provider_idempotency_key=row["provider_idempotency_key"],
            status=EffectStatus(row["status"]),
            output=json.loads(result_json) if result_json is not None else None,
            has_output=result_json is not None,
            provider_operation_id=row["provider_operation_id"],
            error=row["error"],
            attempts=int(row["attempts"]),
            evidence=json.loads(row["evidence_json"]),
            owner_token=row["owner_token"],
            lease_until=row["lease_until"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def prepare(
        self,
        operation_id: str,
        contract_name: str,
        contract_version: str,
        request_fingerprint: str,
        provider_idempotency_key: str | None,
    ) -> EffectRecord:
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO effect_operations(
                        operation_id, contract_name, contract_version, request_fingerprint,
                        provider_idempotency_key, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        contract_name,
                        contract_version,
                        request_fingerprint,
                        provider_idempotency_key,
                        EffectStatus.PREPARED.value,
                        now,
                        now,
                    ),
                )
                self._append_event(
                    connection,
                    operation_id,
                    "prepared",
                    {
                        "contract_name": contract_name,
                        "contract_version": contract_version,
                        "request_fingerprint": request_fingerprint,
                    },
                )
                row = connection.execute(
                    "SELECT * FROM effect_operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
            else:
                if (
                    row["contract_name"] != contract_name
                    or row["contract_version"] != contract_version
                    or row["request_fingerprint"] != request_fingerprint
                    or row["provider_idempotency_key"] != provider_idempotency_key
                ):
                    raise OperationConflictError(
                        "operation_id already belongs to a different contract version, request, or provider key"
                    )
            record = self._to_record(row)
        assert record is not None
        return record

    def get(self, operation_id: str) -> EffectRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            return self._to_record(row)
        finally:
            connection.close()

    def claim_dispatch(
        self, operation_id: str, owner_token: str, lease_seconds: float
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE effect_operations
                SET status = ?, attempts = attempts + 1, owner_token = ?,
                    lease_until = ?, error = NULL, updated_at = ?
                WHERE operation_id = ? AND status = ?
                """,
                (
                    EffectStatus.DISPATCHED.value,
                    owner_token,
                    now + lease_seconds,
                    _utc_now(),
                    operation_id,
                    EffectStatus.PREPARED.value,
                ),
            )
            if cursor.rowcount != 1:
                return False
            row = connection.execute(
                "SELECT attempts FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            self._append_event(
                connection,
                operation_id,
                "dispatched",
                {"attempt": int(row["attempts"])},
            )
            return True

    def mark_outcome_unknown(
        self, operation_id: str, error: str, *, owner_token: str | None = None
    ) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                return False
            if owner_token is None:
                if row["status"] not in {
                    EffectStatus.OUTCOME_UNKNOWN.value,
                    EffectStatus.QUARANTINED.value,
                }:
                    return False
            elif (
                row["status"] != EffectStatus.DISPATCHED.value
                or row["owner_token"] != owner_token
            ):
                return False
            connection.execute(
                """
                UPDATE effect_operations
                SET status = ?, error = ?, owner_token = NULL, lease_until = NULL,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (EffectStatus.OUTCOME_UNKNOWN.value, error, _utc_now(), operation_id),
            )
            self._append_event(
                connection, operation_id, "outcome_unknown", {"error": error}
            )
            return True

    def mark_confirmed(
        self,
        operation_id: str,
        output: Any,
        *,
        provider_operation_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        owner_token: str | None = None,
    ) -> bool:
        result_json = _json_dumps(output)
        evidence_dict = dict(evidence or {})
        evidence_json = _json_dumps(evidence_dict)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row["status"] == EffectStatus.REJECTED.value:
                return False
            if row["status"] == EffectStatus.CONFIRMED.value:
                return True
            if owner_token is None:
                if row["status"] not in {
                    EffectStatus.OUTCOME_UNKNOWN.value,
                    EffectStatus.QUARANTINED.value,
                }:
                    return False
            elif (
                row["status"] != EffectStatus.DISPATCHED.value
                or row["owner_token"] != owner_token
            ):
                return False
            connection.execute(
                """
                UPDATE effect_operations
                SET status = ?, result_json = ?, provider_operation_id = ?,
                    evidence_json = ?, error = NULL, owner_token = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    EffectStatus.CONFIRMED.value,
                    result_json,
                    provider_operation_id,
                    evidence_json,
                    _utc_now(),
                    operation_id,
                ),
            )
            self._append_event(
                connection,
                operation_id,
                "confirmed",
                {
                    "provider_operation_id": provider_operation_id,
                    "evidence": evidence_dict,
                },
            )
            return True

    def mark_rejected(
        self, operation_id: str, error: str, *, owner_token: str
    ) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row["status"] != EffectStatus.DISPATCHED.value:
                return False
            if row["owner_token"] != owner_token:
                return False
            connection.execute(
                """
                UPDATE effect_operations
                SET status = ?, error = ?, owner_token = NULL, lease_until = NULL,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (EffectStatus.REJECTED.value, error, _utc_now(), operation_id),
            )
            self._append_event(connection, operation_id, "rejected", {"error": error})
            return True

    def mark_quarantined(
        self, operation_id: str, error: str, *, evidence: Mapping[str, Any] | None = None
    ) -> bool:
        evidence_dict = dict(evidence or {})
        evidence_json = _json_dumps(evidence_dict)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row["status"] not in {
                EffectStatus.OUTCOME_UNKNOWN.value,
                EffectStatus.QUARANTINED.value,
            }:
                return False
            connection.execute(
                """
                UPDATE effect_operations
                SET status = ?, error = ?, evidence_json = ?, lease_until = 0,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (EffectStatus.QUARANTINED.value, error, evidence_json, _utc_now(), operation_id),
            )
            self._append_event(
                connection, operation_id, "quarantined", {"error": error, "evidence": evidence_dict}
            )
            return True

    def reset_for_retry(
        self, operation_id: str, *, evidence: Mapping[str, Any] | None = None
    ) -> bool:
        evidence_dict = dict(evidence or {})
        evidence_json = _json_dumps(evidence_dict)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row["status"] not in {
                EffectStatus.OUTCOME_UNKNOWN.value,
                EffectStatus.QUARANTINED.value,
            }:
                return False
            connection.execute(
                """
                UPDATE effect_operations
                SET status = ?, error = NULL, evidence_json = ?, owner_token = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    EffectStatus.PREPARED.value,
                    evidence_json,
                    _utc_now(),
                    operation_id,
                ),
            )
            self._append_event(
                connection, operation_id, "retry_authorized", {"evidence": evidence_dict}
            )
            return True

    def list_operations(
        self, status: EffectStatus | None = None, limit: int = 100
    ) -> list[EffectRecord]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        connection = self._connect()
        try:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM effect_operations ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM effect_operations
                    WHERE status = ? ORDER BY updated_at DESC LIMIT ?
                    """,
                    (status.value, limit),
                ).fetchall()
            records = [self._to_record(row) for row in rows]
            return [record for record in records if record is not None]
        finally:
            connection.close()

    def resolve_manually_confirmed(
        self,
        operation_id: str,
        output: Any,
        *,
        provider_operation_id: str | None,
        evidence: Mapping[str, Any],
    ) -> bool:
        result_json = _json_dumps(output)
        evidence_dict = dict(evidence)
        evidence_json = _json_dumps(evidence_dict)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row["status"] not in {
                EffectStatus.OUTCOME_UNKNOWN.value,
                EffectStatus.QUARANTINED.value,
            }:
                return False
            connection.execute(
                """
                UPDATE effect_operations
                SET status = ?, result_json = ?, provider_operation_id = ?,
                    evidence_json = ?, error = NULL, owner_token = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    EffectStatus.CONFIRMED.value,
                    result_json,
                    provider_operation_id,
                    evidence_json,
                    _utc_now(),
                    operation_id,
                ),
            )
            self._append_event(
                connection,
                operation_id,
                "manually_confirmed",
                {
                    "provider_operation_id": provider_operation_id,
                    "evidence": evidence_dict,
                },
            )
            return True

    def authorize_manual_retry(
        self, operation_id: str, *, evidence: Mapping[str, Any]
    ) -> bool:
        evidence_dict = dict(evidence)
        evidence_json = _json_dumps(evidence_dict)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row["status"] not in {
                EffectStatus.OUTCOME_UNKNOWN.value,
                EffectStatus.QUARANTINED.value,
            }:
                return False
            connection.execute(
                """
                UPDATE effect_operations
                SET status = ?, error = NULL, evidence_json = ?, owner_token = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    EffectStatus.PREPARED.value,
                    evidence_json,
                    _utc_now(),
                    operation_id,
                ),
            )
            self._append_event(
                connection, operation_id, "manual_retry_authorized", {"evidence": evidence_dict}
            )
            return True

    def resolve_manually_rejected(
        self, operation_id: str, *, reason: str, evidence: Mapping[str, Any]
    ) -> bool:
        evidence_dict = dict(evidence)
        evidence_json = _json_dumps(evidence_dict)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM effect_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row["status"] not in {
                EffectStatus.OUTCOME_UNKNOWN.value,
                EffectStatus.QUARANTINED.value,
            }:
                return False
            connection.execute(
                """
                UPDATE effect_operations
                SET status = ?, error = ?, evidence_json = ?, owner_token = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE operation_id = ?
                """,
                (EffectStatus.REJECTED.value, reason, evidence_json, _utc_now(), operation_id),
            )
            self._append_event(
                connection,
                operation_id,
                "manually_rejected",
                {"reason": reason, "evidence": evidence_dict},
            )
            return True

    def events(self, operation_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT sequence, event_type, data_json, timestamp
                FROM effect_events WHERE operation_id = ? ORDER BY sequence
                """,
                (operation_id,),
            ).fetchall()
            return [
                {
                    "sequence": int(row["sequence"]),
                    "event_type": row["event_type"],
                    "data": json.loads(row["data_json"]),
                    "timestamp": row["timestamp"],
                }
                for row in rows
            ]
        finally:
            connection.close()
