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

def test_provider_idempotency_key_allows_safe_replay_without_probe(tmp_path):
    provider = FaultInjectingPaymentProvider(
        fault_point=FaultPoint.AFTER_COMMIT_BEFORE_ACK
    )
    base = payment_contract(provider)
    contract = EffectContract(
        name=base.name,
        operation_id=base.operation_id,
        provider_idempotency_key=base.provider_idempotency_key,
        provider_idempotency_guaranteed=True,
    )
    result = EffectExecutor(
        ledger=SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    ).execute(
        contract=contract,
        arguments={"invoice_id": "INV-400"},
        handler=provider.capture,
    )

    assert result.status is EffectStatus.CONFIRMED
    assert result.attempts == 2
    assert len(provider.committed_effects) == 1


def test_active_dispatch_lease_prevents_a_competing_handler(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered = Event()
    release = Event()
    calls: list[str] = []

    def handler(arguments, *, idempotency_key):
        calls.append(arguments["resource_id"])
        entered.set()
        assert release.wait(timeout=3)
        return {"resource_id": arguments["resource_id"], "created": True}

    contract = EffectContract(
        name="create_resource",
        operation_id=lambda args: f"resource:{args['resource_id']}:create:v1",
    )
    ledger = SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    executor = EffectExecutor(ledger=ledger, lease_seconds=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            executor.execute,
            contract=contract,
            arguments={"resource_id": "R-10"},
            handler=handler,
        )
        assert entered.wait(timeout=2)
        second = executor.execute(
            contract=contract,
            arguments={"resource_id": "R-10"},
            handler=handler,
        )
        assert second.status is EffectStatus.DISPATCHED
        assert second.in_progress is True
        assert len(calls) == 1
        release.set()
        first = first_future.result(timeout=3)

    assert first.status is EffectStatus.CONFIRMED
    assert len(calls) == 1

def test_agent_runtime_waits_on_uncertain_effect_then_resumes_after_reconciliation(tmp_path):
    from production_agent_systems import (
        AgentAction,
        AgentRuntime,
        CheckpointStore,
        DeterministicPlanner,
        InMemoryEventSink,
        InMemoryStateStore,
        PermissionPolicy,
        RunStatus,
        Task,
        ToolDefinition,
        ToolEffect,
        ToolRegistry,
    )

    provider = FaultInjectingPaymentProvider(
        fault_point=FaultPoint.AFTER_COMMIT_BEFORE_ACK
    )
    probe_calls = 0

    def delayed_probe(arguments, *, idempotency_key):
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            return OutcomeProbeResult(
                status=ProbeStatus.UNKNOWN,
                evidence={"reason": "provider lookup temporarily unavailable"},
            )
        return provider.probe(arguments, idempotency_key=idempotency_key)

    contract = EffectContract(
        name="capture_payment",
        operation_id=lambda args: f"invoice:{args['invoice_id']}:capture:v1",
        provider_idempotency_key=lambda args: f"capture:{args['invoice_id']}",
        provider_idempotency_guaranteed=True,
        outcome_probe=delayed_probe,
    )
    tool = ToolDefinition(
        name="capture_payment",
        description="Capture a payment",
        input_schema={
            "required": ["invoice_id"],
            "properties": {"invoice_id": {"type": "string"}},
        },
        effect=ToolEffect.MUTATION,
        version="1",
        handler=provider.capture,
        effect_contract=contract,
    )
    registry = ToolRegistry([tool])
    planner = DeterministicPlanner(
        [
            AgentAction.call_tool("capture_payment", {"invoice_id": "INV-RUNTIME-1"}),
            AgentAction.finish("payment handled"),
        ]
    )
    runtime = AgentRuntime(
        planner=planner,
        registry=registry,
        policy=PermissionPolicy(
            frozenset({"capture_payment"}),
            mutation_requires_confirmation=False,
        ),
        state_store=InMemoryStateStore(),
        checkpoint_store=CheckpointStore(),
        event_sink=InMemoryEventSink(),
        effect_executor=EffectExecutor(
            ledger=SQLiteEffectLedger(tmp_path / "runtime-effects.sqlite3")
        ),
    )

    first = runtime.run(Task("runtime-effect-1", "capture payment"))
    assert first.status is RunStatus.WAITING
    assert first.observations[0].result.effect_status == EffectStatus.QUARANTINED.value

    resumed = runtime.run(
        Task("runtime-effect-1", "capture payment"),
        resume=True,
    )

    assert resumed.status is RunStatus.COMPLETED
    assert len(provider.committed_effects) == 1
    assert probe_calls == 2

def test_handler_timeout_preserves_uncertainty_until_late_effect_is_observed(tmp_path):
    from threading import Event

    entered = Event()
    release = Event()
    committed: list[dict[str, str]] = []

    def handler(arguments, *, idempotency_key):
        entered.set()
        assert release.wait(timeout=3)
        result = {"resource_id": arguments["resource_id"], "status": "created"}
        committed.append(result)
        return result

    def probe(arguments, *, idempotency_key):
        if committed:
            return OutcomeProbeResult(
                status=ProbeStatus.COMMITTED,
                output=committed[0],
                evidence={"source": "test provider"},
            )
        return OutcomeProbeResult(
            status=ProbeStatus.UNKNOWN,
            evidence={"reason": "handler may still be running"},
        )

    contract = EffectContract(
        name="create_resource",
        operation_id=lambda args: f"resource:{args['resource_id']}:create:v1",
        outcome_probe=probe,
    )
    executor = EffectExecutor(
        ledger=SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    )

    first = executor.execute(
        contract=contract,
        arguments={"resource_id": "R-LATE"},
        handler=handler,
        handler_timeout_seconds=0.01,
    )
    assert entered.is_set()
    assert first.status is EffectStatus.QUARANTINED

    release.set()
    from time import sleep

    deadline = __import__("time").monotonic() + 2
    while not committed and __import__("time").monotonic() < deadline:
        sleep(0.01)
    assert len(committed) == 1

    recovered = executor.execute(
        contract=contract,
        arguments={"resource_id": "R-LATE"},
        handler=handler,
        handler_timeout_seconds=0.01,
    )
    assert recovered.status is EffectStatus.CONFIRMED
    assert len(committed) == 1

def test_manual_confirmation_is_audited_and_prevents_reexecution(tmp_path):
    provider = FaultInjectingPaymentProvider(
        fault_point=FaultPoint.AFTER_COMMIT_BEFORE_ACK
    )
    contract = EffectContract(
        name="capture_payment",
        operation_id=lambda args: f"invoice:{args['invoice_id']}:capture:v1",
    )
    ledger = SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    executor = EffectExecutor(ledger=ledger)
    arguments = {"invoice_id": "INV-MANUAL-1"}

    first = executor.execute(
        contract=contract,
        arguments=arguments,
        handler=provider.capture,
    )
    assert first.status is EffectStatus.QUARANTINED
    assert len(provider.committed_effects) == 1
    quarantined = ledger.list_operations(status=EffectStatus.QUARANTINED)
    assert len(quarantined) == 1

    resolved = executor.resolve_manually_confirmed(
        first.operation_id,
        provider.committed_effects[0],
        actor="on-call-operator",
        reason="Verified the capture in the provider dashboard",
        evidence_reference="INC-1042/provider-event-1",
        provider_operation_id="pay-000001",
    )
    replay = executor.execute(
        contract=contract,
        arguments=arguments,
        handler=provider.capture,
    )

    assert resolved.status is EffectStatus.CONFIRMED
    assert replay.status is EffectStatus.CONFIRMED
    assert replay.duplicate_suppressed is True
    assert len(provider.committed_effects) == 1
    assert any(
        event["event_type"] == "manually_confirmed"
        for event in ledger.events(first.operation_id)
    )


def test_manual_retry_requires_explicit_evidence_and_only_retries_uncertain_records(tmp_path):
    calls = 0

    def handler(arguments, *, idempotency_key):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("injected failure before commit")
        return {"message_id": arguments["message_id"], "sent": True}

    contract = EffectContract(
        name="send_email",
        operation_id=lambda args: f"email:{args['message_id']}:send:v1",
    )
    ledger = SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    executor = EffectExecutor(ledger=ledger)
    arguments = {"message_id": "MSG-MANUAL-1"}
    first = executor.execute(contract=contract, arguments=arguments, handler=handler)

    assert first.status is EffectStatus.QUARANTINED
    prepared = executor.authorize_manual_retry(
        first.operation_id,
        actor="on-call-operator",
        reason="Provider confirms the prior request is terminal and was not accepted",
        evidence_reference="INC-2048/provider-query-2",
    )
    assert prepared.status is EffectStatus.PREPARED

    second = executor.execute(contract=contract, arguments=arguments, handler=handler)
    assert second.status is EffectStatus.CONFIRMED
    assert calls == 2
    assert any(
        event["event_type"] == "manual_retry_authorized"
        for event in ledger.events(first.operation_id)
    )


def test_manual_resolution_requires_a_recorded_actor_reason_and_evidence(tmp_path):
    contract = EffectContract(
        name="mutate",
        operation_id=lambda args: f"resource:{args['id']}:update:v1",
    )
    executor = EffectExecutor(
        ledger=SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    )
    result = executor.execute(
        contract=contract,
        arguments={"id": "42"},
        handler=lambda arguments, *, idempotency_key: (_ for _ in ()).throw(TimeoutError()),
    )

    with pytest.raises(ValueError):
        executor.authorize_manual_retry(
            result.operation_id,
            actor="",
            reason="Checked",
            evidence_reference="INC-1",
        )

def test_provider_idempotency_key_allows_retry_when_probe_is_unknown(tmp_path):
    provider = FaultInjectingPaymentProvider(
        fault_point=FaultPoint.AFTER_COMMIT_BEFORE_ACK
    )
    probe_calls = 0

    def uncertain_probe(arguments, *, idempotency_key):
        nonlocal probe_calls
        probe_calls += 1
        return OutcomeProbeResult(
            status=ProbeStatus.UNKNOWN,
            evidence={"source": "provider query", "query_attempt": probe_calls},
        )

    contract = EffectContract(
        name="capture_payment",
        operation_id=lambda args: f"invoice:{args['invoice_id']}:capture:v1",
        provider_idempotency_key=lambda args: f"capture:{args['invoice_id']}",
        provider_idempotency_guaranteed=True,
        outcome_probe=uncertain_probe,
    )
    result = EffectExecutor(
        ledger=SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    ).execute(
        contract=contract,
        arguments={"invoice_id": "INV-UNKNOWN-PROBE"},
        handler=provider.capture,
    )

    assert result.status is EffectStatus.CONFIRMED
    assert result.attempts == 2
    assert len(provider.committed_effects) == 1


def test_provider_idempotency_key_allows_retry_when_probe_raises(tmp_path):
    provider = FaultInjectingPaymentProvider(
        fault_point=FaultPoint.AFTER_COMMIT_BEFORE_ACK
    )
    contract = EffectContract(
        name="capture_payment",
        operation_id=lambda args: f"invoice:{args['invoice_id']}:capture:v1",
        provider_idempotency_key=lambda args: f"capture:{args['invoice_id']}",
        provider_idempotency_guaranteed=True,
        outcome_probe=lambda arguments, *, idempotency_key: (
            _raise_probe_error()
        ),
    )
    result = EffectExecutor(
        ledger=SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    ).execute(
        contract=contract,
        arguments={"invoice_id": "INV-PROBE-ERROR"},
        handler=provider.capture,
    )

    assert result.status is EffectStatus.CONFIRMED
    assert result.attempts == 2
    assert len(provider.committed_effects) == 1


def _raise_probe_error():
    raise ConnectionError("provider outcome endpoint unavailable")

def test_changed_contract_version_conflicts_with_an_existing_operation(tmp_path):
    ledger = SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    executor = EffectExecutor(ledger=ledger)
    args = {"invoice_id": "INV-VERSION-1"}
    first_contract = EffectContract(
        name="capture_payment",
        operation_id=lambda values: f"invoice:{values['invoice_id']}:capture",
        version="1",
    )
    second_contract = EffectContract(
        name="capture_payment",
        operation_id=lambda values: f"invoice:{values['invoice_id']}:capture",
        version="2",
    )

    executor.execute(
        contract=first_contract,
        arguments=args,
        handler=lambda arguments, *, idempotency_key: {"captured": True},
    )

    with pytest.raises(OperationConflictError, match="contract version"):
        executor.execute(
            contract=second_contract,
            arguments=args,
            handler=lambda arguments, *, idempotency_key: {"captured": True},
        )


def test_sqlite_ledger_migrates_legacy_rows_to_contract_version_one(tmp_path):
    import sqlite3

    database = tmp_path / "legacy-effects.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE effect_operations (
                operation_id TEXT PRIMARY KEY,
                contract_name TEXT NOT NULL,
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
            )
            """
        )
        connection.execute(
            """
            INSERT INTO effect_operations(
                operation_id, contract_name, request_fingerprint,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-operation",
                "legacy-contract",
                "old-fingerprint",
                EffectStatus.QUARANTINED.value,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    ledger = SQLiteEffectLedger(database)
    record = ledger.get("legacy-operation")

    assert record is not None
    assert record.contract_name == "legacy-contract"
    assert record.contract_version == "1"
    columns = {
        row["name"]
        for row in sqlite3.connect(database).execute("PRAGMA table_info(effect_operations)")
    }
    assert "contract_version" in columns

def test_handler_mutation_does_not_change_reconciliation_arguments(tmp_path):
    observed_probe_ids: list[str] = []

    def handler(arguments, *, idempotency_key):
        arguments["resource"]["id"] = "MUTATED-BY-HANDLER"
        raise TimeoutError("acknowledgement lost after remote commit")

    def probe(arguments, *, idempotency_key):
        resource_id = arguments["resource"]["id"]
        observed_probe_ids.append(resource_id)
        if resource_id == "R-ORIGINAL":
            return OutcomeProbeResult(
                status=ProbeStatus.COMMITTED,
                output={"resource_id": resource_id, "created": True},
                evidence={"source": "fake provider"},
            )
        return OutcomeProbeResult(
            status=ProbeStatus.UNKNOWN,
            evidence={"observed_resource_id": resource_id},
        )

    contract = EffectContract(
        name="create_resource",
        operation_id=lambda args: f"resource:{args['resource']['id']}:create:v1",
        outcome_probe=probe,
    )
    result = EffectExecutor(
        ledger=SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    ).execute(
        contract=contract,
        arguments={"resource": {"id": "R-ORIGINAL"}},
        handler=handler,
    )

    assert result.status is EffectStatus.CONFIRMED
    assert observed_probe_ids == ["R-ORIGINAL"]


def test_operation_id_callback_cannot_mutate_arguments_for_provider_key(tmp_path):
    seen_provider_inputs: list[str] = []

    def operation_id(arguments):
        arguments["invoice_id"] = "MUTATED"
        return "invoice:INV-IMMUTABLE:capture:v1"

    def provider_key(arguments):
        seen_provider_inputs.append(arguments["invoice_id"])
        return f"capture:{arguments['invoice_id']}"

    contract = EffectContract(
        name="capture_payment",
        operation_id=operation_id,
        provider_idempotency_key=provider_key,
    )
    result = EffectExecutor(
        ledger=SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    ).execute(
        contract=contract,
        arguments={"invoice_id": "INV-IMMUTABLE"},
        handler=lambda arguments, *, idempotency_key: {
            "invoice_id": arguments["invoice_id"],
            "idempotency_key": idempotency_key,
        },
    )

    assert result.status is EffectStatus.CONFIRMED
    assert seen_provider_inputs == ["INV-IMMUTABLE"]
    assert result.output["invoice_id"] == "INV-IMMUTABLE"
