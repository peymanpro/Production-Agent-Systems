from production_agent_systems import (
    AgentState,
    CheckpointStore,
    DeterministicPlanner,
    InMemoryEventSink,
    InMemoryStateStore,
    PermissionPolicy,
    RunStatus,
    Task,
    ToolRegistry,
)
from production_agent_systems.runtime import AgentRuntime


def test_state_snapshot_round_trip():
    task = Task("t-1", "demo", {"x": 1})
    state = AgentState(task=task, status=RunStatus.RUNNING, step_index=2)
    state.working["value"] = 3

    restored = AgentState.from_snapshot(state.snapshot())

    assert restored.task == state.task
    assert restored.status is RunStatus.RUNNING
    assert restored.step_index == 2
    assert restored.working == {"value": 3}


def test_invalid_transition_is_rejected():
    runtime = AgentRuntime(
        planner=DeterministicPlanner([]),
        registry=ToolRegistry([]),
        policy=PermissionPolicy(frozenset()),
        state_store=InMemoryStateStore(),
        checkpoint_store=CheckpointStore(),
        event_sink=InMemoryEventSink(),
    )
    state = AgentState(Task("invalid", "test"))

    try:
        runtime._transition(state, RunStatus.COMPLETED, "invalid")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid transition was accepted")
