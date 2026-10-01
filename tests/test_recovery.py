from production_agent_systems import (
    AgentAction,
    AgentRuntime,
    CheckpointStore,
    DeterministicPlanner,
    EventType,
    InMemoryEventSink,
    InMemoryStateStore,
    PermissionPolicy,
    RunStatus,
    Task,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
)


def test_checkpoint_can_resume_without_repeating_idempotent_side_effect():
    calls = []

    def charge(order_id: str):
        calls.append(order_id)
        return {"charged": order_id}

    registry = ToolRegistry(
        [
            ToolDefinition(
                "charge",
                "Charge order",
                {
                    "required": ["order_id"],
                    "properties": {"order_id": {"type": "string"}},
                },
                ToolEffect.MUTATION,
                "1",
                charge,
            )
        ]
    )
    sink = InMemoryEventSink()
    checkpoints = CheckpointStore()
    runtime = AgentRuntime(
        planner=DeterministicPlanner(
            [
                AgentAction.call_tool(
                    "charge",
                    {"order_id": "A-1"},
                    idempotency_key="charge:A-1",
                ),
                AgentAction.finish("done"),
            ]
        ),
        registry=registry,
        policy=PermissionPolicy(
            frozenset({"charge"}),
            mutation_requires_confirmation=False,
        ),
        state_store=InMemoryStateStore(),
        checkpoint_store=checkpoints,
        event_sink=sink,
    )

    state = runtime.run(Task("run-4", "charge"), stop_after_steps=1)

    assert state.status is RunStatus.PAUSED
    assert calls == ["A-1"]

    resumed = runtime.run(Task("run-4", "charge"), resume=True)

    assert resumed.status is RunStatus.COMPLETED
    assert calls == ["A-1"]
    assert any(
        event.event_type is EventType.RECOVERY
        for event in sink.events("run-4")
    )
