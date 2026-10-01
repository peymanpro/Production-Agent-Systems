from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class AuditRecord:
    run_id: str
    action: str
    resource: str
    outcome: str
    details: dict[str, Any]
    timestamp: str

    @classmethod
    def create(
        cls,
        run_id: str,
        action: str,
        resource: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> AuditRecord:
        return cls(
            run_id=run_id,
            action=action,
            resource=resource,
            outcome=outcome,
            details=details or {},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


class AuditSink:
    def append(self, record: AuditRecord) -> None:
        raise NotImplementedError

    def records(self, run_id: str | None = None) -> list[AuditRecord]:
        raise NotImplementedError


class InMemoryAuditSink(AuditSink):
    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self._records.append(record)

    def records(self, run_id: str | None = None) -> list[AuditRecord]:
        if run_id is None:
            return list(self._records)
        return [record for record in self._records if record.run_id == run_id]
