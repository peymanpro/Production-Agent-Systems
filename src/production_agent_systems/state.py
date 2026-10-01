from __future__ import annotations

from typing import Protocol

from .models import AgentState


class StateStore(Protocol):
    def save(self, state: AgentState) -> None: ...
    def load(self, task_id: str) -> AgentState | None: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self._states: dict[str, dict] = {}

    def save(self, state: AgentState) -> None:
        self._states[state.task.task_id] = state.snapshot()

    def load(self, task_id: str) -> AgentState | None:
        snapshot = self._states.get(task_id)
        return None if snapshot is None else AgentState.from_snapshot(snapshot)


class CheckpointStore(InMemoryStateStore):
    """V1 checkpoint boundary; persistence technology can be replaced later."""
