from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from production_agent_systems import (
    AgentAction,
    AgentRuntime,
    CheckpointStore,
    DeterministicPlanner,
    EffectContract,
    EffectExecutor,
    EventSink,
    InMemoryEventSink,
    InMemoryStateStore,
    PermissionPolicy,
    RunStatus,
    SQLiteEffectLedger,
    Task,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
)
from production_agent_systems.testkit import (
    FaultInjectingPaymentProvider,
    FaultPoint,
)


def run_demo() -> dict[str, Any]:
    """Run a complete runtime → payment tool → lost-ack → reconciliation scenario."""
    provider = FaultInjectingPaymentProvider(
        fault_point=FaultPoint.AFTER_COMMIT_BEFORE_ACK,
        supports_idempotency=True,
    )
    contract = EffectContract(
        name="capture_payment",
        operation_id=lambda args: f"invoice:{args['invoice_id']}:capture",
        provider_idempotency_key=lambda args: f"capture:{args['invoice_id']}",
        provider_idempotency_guaranteed=True,
        outcome_probe=provider.probe,
    )
    payment_tool = ToolDefinition(
        name="capture_payment",
        description="Capture an invoice payment in the test provider",
        input_schema={
            "required": ["invoice_id"],
            "properties": {"invoice_id": {"type": "string", "minLength": 1}},
        },
        effect=ToolEffect.MUTATION,
        version="1",
        handler=provider.capture,
        effect_contract=contract,
    )

    with tempfile.TemporaryDirectory(prefix="production-agent-effects-") as temp_dir:
        ledger_path = Path(temp_dir) / "effects.sqlite3"
        executor = EffectExecutor(ledger=SQLiteEffectLedger(ledger_path))
        event_sink = InMemoryEventSink()
        runtime = AgentRuntime(
            planner=DeterministicPlanner(
                [
                    AgentAction.call_tool(
                        "capture_payment",
                        {"invoice_id": "INV-DEMO-1042"},
                        reason="Capture the demo invoice",
                    ),
                    AgentAction.finish("Payment outcome reconciled"),
                ]
            ),
            registry=ToolRegistry([payment_tool]),
            policy=PermissionPolicy(
                frozenset({"capture_payment"}),
                mutation_requires_confirmation=False,
            ),
            state_store=InMemoryStateStore(),
            checkpoint_store=CheckpointStore(),
            event_sink=event_sink,
            effect_executor=executor,
        )
        state = runtime.run(Task("effect-demo-1042", "Capture demo invoice"))
        operation_id = "invoice:INV-DEMO-1042:capture"
        effect_record = executor.ledger.get(operation_id)

        report = {
            "run_status": state.status.value,
            "effect_status": effect_record.status.value if effect_record else None,
            "operation_id": operation_id,
            "attempts": effect_record.attempts if effect_record else None,
            "committed_effect_count": len(provider.committed_effects),
            "duplicate_effect_detected": len(provider.committed_effects) > 1,
            "payment": effect_record.output if effect_record else None,
            "runtime_event_count": len(event_sink.events("effect-demo-1042")),
            "ledger_event_count": len(executor.ledger.events(operation_id)),
        }
        if (
            state.status is not RunStatus.COMPLETED
            or effect_record is None
            or effect_record.status.value != "confirmed"
            or len(provider.committed_effects) != 1
        ):
            raise RuntimeError(f"effect assurance demo failed: {report}")
        return report


def main() -> None:
    print(json.dumps(run_demo(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
