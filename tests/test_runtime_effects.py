from __future__ import annotations

from production_agent_systems.effects import (
    EffectContract,
    EffectExecutor,
    EffectStatus,
    OutcomeProbeResult,
    ProbeStatus,
    SQLiteEffectLedger,
)
from production_agent_systems.testkit import FaultInjectingPaymentProvider, FaultPoint


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
        provider_idempotency_guaranteed=False,
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
