from production_agent_systems import (
    AgentAction,
    AgentRuntime,
    CheckpointStore,
    DeterministicPlanner,
    EventType,
    InMemoryEventSink,
    InMemoryStateStore,
    PermissionPolicy,
    RetryPolicy,
    RunStatus,
    RuntimeConfig,
    Task,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
)


def test_timeout_is_retried_and_then_fails():
    attempts = []

    def slow():
        import time

        attempts.append(1)
        time.sleep(0.03)
        return "done"

    registry = ToolRegistry(
        [
            ToolDefinition(
                "slow",
                "Slow tool",
                {"required": [], "properties": {}},
                ToolEffect.READ,
                "1",
                slow,
            )
        ]
    )
    sink = InMemoryEventSink()
    runtime = AgentRuntime(
        planner=DeterministicPlanner([AgentAction.call_tool("slow", {})]),
        registry=registry,
        policy=PermissionPolicy(frozenset({"slow"})),
        state_store=InMemoryStateStore(),
        checkpoint_store=CheckpointStore(),
        event_sink=sink,
        config=RuntimeConfig(
            timeout_seconds=0.005,
            retry_policy=RetryPolicy(max_attempts=3),
        ),
        sleep_fn=lambda _: None,
    )

    state = runtime.run(Task("run-3", "timeout"))

    assert state.status is RunStatus.FAILED
    assert len(attempts) == 3
    assert len(
        [event for event in sink.events("run-3") if event.event_type is EventType.RETRY]
    ) == 2
