from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol


class EffectStatus(str, Enum):
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    CONFIRMED = "confirmed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class ProbeStatus(str, Enum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    PENDING = "pending"
    UNKNOWN = "unknown"


class UnknownOutcomePolicy(str, Enum):
    QUARANTINE = "quarantine"
    KEEP_UNKNOWN = "keep_unknown"


class EffectError(RuntimeError):
    """Base class for effect-ledger errors."""


class OperationConflictError(EffectError):
    """Raised when an operation ID is reused for a different request or contract."""


class EffectRejected(EffectError):
    """A handler may raise this only when the provider definitively rejected the effect."""


@dataclass(frozen=True)
class OutcomeProbeResult:
    status: ProbeStatus
    output: Any = None
    provider_operation_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProbeStatus):
            raise ValueError("outcome probe status must be a ProbeStatus value")
        try:
            _json_dumps(dict(self.evidence))
        except (TypeError, ValueError) as exc:
            raise ValueError("outcome probe evidence must be finite JSON-compatible data") from exc


@dataclass(frozen=True)
class EffectContract:
    """The rules needed to execute and recover one kind of external effect.

    A NOT_COMMITTED probe result must be authoritative and terminal: it must prove
    that the earlier request cannot still commit later. A merely empty or eventually
    consistent read is not sufficient.
    """

    name: str
    operation_id: Callable[[Mapping[str, Any]], str]
    provider_idempotency_key: Callable[[Mapping[str, Any]], str] | None = None
    provider_idempotency_guaranteed: bool = False
    outcome_probe: Callable[..., OutcomeProbeResult] | None = None
    postcondition: Callable[[Mapping[str, Any], Any], bool] | None = None
    unknown_outcome: UnknownOutcomePolicy = UnknownOutcomePolicy.QUARANTINE
    version: str = "1"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("contract name must be a non-empty string")
        if not callable(self.operation_id):
            raise ValueError("contract operation_id must be callable")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("contract version must be a non-empty string")
        if self.provider_idempotency_key is not None and not callable(
            self.provider_idempotency_key
        ):
            raise ValueError("provider_idempotency_key must be callable or None")
        if self.outcome_probe is not None and not callable(self.outcome_probe):
            raise ValueError("outcome_probe must be callable or None")
        if self.postcondition is not None and not callable(self.postcondition):
            raise ValueError("postcondition must be callable or None")
        if not isinstance(self.unknown_outcome, UnknownOutcomePolicy):
            raise ValueError("unknown_outcome must be an UnknownOutcomePolicy value")
        if self.provider_idempotency_guaranteed and self.provider_idempotency_key is None:
            raise ValueError(
                "provider_idempotency_guaranteed requires provider_idempotency_key"
            )


@dataclass(frozen=True)
class EffectRecord:
    operation_id: str
    contract_name: str
    contract_version: str
    request_fingerprint: str
    provider_idempotency_key: str | None
    status: EffectStatus
    output: Any = None
    has_output: bool = False
    provider_operation_id: str | None = None
    error: str | None = None
    attempts: int = 0
    evidence: Mapping[str, Any] = field(default_factory=dict)
    owner_token: str | None = None
    lease_until: float | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class EffectResult:
    operation_id: str
    status: EffectStatus
    output: Any = None
    error: str | None = None
    attempts: int = 0
    duplicate_suppressed: bool = False
    in_progress: bool = False
    provider_operation_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.status is EffectStatus.CONFIRMED


