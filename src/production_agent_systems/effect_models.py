from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol


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
        self,
        operation_id: str,
        error: str,
        *,
        owner_token: str | None = None,
        evidence: Mapping[str, Any] | None = None,
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
