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
    ToolRegistry,
)


def test_replay_reports_consistent_execution():
    sink = InMemoryEventSink()
    runtime = AgentRuntime(
        planner=DeterministicPlanner([AgentAction.finish("done")]),
        registry=ToolRegistry([]),
        policy=PermissionPolicy(frozenset()),
        state_store=InMemoryStateStore(),
        checkpoint_store=CheckpointStore(),
        event_sink=sink,
    )

    state = runtime.run(Task("run-5", "finish"))
    result = ReplayEngine().replay(sink.events(state.task.task_id))

    assert result.terminal_status == "completed"
    assert result.transition_count == 2
    assert result.tool_call_count == 0
    assert result.consistent_sequence
