from __future__ import annotations

import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Callable, Mapping

from .effect_models import (
    EffectContract,
    EffectError,
    EffectLedger,
    EffectRecord,
    EffectRejected,
    EffectResult,
    EffectStatus,
    OutcomeProbeResult,
    ProbeStatus,
    UnknownOutcomePolicy,
    _json_dumps,
)


class _HandlerDeadlineExceeded(TimeoutError):
    """A local wait timed out; the underlying handler thread may still be running."""


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
        if contract.provider_idempotency_guaranteed and provider_key is None:
            raise ValueError(
                "contract declared provider idempotency guaranteed but produced no key"
            )

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
                    evidence={
                        "handler_may_still_be_running": True,
                        "dispatch_interruption": "lease_expired",
                    },
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
            except _HandlerDeadlineExceeded as exc:
                self.ledger.mark_outcome_unknown(
                    operation_id,
                    f"{type(exc).__name__}: handler wait timed out; the handler may still be running",
                    owner_token=owner_token,
                    evidence={
                        "handler_may_still_be_running": True,
                        "dispatch_interruption": "handler_timeout",
                    },
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
            except Exception as exc:
                self.ledger.mark_outcome_unknown(
                    operation_id,
                    f"{type(exc).__name__}: handler raised after dispatch; outcome may be unknown",
                    owner_token=owner_token,
                    evidence={"dispatch_interruption": "handler_exception"},
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
            raise _HandlerDeadlineExceeded(
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
        if record.status is EffectStatus.DISPATCHED:
            if record.lease_until is not None and record.lease_until > time.time():
                return self._result(record, in_progress=True)
            self.ledger.mark_outcome_unknown(
                operation_id,
                "dispatch lease expired during reconciliation; outcome must be rechecked",
                owner_token=record.owner_token,
                evidence={
                    "handler_may_still_be_running": True,
                    "dispatch_interruption": "lease_expired",
                },
            )
            record = self.ledger.get(operation_id)
            assert record is not None
            if record.status is EffectStatus.DISPATCHED:
                return self._result(
                    record,
                    in_progress=record.lease_until is not None
                    and record.lease_until > time.time(),
                )
        if record.status not in {
            EffectStatus.OUTCOME_UNKNOWN,
            EffectStatus.QUARANTINED,
        }:
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
                            "provider_idempotency_key_present": record.provider_idempotency_key is not None,
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
                        self.ledger.mark_quarantined(
                            operation_id,
                            "probe found a commit but the required postcondition was not satisfied",
                            evidence={
                                **evidence,
                                "postcondition": "failed",
                            },
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
                if (
                    current.status is EffectStatus.DISPATCHED
                    and current.lease_until is not None
                    and current.lease_until > time.time()
                ):
                    return self._result(current, in_progress=True)
                return self._result(
                    current,
                    duplicate_suppressed=current.status is EffectStatus.CONFIRMED,
                )

            if probe.status is ProbeStatus.NOT_COMMITTED:
                # Even a terminal provider lookup cannot prove that a timed-out local
                # handler will not submit its request later. Only a provider idempotency
                # guarantee makes that overlapping retry safe.
                if (
                    record.evidence.get("handler_may_still_be_running") is True
                    and not (
                        allow_idempotent_retry
                        and contract.provider_idempotency_guaranteed
                    )
                ):
                    message = (
                        "provider reports no committed effect, but a previous handler "
                        "may still dispatch; automatic retry is blocked"
                    )
                    if contract.unknown_outcome is UnknownOutcomePolicy.QUARANTINE:
                        self.ledger.mark_quarantined(
                            operation_id,
                            message,
                            evidence={
                                **evidence,
                                "handler_may_still_be_running": True,
                            },
                        )
                    else:
                        self.ledger.mark_outcome_unknown(
                            operation_id,
                            message,
                            evidence={
                                **evidence,
                                "handler_may_still_be_running": True,
                            },
                        )
                    current = self.ledger.get(operation_id)
                    assert current is not None
                    return self._result(current)
                # Here either the previous handler is known to have exited, or the
                # provider's idempotency guarantee makes an overlapping dispatch safe.
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
                        "provider_idempotency_key_present": record.provider_idempotency_key is not None,
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
                    "provider_idempotency_key_present": record.provider_idempotency_key is not None,
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
    def _validate_manual_resolution(actor: str, reason: str, evidence_reference: str) -> dict[str, Any]:
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
        # The trusted operator is asserting that no earlier handler can still dispatch
        # and the previous request is terminally known not to have taken effect.
        evidence["handler_may_still_be_running"] = False
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
