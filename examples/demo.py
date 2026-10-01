from production_agent_systems import (
    AgentAction,
    AgentRuntime,
    CheckpointStore,
    DeterministicPlanner,
    InMemoryEventSink,
    InMemoryStateStore,
    PermissionPolicy,
    ReplayEngine,
    Task,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
)


def get_customer(customer_id: str) -> dict:
    return {"customer_id": customer_id, "name": "Demo Customer", "state": "active"}


def suspend_customer(customer_id: str) -> dict:
    return {"customer_id": customer_id, "state": "suspended"}


registry = ToolRegistry(
    [
        ToolDefinition(
            "get_customer",
            "Read a customer",
            {
                "required": ["customer_id"],
                "properties": {"customer_id": {"type": "string"}},
            },
            ToolEffect.READ,
            "1",
            get_customer,
        ),
        ToolDefinition(
            "suspend_customer",
            "Suspend a customer",
            {
                "required": ["customer_id"],
                "properties": {"customer_id": {"type": "string"}},
            },
            ToolEffect.MUTATION,
            "1",
            suspend_customer,
        ),
    ]
)

sink = InMemoryEventSink()
runtime = AgentRuntime(
    planner=DeterministicPlanner(
        [
            AgentAction.call_tool("get_customer", {"customer_id": "C-42"}),
            AgentAction.call_tool(
                "suspend_customer",
                {"customer_id": "C-42"},
                idempotency_key="suspend:C-42",
            ),
            AgentAction.finish("workflow complete"),
        ]
    ),
    registry=registry,
    policy=PermissionPolicy(
        frozenset(registry.names()),
        mutation_requires_confirmation=True,
    ),
    state_store=InMemoryStateStore(),
    checkpoint_store=CheckpointStore(),
    event_sink=sink,
)


if __name__ == "__main__":
    task = Task(
        "demo-1",
        "Suspend customer C-42",
        {"confirmations": {"suspend_customer": True}},
    )
    result = runtime.run(task)
    print("status:", result.status.value)
    print("steps:", result.step_index)
    print("events:", len(sink.events(task.task_id)))
    print("replay:", ReplayEngine().replay(sink.events(task.task_id)))
