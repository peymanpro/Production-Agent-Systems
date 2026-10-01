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
    RetryPolicy,
    RuntimeConfig,
    Task,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
)


def build_runtime(
    actions,
    tools,
    *,
    confirm_mutations=True,
    retry_policy=None,
    timeout=0.1,
    sleep_fn=lambda _: None,
):
    registry = ToolRegistry(tools)
    return AgentRuntime(
        planner=DeterministicPlanner(actions),
        registry=registry,
        policy=PermissionPolicy(
            frozenset(registry.names()),
            mutation_requires_confirmation=confirm_mutations,
        ),
        state_store=InMemoryStateStore(),
        checkpoint_store=CheckpointStore(),
        event_sink=InMemoryEventSink(),
        config=RuntimeConfig(
            timeout_seconds=timeout,
            retry_policy=retry_policy or RetryPolicy(),
        ),
        sleep_fn=sleep_fn,
    )


def test_end_to_end_read_and_mutation_with_confirmation():
    calls = []

    def read_order(order_id: str):
        return {"order_id": order_id, "status": "open"}

    def update_order(order_id: str, status: str):
        calls.append((order_id, status))
        return {"order_id": order_id, "status": status}

    runtime = build_runtime(
        [
            AgentAction.call_tool("read_order", {"order_id": "A-10"}),
            AgentAction.call_tool(
                "update_order",
                {"order_id": "A-10", "status": "approved"},
                idempotency_key="approve:A-10",
            ),
            AgentAction.finish("order processed"),
        ],
        [
            ToolDefinition(
                "read_order",
                "Read order",
                {
                    "required": ["order_id"],
                    "properties": {"order_id": {"type": "string"}},
                },
                ToolEffect.READ,
                "1",
                read_order,
            ),
            ToolDefinition(
                "update_order",
                "Update order",
                {
                    "required": ["order_id", "status"],
                    "properties": {
                        "order_id": {"type": "string"},
                        "status": {"type": "string"},
                    },
                },
                ToolEffect.MUTATION,
                "1",
                update_order,
            ),
        ],
    )
    task = Task(
        "run-1",
        "approve order",
        {"confirmations": {"update_order": True}},
    )
    state = runtime.run(task)

    assert state.status is RunStatus.COMPLETED
    assert state.working["update_order"]["status"] == "approved"
    assert calls == [("A-10", "approved")]
    assert len(runtime.event_sink.events("run-1")) > 10


def test_unauthorized_tool_is_blocked_before_side_effect():
    calls = []
    tool = ToolDefinition(
        "dangerous_write",
        "Mutation",
        {"required": [], "properties": {}},
        ToolEffect.MUTATION,
        "1",
        lambda: calls.append(1),
    )
    runtime = build_runtime([AgentAction.call_tool("dangerous_write", {})], [tool])
    runtime.policy = PermissionPolicy(
        frozenset(),
        mutation_requires_confirmation=False,
    )

    state = runtime.run(Task("run-2", "do unsafe thing"))

    assert state.status is RunStatus.FAILED
    assert calls == []
    assert any(
        event.event_type is EventType.POLICY_DECISION
        for event in runtime.event_sink.events("run-2")
    )


def test_mutation_without_confirmation_enters_waiting_state():
    tool = ToolDefinition(
        "write",
        "Mutation",
        {"required": [], "properties": {}},
        ToolEffect.MUTATION,
        "1",
        lambda: "ok",
    )
    runtime = build_runtime(
        [AgentAction.call_tool("write", {})],
        [tool],
        confirm_mutations=True,
    )

    state = runtime.run(Task("run-6", "write"))

    assert state.status is RunStatus.WAITING
    assert state.failure_reason == "confirmation required for tool: write"


def test_duplicate_idempotency_key_reuses_original_result():
    calls = []

    def write():
        calls.append(1)
        return {"ok": True}

    registry = ToolRegistry(
        [
            ToolDefinition(
                "write",
                "Mutation",
                {"required": [], "properties": {}},
                ToolEffect.MUTATION,
                "1",
                write,
            )
        ]
    )
    sink = InMemoryEventSink()
    runtime = AgentRuntime(
        planner=DeterministicPlanner(
            [
                AgentAction.call_tool("write", {}, idempotency_key="w:1"),
                AgentAction.call_tool("write", {}, idempotency_key="w:1"),
                AgentAction.finish("done"),
            ]
        ),
        registry=registry,
        policy=PermissionPolicy(
            frozenset({"write"}),
            mutation_requires_confirmation=False,
        ),
        state_store=InMemoryStateStore(),
        checkpoint_store=CheckpointStore(),
        event_sink=sink,
    )

    state = runtime.run(Task("run-7", "write twice"))

    assert state.status is RunStatus.COMPLETED
    assert calls == [1]
    assert state.observations[1].result.duplicate_suppressed