class EffectLedger(Protocol):
    def prepare(
        self,
        operation_id: str,
        contract_name: str,
        contract_version: str,
        request_fingerprint: str,
        provider_idempotency_key: str | None,
    ) -> EffectRecord: ...

    def get(self, operation_id: str) -> EffectRecord | None: ...

    def claim_dispatch(
        self, operation_id: str, owner_token: str, lease_seconds: float
    ) -> bool: ...

    def mark_outcome_unknown(
        self, operation_id: str, error: str, *, owner_token: str | None = None
    ) -> bool: ...

    def mark_confirmed(
        self,
        operation_id: str,
        output: Any,
        *,
        provider_operation_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        owner_token: str | None = None,
    ) -> bool: ...

    def mark_rejected(
        self, operation_id: str, error: str, *, owner_token: str
    ) -> bool: ...

    def mark_quarantined(
        self, operation_id: str, error: str, *, evidence: Mapping[str, Any] | None = None
    ) -> bool: ...

    def reset_for_retry(
        self, operation_id: str, *, evidence: Mapping[str, Any] | None = None
    ) -> bool: ...

    def list_operations(
        self, status: EffectStatus | None = None, limit: int = 100
    ) -> list[EffectRecord]: ...

    def resolve_manually_confirmed(
        self,
        operation_id: str,
        output: Any,
        *,
        provider_operation_id: str | None,
        evidence: Mapping[str, Any],
    ) -> bool: ...

    def authorize_manual_retry(
        self, operation_id: str, *, evidence: Mapping[str, Any]
    ) -> bool: ...

    def resolve_manually_rejected(
        self, operation_id: str, *, reason: str, evidence: Mapping[str, Any]
    ) -> bool: ...

    def events(self, operation_id: str) -> list[dict[str, Any]]: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
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
            if row is None or row["status"] in {
                EffectStatus.CONFIRMED.value,
                EffectStatus.REJECTED.value,
            }:
                return False
            if owner_token is not None and row["owner_token"] != owner_token:
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
            if row["status"] not in {
                EffectStatus.DISPATCHED.value,
                EffectStatus.OUTCOME_UNKNOWN.value,
                EffectStatus.QUARANTINED.value,
            }:
                return False
            if owner_token is not None and row["owner_token"] != owner_token:
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
            if row is None or row["status"] in {
                EffectStatus.CONFIRMED.value,
                EffectStatus.REJECTED.value,
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


class EffectExecutor:
    """Execute a contracted external effect and preserve uncertainty on failure."""

    def __init__(self, *, ledger: EffectLedger, lease_seconds: float = 30.0) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.ledger = ledger
        self.lease_seconds = lease_seconds

    def execute(
        self,
        *,
        contract: EffectContract,
        arguments: Mapping[str, Any],
        handler: Callable[..., Any],
        handler_timeout_seconds: float | None = None,
    ) -> EffectResult:
        if handler_timeout_seconds is not None and handler_timeout_seconds <= 0:
            raise ValueError("handler_timeout_seconds must be positive")
        try:
            canonical = _json_dumps(dict(arguments))
        except (TypeError, ValueError) as exc:
            raise ValueError("effect arguments must be finite JSON-compatible data") from exc

        # Treat the canonical request as the immutable source of truth. User callbacks
        # and handlers receive fresh decoded copies so they cannot mutate later probes.
        operation_id = contract.operation_id(json.loads(canonical))
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("contract operation_id must return a non-empty string")

        provider_key = (
            contract.provider_idempotency_key(json.loads(canonical))
            if contract.provider_idempotency_key is not None
            else None
        )
        if provider_key is not None and (
            not isinstance(provider_key, str) or not provider_key.strip()
        ):
            raise ValueError("provider idempotency key must be a non-empty string or None")

        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.ledger.prepare(
            operation_id,
            contract.name,
            contract.version,
            fingerprint,
            provider_key,
        )

        # One immediate retry is allowed after authoritative proof of non-execution
        # or when the provider contract guarantees deduplication for the stable key.
        for dispatch_round in range(2):
            record = self.ledger.get(operation_id)
            assert record is not None

            if record.status is EffectStatus.CONFIRMED:
                return self._result(record, duplicate_suppressed=True)
            if record.status is EffectStatus.REJECTED:
                return self._result(record)
            if record.status is EffectStatus.DISPATCHED:
                if record.lease_until is not None and record.lease_until > time.time():
                    return self._result(record, in_progress=True)
                self.ledger.mark_outcome_unknown(
                    operation_id,
                    "dispatch lease expired; external outcome must be reconciled",
                    owner_token=record.owner_token,
                )
                record = self.ledger.get(operation_id)
                assert record is not None
                if (
                    record.status is EffectStatus.DISPATCHED
                    and record.lease_until is not None
                    and record.lease_until > time.time()
                ):
                    return self._result(record, in_progress=True)

            if record.status in {EffectStatus.OUTCOME_UNKNOWN, EffectStatus.QUARANTINED}:
                reconciled = self._reconcile(
                    contract=contract,
                    arguments=json.loads(canonical),
                    operation_id=operation_id,
                    allow_idempotent_retry=True,
                )
                if reconciled is not None:
                    return reconciled
                record = self.ledger.get(operation_id)
                assert record is not None

            if (
                record.status is EffectStatus.DISPATCHED
                and record.lease_until is not None
                and record.lease_until > time.time()
            ):
                return self._result(record, in_progress=True)
            if record.status is not EffectStatus.PREPARED:
                return self._result(record)

            owner_token = uuid.uuid4().hex
            if not self.ledger.claim_dispatch(
                operation_id, owner_token, self.lease_seconds
            ):
                current = self.ledger.get(operation_id)
                assert current is not None
                if (
                    current.status is EffectStatus.DISPATCHED
                    and current.lease_until is not None
                    and current.lease_until > time.time()
                ):
                    return self._result(current, in_progress=True)
                continue

            try:
                output = self._invoke_handler(
                    handler,
                    json.loads(canonical),
                    provider_key,
                    timeout_seconds=handler_timeout_seconds,
                )
            except EffectRejected as exc:
                self.ledger.mark_rejected(
                    operation_id,
                    f"{type(exc).__name__}: provider definitively rejected the effect",
                    owner_token=owner_token,
                )
                current = self.ledger.get(operation_id)
                assert current is not None
                return self._result(current)
            except Exception as exc:
                self.ledger.mark_outcome_unknown(
                    operation_id,
                    f"{type(exc).__name__}: handler raised after dispatch; outcome may be unknown",
                    owner_token=owner_token,
                )
                reconciled = self._reconcile(
                    contract=contract,
                    arguments=json.loads(canonical),
                    operation_id=operation_id,
                    allow_idempotent_retry=True,
                )
                if reconciled is not None:
                    return reconciled
                if dispatch_round == 0:
                    continue
                current = self.ledger.get(operation_id)
                assert current is not None
                return self._result(current)

            if contract.postcondition is not None:
                try:
                    condition_met = contract.postcondition(json.loads(canonical), output)
                except Exception as exc:
                    condition_met = False
                    postcondition_error = (
                        f"{type(exc).__name__}: postcondition evaluation failed"
                    )
                else:
                    postcondition_error = "postcondition was not satisfied"
                if not condition_met:
                    self.ledger.mark_outcome_unknown(
                        operation_id,
                        postcondition_error,
                        owner_token=owner_token,
                    )
                    reconciled = self._reconcile(
                        contract=contract,
                        arguments=json.loads(canonical),
                        operation_id=operation_id,
                        allow_idempotent_retry=False,
                    )
                    if reconciled is not None:
                        return reconciled
                    current = self.ledger.get(operation_id)
                    assert current is not None
                    return self._result(current)

            try:
                confirmed = self.ledger.mark_confirmed(
                    operation_id,
                    output,
                    evidence={"source": "handler_return"},
                    owner_token=owner_token,
                )
            except (TypeError, ValueError):
                self.ledger.mark_outcome_unknown(
                    operation_id,
                    "handler returned a result that cannot be persisted as JSON",
                    owner_token=owner_token,
                )
                reconciled = self._reconcile(
                    contract=contract,
                    arguments=json.loads(canonical),
                    operation_id=operation_id,
                    allow_idempotent_retry=False,
                )
                if reconciled is not None:
                    return reconciled
                current = self.ledger.get(operation_id)
                assert current is not None
                return self._result(current)

            current = self.ledger.get(operation_id)
            assert current is not None
            if not confirmed:
                return self._result(current)
            return self._result(current)

        record = self.ledger.get(operation_id)
        assert record is not None
        return self._result(record)

    @staticmethod
    def _invoke_handler(
        handler: Callable[..., Any],
        arguments: Mapping[str, Any],
        provider_key: str | None,
        *,
        timeout_seconds: float | None,
    ) -> Any:
        if timeout_seconds is None:
            return handler(arguments, idempotency_key=provider_key)

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(handler, arguments, idempotency_key=provider_key)
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            # Cancelling a running Python thread cannot stop a remote side effect.
            # Preserve uncertainty; the provider contract must settle the outcome.
            future.cancel()
            raise TimeoutError(
                "effect handler timed out; external work may still be running"
            ) from exc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _reconcile(
        self,
        *,
        contract: EffectContract,
        arguments: Mapping[str, Any],
        operation_id: str,
        allow_idempotent_retry: bool,
    ) -> EffectResult | None:
        """Return a result if execution must stop; None means retry was authorized."""
        record = self.ledger.get(operation_id)
        assert record is not None
        try:
            canonical_arguments = _json_dumps(dict(arguments))
        except (TypeError, ValueError) as exc:
            raise ValueError("reconciliation arguments must be finite JSON-compatible data") from exc
        if record.status is EffectStatus.CONFIRMED:
            return self._result(record, duplicate_suppressed=True)
        if record.status is EffectStatus.REJECTED:
            return self._result(record)

        if contract.outcome_probe is not None:
            try:
                probe = contract.outcome_probe(
                    json.loads(canonical_arguments),
                    idempotency_key=record.provider_idempotency_key,
                )
                if not isinstance(probe, OutcomeProbeResult):
                    raise TypeError("outcome_probe must return OutcomeProbeResult")
            except Exception as exc:
                message = (
                    f"{type(exc).__name__}: outcome probe failed; external outcome remains unknown"
                )
                if allow_idempotent_retry and contract.provider_idempotency_guaranteed:
                    # A documented provider idempotency contract is sufficient to retry
                    # with the same key even when the reconciliation query is unavailable.
                    self.ledger.reset_for_retry(
                        operation_id,
                        evidence={
                            "retry_basis": "provider idempotency contract after probe failure",
                            "provider_idempotency_key": record.provider_idempotency_key,
                        },
                    )
                    return None
                if contract.unknown_outcome is UnknownOutcomePolicy.QUARANTINE:
                    self.ledger.mark_quarantined(operation_id, message)
                else:
                    self.ledger.mark_outcome_unknown(operation_id, message)
                current = self.ledger.get(operation_id)
                assert current is not None
                return self._result(current)

            evidence = dict(probe.evidence)
            evidence["probe_status"] = probe.status.value

            if probe.status is ProbeStatus.COMMITTED:
                if contract.postcondition is not None:
                    try:
                        condition_met = contract.postcondition(
                            json.loads(canonical_arguments), probe.output
                        )
                    except Exception:
                        condition_met = False
                    if not condition_met:
                        self.ledger.mark_outcome_unknown(
                            operation_id,
                            "probe found a commit but the required postcondition was not satisfied",
                        )
                        current = self.ledger.get(operation_id)
                        assert current is not None
                        return self._result(current)
                try:
                    self.ledger.mark_confirmed(
                        operation_id,
                        probe.output,
                        provider_operation_id=probe.provider_operation_id,
                        evidence=evidence,
                    )
                except (TypeError, ValueError):
                    self.ledger.mark_outcome_unknown(
                        operation_id,
                        "probe confirmed a commit but its result cannot be persisted as JSON",
                    )
                current = self.ledger.get(operation_id)
                assert current is not None
                return self._result(current, duplicate_suppressed=True)

            if probe.status is ProbeStatus.NOT_COMMITTED:
                # This status is a contract assertion: the provider has established
                # that the prior request is terminal and can no longer commit later.
                self.ledger.reset_for_retry(operation_id, evidence=evidence)
                return None

            if probe.status is ProbeStatus.PENDING:
                self.ledger.mark_outcome_unknown(
                    operation_id,
                    "provider reports that the external operation is still pending",
                )
                current = self.ledger.get(operation_id)
                assert current is not None
                return self._result(current)

            message = "provider could not establish the external operation outcome"
            if allow_idempotent_retry and contract.provider_idempotency_guaranteed:
                self.ledger.reset_for_retry(
                    operation_id,
                    evidence={
                        **evidence,
                        "retry_basis": "provider idempotency contract",
                        "provider_idempotency_key": record.provider_idempotency_key,
                    },
                )
                return None
            if contract.unknown_outcome is UnknownOutcomePolicy.QUARANTINE:
                self.ledger.mark_quarantined(operation_id, message, evidence=evidence)
            else:
                self.ledger.mark_outcome_unknown(operation_id, message)
            current = self.ledger.get(operation_id)
            assert current is not None
            return self._result(current)

        if allow_idempotent_retry and contract.provider_idempotency_guaranteed:
            self.ledger.reset_for_retry(
                operation_id,
                evidence={
                    "retry_basis": "provider idempotency contract",
                    "provider_idempotency_key": record.provider_idempotency_key,
                },
            )
            return None

        message = "no authoritative outcome probe or provider idempotency guarantee is available"
        if contract.unknown_outcome is UnknownOutcomePolicy.QUARANTINE:
            self.ledger.mark_quarantined(operation_id, message)
        else:
            self.ledger.mark_outcome_unknown(operation_id, message)
        current = self.ledger.get(operation_id)
        assert current is not None
        return self._result(current)

    def list_operations(
        self, status: EffectStatus | None = None, limit: int = 100
    ) -> list[EffectRecord]:
        return self.ledger.list_operations(status=status, limit=limit)

    @staticmethod
    def _validate_manual_resolution(actor: str, reason: str, evidence_reference: str) -> dict[str, str]:
        values = {
            "actor": actor,
            "reason": reason,
            "evidence_reference": evidence_reference,
        }
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise ValueError("actor, reason, and evidence_reference must be non-empty strings")
        return values

    def resolve_manually_confirmed(
        self,
        operation_id: str,
        output: Any,
        *,
        actor: str,
        reason: str,
        evidence_reference: str,
        provider_operation_id: str | None = None,
    ) -> EffectResult:
        """Resolve an uncertain operation after trusted, independent confirmation."""
        evidence = self._validate_manual_resolution(actor, reason, evidence_reference)
        evidence["resolution"] = "manual_confirmation"
        if not self.ledger.resolve_manually_confirmed(
            operation_id,
            output,
            provider_operation_id=provider_operation_id,
            evidence=evidence,
        ):
            raise EffectError(
                "manual confirmation is allowed only for an existing unknown or quarantined operation"
            )
        record = self.ledger.get(operation_id)
        assert record is not None
        return self._result(record, duplicate_suppressed=True)

    def authorize_manual_retry(
        self,
        operation_id: str,
        *,
        actor: str,
        reason: str,
        evidence_reference: str,
    ) -> EffectRecord:
        """Authorize retry only after a trusted operator establishes terminal non-execution."""
        evidence = self._validate_manual_resolution(actor, reason, evidence_reference)
        evidence["resolution"] = "manual_retry_authorization"
        if not self.ledger.authorize_manual_retry(operation_id, evidence=evidence):
            raise EffectError(
                "manual retry is allowed only for an existing unknown or quarantined operation"
            )
        record = self.ledger.get(operation_id)
        assert record is not None
        return record

    def resolve_manually_rejected(
        self,
        operation_id: str,
        *,
        actor: str,
        reason: str,
        evidence_reference: str,
    ) -> EffectResult:
        """Record trusted evidence that an uncertain operation did not take effect."""
        evidence = self._validate_manual_resolution(actor, reason, evidence_reference)
        evidence["resolution"] = "manual_rejection"
        if not self.ledger.resolve_manually_rejected(
            operation_id,
            reason=reason,
            evidence=evidence,
        ):
            raise EffectError(
                "manual rejection is allowed only for an existing unknown or quarantined operation"
            )
        record = self.ledger.get(operation_id)
        assert record is not None
        return self._result(record)

    @staticmethod
    def _result(
        record: EffectRecord,
        *,
        duplicate_suppressed: bool = False,
        in_progress: bool = False,
    ) -> EffectResult:
        return EffectResult(
            operation_id=record.operation_id,
            status=record.status,
            output=record.output,
            error=record.error,
            attempts=record.attempts,
            duplicate_suppressed=duplicate_suppressed,
            in_progress=in_progress,
            provider_operation_id=record.provider_operation_id,
            evidence=record.evidence,
        )
