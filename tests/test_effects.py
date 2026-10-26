from __future__ import annotations

import pytest

from production_agent_systems.effects import (
    EffectContract,
    EffectExecutor,
    EffectStatus,
    OperationConflictError,
    OutcomeProbeResult,
    ProbeStatus,
    SQLiteEffectLedger,
    UnknownOutcomePolicy,
)
from production_agent_systems.testkit import (
    FaultInjectingPaymentProvider,
    FaultPoint,
    run_scenario,
)


def payment_contract(provider: FaultInjectingPaymentProvider) -> EffectContract:
    return EffectContract(
        name="capture_payment",
        operation_id=lambda args: f"invoice:{args['invoice_id']}:capture:v1",
        provider_idempotency_key=lambda args: f"capture:{args['invoice_id']}",
        provider_idempotency_guaranteed=provider.supports_idempotency,
        outcome_probe=provider.probe,
    )


def test_commit_then_lost_ack_is_reconciled_without_a_duplicate(tmp_path):
    provider = FaultInjectingPaymentProvider(
        fault_point=FaultPoint.AFTER_COMMIT_BEFORE_ACK
    )
    ledger = SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    executor = EffectExecutor(ledger=ledger)

    result = executor.execute(
        contract=payment_contract(provider),
        arguments={"invoice_id": "INV-100"},
        handler=provider.capture,
    )

    assert result.status is EffectStatus.CONFIRMED
    assert result.output["payment_id"] == "pay-000001"
    assert len(provider.committed_effects) == 1
    assert result.attempts == 1
    assert result.duplicate_suppressed is True
    assert [event["event_type"] for event in ledger.events(result.operation_id)] == [
        "prepared",
        "dispatched",
        "outcome_unknown",
        "confirmed",
    ]


def test_authoritative_non_commit_allows_one_safe_retry(tmp_path):
    provider = FaultInjectingPaymentProvider(fault_point=FaultPoint.BEFORE_COMMIT)
    ledger = SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    executor = EffectExecutor(ledger=ledger)

    result = executor.execute(
        contract=payment_contract(provider),
        arguments={"invoice_id": "INV-101"},
        handler=provider.capture,
    )

    assert result.status is EffectStatus.CONFIRMED
    assert result.attempts == 2
    assert len(provider.committed_effects) == 1


def test_unknown_mutation_without_probe_or_idempotency_is_quarantined(tmp_path):
    calls: list[str] = []

    contract = EffectContract(
        name="send_email",
        operation_id=lambda args: f"email:{args['message_id']}:send:v1",
    )

    def handler(arguments, *, idempotency_key):
        calls.append(arguments["message_id"])
        raise TimeoutError("acknowledgement lost")

    ledger = SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    executor = EffectExecutor(ledger=ledger)
    first = executor.execute(
        contract=contract,
        arguments={"message_id": "MSG-1"},
        handler=handler,
    )
    second = executor.execute(
        contract=contract,
        arguments={"message_id": "MSG-1"},
        handler=handler,
    )

    assert first.status is EffectStatus.QUARANTINED
    assert second.status is EffectStatus.QUARANTINED
    assert len(calls) == 1


def test_same_operation_id_with_different_request_is_rejected(tmp_path):
    ledger = SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    executor = EffectExecutor(ledger=ledger)
    contract = EffectContract(
        name="capture_payment",
        operation_id=lambda args: "invoice:INV-200:capture:v1",
    )
    calls: list[dict] = []

    executor.execute(
        contract=contract,
        arguments={"invoice_id": "INV-200", "amount": 10},
        handler=lambda arguments, *, idempotency_key: calls.append(arguments) or {"ok": True},
    )

    with pytest.raises(OperationConflictError):
        executor.execute(
            contract=contract,
            arguments={"invoice_id": "INV-200", "amount": 1000},
            handler=lambda arguments, *, idempotency_key: calls.append(arguments) or {"ok": True},
        )

    assert len(calls) == 1


def test_confirmed_result_is_replayed_after_ledger_reopen(tmp_path):
    database = tmp_path / "effects.sqlite3"
    provider = FaultInjectingPaymentProvider()
    contract = payment_contract(provider)

    first = EffectExecutor(ledger=SQLiteEffectLedger(database)).execute(
        contract=contract,
        arguments={"invoice_id": "INV-300"},
        handler=provider.capture,
    )
    second = EffectExecutor(ledger=SQLiteEffectLedger(database)).execute(
        contract=contract,
        arguments={"invoice_id": "INV-300"},
        handler=provider.capture,
    )

    assert first.status is EffectStatus.CONFIRMED
    assert second.status is EffectStatus.CONFIRMED
    assert second.duplicate_suppressed is True
    assert second.output == first.output
    assert len(provider.committed_effects) == 1


def test_pending_probe_preserves_unknown_outcome(tmp_path):
    calls: list[int] = []

    def handler(arguments, *, idempotency_key):
        calls.append(1)
        raise TimeoutError

    contract = EffectContract(
        name="provision_resource",
        operation_id=lambda args: f"resource:{args['resource_id']}:create:v1",
        outcome_probe=lambda arguments, *, idempotency_key: OutcomeProbeResult(
            status=ProbeStatus.PENDING,
            evidence={"provider_state": "processing"},
        ),
        unknown_outcome=UnknownOutcomePolicy.KEEP_UNKNOWN,
    )
    result = EffectExecutor(
        ledger=SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    ).execute(
        contract=contract,
        arguments={"resource_id": "R-9"},
        handler=handler,
    )

    assert result.status is EffectStatus.OUTCOME_UNKNOWN
    assert len(calls) == 1


@pytest.mark.parametrize(
    "scenario",
    ["after-commit-before-ack", "before-commit"],
)
def test_cli_scenarios_assert_one_effect(scenario):
    report = run_scenario(scenario)

    assert report["passed"] is True
    assert report["confirmed"] is True
    assert report["committed_effect_count"] == 1
    assert report["duplicate_effect_detected"] is False
