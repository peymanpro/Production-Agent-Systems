from production_agent_systems import (
    AgentAction,
    AgentRuntime,
    CheckpointStore,
    CircuitBreaker,
    DeterministicPlanner,
    InMemoryAuditSink,
    InMemoryEventSink,
    InMemoryStateStore,
    PermissionPolicy,
    RetryPolicy,
    RuntimeConfig,
    Task,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
)


def test_circuit_breaker_blocks_dependency_after_repeated_failures():
    calls = []

    def failing():
        calls.append(1)
        raise RuntimeError("down")

    registry = ToolRegistry(
        [
            ToolDefinition(
                "downstream",
                "Fails",
                {"required": [], "properties": {}},
                ToolEffect.READ,
                "1",
                failing,
            )
        ]
    )
    breaker = CircuitBreaker(failure_threshold=2, reset_after_calls=1)
    runtime = AgentRuntime(
        planner=DeterministicPlanner([AgentAction.call_tool("downstream", {})]),
        registry=registry,
        policy=PermissionPolicy(frozenset({"downstream"})),
        state_store=InMemoryStateStore(),
        checkpoint_store=CheckpointStore(),
        event_sink=InMemoryEventSink(),
        circuit_breaker=breaker,
        config=RuntimeConfig(
            timeout_seconds=0.1,
            retry_policy=RetryPolicy(
                max_attempts=5,
                retryable_errors=frozenset({"RuntimeError: down"}),
            ),
        ),
    )

    state = runtime.run(Task("cb-1", "test"))

    assert not state.observations[0].result.success
    assert state.observations[0].result.attempts == 2
    assert len(calls) == 2


def test_audit_records_are_separate_from_trace_events():
    audit = InMemoryAuditSink()
    registry = ToolRegistry(
        [
            ToolDefinition(
                "read",
                "Read",
                {"required": [], "properties": {}},
                ToolEffect.READ,
                "1",
                lambda: "ok",
            )
        ]
    )
    runtime = AgentRuntime(
        planner=DeterministicPlanner(
            [AgentAction.call_tool("read", {}), AgentAction.finish("done")]
        ),
        registry=registry,
        policy=PermissionPolicy(frozenset({"read"})),
        state_store=InMemoryStateStore(),
        checkpoint_store=CheckpointStore(),
        event_sink=InMemoryEventSink(),
        audit_sink=audit,
    )

    runtime.run(Task("audit-1", "audit"))

    records = audit.records("audit-1")
    assert records
    assert all(record.run_id == "audit-1" for record in records)
    assert {record.outcome for record in records} >= {"requested", "succeeded"}
