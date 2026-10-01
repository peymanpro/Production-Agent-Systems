from __future__ import annotations

from typing import Protocol

from .models import AgentAction, AgentState


class Planner(Protocol):
    def next_action(self, state: AgentState) -> AgentAction: ...


class DeterministicPlanner:
    def __init__(self, actions: list[AgentAction]) -> None:
        self._actions = tuple(actions)

    def next_action(self, state: AgentState) -> AgentAction:
        if state.step_index >= len(self._actions):
            return AgentAction.finish("plan exhausted")
        return self._actions[state.step_index]
