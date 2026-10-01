from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActionKind(str, Enum):
    TOOL = "tool"
    FINISH = "finish"


class ToolEffect(str, Enum):
    READ = "read"
    MUTATION = "mutation"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRMATION_REQUIRED = "confirmation_required"


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class Task:
    task_id: str
    objective: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentAction:
    kind: ActionKind
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    idempotency_key: str | None = None

    def validate(self) -> None:
        if self.kind is ActionKind.TOOL and not self.tool_name:
            raise ValueError("tool action must specify tool_name")
        if self.kind is ActionKind.FINISH and self.tool_name is not None:
            raise ValueError("finish action cannot specify tool_name")
        if not isinstance(self.arguments, dict):
            raise ValueError("action arguments must be an object")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("idempotency key must not be empty")

    @classmethod
    def call_tool(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> AgentAction:
        action = cls(
            kind=ActionKind.TOOL,
            tool_name=tool_name,
            arguments=arguments,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        action.validate()
        return action

    @classmethod
    def finish(cls, reason: str = "") -> AgentAction:
        action = cls(kind=ActionKind.FINISH, reason=reason)
        action.validate()
        return action


@dataclass(frozen=True)
class ToolResult:
    success: bool
    output: Any = None
    error: str | None = None
    attempts: int = 1
    duplicate_suppressed: bool = False


@dataclass(frozen=True)
class Observation:
    step: int
    tool_name: str
    result: ToolResult


@dataclass
class AgentState:
    task: Task
    status: RunStatus = RunStatus.CREATED
    step_index: int = 0
    observations: list[Observation] = field(default_factory=list)
    working: dict[str, Any] = field(default_factory=dict)
    executed_idempotency_keys: set[str] = field(default_factory=set)
    idempotency_results: dict[str, ToolResult] = field(default_factory=dict)
    failure_reason: str | None = None
    version: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "task": {
                "task_id": self.task.task_id,
                "objective": self.task.objective,
                "metadata": self.task.metadata,
            },
            "status": self.status.value,
            "step_index": self.step_index,
            "observations": [
                {
                    "step": item.step,
                    "tool_name": item.tool_name,
                    "result": {
                        "success": item.result.success,
                        "output": item.result.output,
                        "error": item.result.error,
                        "attempts": item.result.attempts,
                        "duplicate_suppressed": item.result.duplicate_suppressed,
                    },
                }
                for item in self.observations
            ],
            "working": self.working,
            "executed_idempotency_keys": sorted(self.executed_idempotency_keys),
            "idempotency_results": {
                key: {
                    "success": value.success,
                    "output": value.output,
                    "error": value.error,
                    "attempts": value.attempts,
                    "duplicate_suppressed": value.duplicate_suppressed,
                }
                for key, value in self.idempotency_results.items()
            },
            "failure_reason": self.failure_reason,
            "version": self.version,
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> AgentState:
        task_data = snapshot["task"]
        state = cls(
            task=Task(
                task_id=task_data["task_id"],
                objective=task_data["objective"],
                metadata=task_data.get("metadata", {}),
            ),
            status=RunStatus(snapshot["status"]),
            step_index=snapshot["step_index"],
            working=snapshot.get("working", {}),
            executed_idempotency_keys=set(snapshot.get("executed_idempotency_keys", [])),
            failure_reason=snapshot.get("failure_reason"),
            version=snapshot.get("version", 0),
        )
        for key, raw in snapshot.get("idempotency_results", {}).items():
            state.idempotency_results[key] = ToolResult(
                success=raw["success"],
                output=raw.get("output"),
                error=raw.get("error"),
                attempts=raw.get("attempts", 1),
                duplicate_suppressed=raw.get("duplicate_suppressed", False),
            )
        for item in snapshot.get("observations", []):
            raw = item["result"]
            state.observations.append(
                Observation(
                    step=item["step"],
                    tool_name=item["tool_name"],
                    result=ToolResult(
                        success=raw["success"],
                        output=raw.get("output"),
                        error=raw.get("error"),
                        attempts=raw.get("attempts", 1),
                        duplicate_suppressed=raw.get("duplicate_suppressed", False),
                    ),
                )
            )
        return state
