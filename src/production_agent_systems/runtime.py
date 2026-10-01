from __future__ import annotations

from dataclasses import dataclass
from time import sleep
from typing import Callable

from .audit import AuditRecord, AuditSink
from .events import Event, EventSink, EventType
from .models import (
    ActionKind,
    AgentState,
    Observation,
    PolicyDecision,
    RunStatus,
    Task,
    ToolResult,
)
from .planner import Planner
from .policy import PermissionPolicy
from .reliability import CircuitBreaker, RetryPolicy
from .state import CheckpointStore, StateStore
from .tools import ToolExecutor, ToolRegistry


VALID_TRANSITIONS = {
    RunStatus.CREATED: {RunStatus.RUNNING},
    RunStatus.RUNNING: {
        RunStatus.WAITING,
        RunStatus.PAUSED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.WAITING: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: {RunStatus.RUNNING},
}


@dataclass(frozen=True)
class RuntimeConfig:
    timeout_seconds: float = 2.0
    retry_policy: RetryPolicy = RetryPolicy()


class AgentRuntime:
    def __init__(
        self,
        *,
        planner: Planner,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        state_store: StateStore,
        checkpoint_store: CheckpointStore,
        event_sink: EventSink,
        audit_sink: AuditSink | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        config: RuntimeConfig | None = None,
        sleep_fn: Callable[[float], None] = sleep,
    ) -> None:
        self.planner = planner
        self.registry = registry
        self.policy = policy
        self.state_store = state_store
        self.checkpoint_store = checkpoint_store
        self.event_sink = event_sink
        self.audit_sink = audit_sink
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.tool_executor = ToolExecutor(registry)
        self.config = config or RuntimeConfig()
        self.sleep_fn = sleep_fn

    def _emit(self, state: AgentState, event_type: EventType, data: dict) -> None:
        existing = self.event_sink.events(state.task.task_id)
        self.event_sink.append(
            Event.create(len(existing) + 1, state.task.task_id, event_type, data)
        )

    def _audit(
        self,
        state: AgentState,
        action: str,
        resource: str,
        outcome: str,
        details: dict | None = None,
    ) -> None:
        if self.audit_sink is not None:
            self.audit_sink.append(
                AuditRecord.create(
                    state.task.task_id,
                    action,
                    resource,
                    outcome,
                    details,
                )
            )

    def _transition(self, state: AgentState, new_status: RunStatus, reason: str) -> None:
        old = state.status
        if new_status not in VALID_TRANSITIONS[old]:
            raise ValueError(f"invalid state transition: {old.value} -> {new_status.value}")
        state.status = new_status
        state.version += 1
        self._emit(
            state,
            EventType.STATE_TRANSITION,
            {"from": old.value, "to": new_status.value, "reason": reason},
        )

    def _checkpoint(self, state: AgentState) -> None:
        self.checkpoint_store.save(state)
        self.state_store.save(state)
        self._emit(
            state,
            EventType.CHECKPOINT,
            {"version": state.version, "step_index": state.step_index},
        )

    def run(
        self,
        task: Task,
        *,
        resume: bool = False,
        stop_after_steps: int | None = None,
    ) -> AgentState:
        state = self.checkpoint_store.load(task.task_id) if resume else None

        if state is None:
            state = AgentState(task=task)
            self._emit(state, EventType.RUN_STARTED, {"objective": task.objective})
            self._transition(state, RunStatus.RUNNING, "start")
        else:
            if state.status is RunStatus.COMPLETED:
                return state
            self._emit(
                state,
                EventType.RECOVERY,
                {"from_version": state.version, "step_index": state.step_index},
            )
            self._transition(state, RunStatus.RUNNING, "resume from checkpoint")

        self._checkpoint(state)

        while state.status is RunStatus.RUNNING:
            action = self.planner.next_action(state)
            self._emit(
                state,
                EventType.DECISION,
                {
                    "kind": action.kind.value,
                    "tool_name": action.tool_name,
                    "reason": action.reason,
                },
            )

            if action.kind is ActionKind.FINISH:
                self._transition(
                    state,
                    RunStatus.COMPLETED,
                    action.reason or "planner finished",
                )
                self._emit(
                    state,
                    EventType.TERMINATION,
                    {"status": state.status.value, "reason": action.reason},
                )
                self._checkpoint(state)
                return state

            if action.tool_name is None:
                raise ValueError("tool action must specify tool_name")

            tool = self.registry.get(action.tool_name)
            decision = self.policy.evaluate(
                tool,
                confirmed=bool(
                    state.task.metadata.get("confirmations", {}).get(tool.name)
                ),
            )
            self._emit(
                state,
                EventType.POLICY_DECISION,
                {"tool_name": tool.name, "decision": decision.value},
            )

            if decision is PolicyDecision.DENY:
                self._audit(
                    state,
                    "tool_policy",
                    tool.name,
                    "denied",
                    {"decision": decision.value},
                )
                state.failure_reason = f"policy denied tool: {tool.name}"
                self._transition(state, RunStatus.FAILED, state.failure_reason)
                self._emit(
                    state,
                    EventType.TERMINATION,
                    {"status": state.status.value, "reason": state.failure_reason},
                )
                self._checkpoint(state)
                return state

            if decision is PolicyDecision.CONFIRMATION_REQUIRED:
                self._audit(
                    state,
                    "tool_policy",
                    tool.name,
                    "confirmation_required",
                    {"decision": decision.value},
                )
                state.failure_reason = f"confirmation required for tool: {tool.name}"
                self._transition(state, RunStatus.WAITING, state.failure_reason)
                self._emit(
                    state,
                    EventType.TERMINATION,
                    {"status": state.status.value, "reason": state.failure_reason},
                )
                self._checkpoint(state)
                return state

            if action.idempotency_key and action.idempotency_key in state.idempotency_results:
                previous = state.idempotency_results[action.idempotency_key]
                result = ToolResult(
                    success=previous.success,
                    output=previous.output,
                    error=previous.error,
                    attempts=previous.attempts,
                    duplicate_suppressed=True,
                )
            else:
                self._audit(
                    state,
                    "tool_execution",
                    tool.name,
                    "requested",
                    {"effect": tool.effect.value},
                )
                self._emit(
                    state,
                    EventType.TOOL_REQUEST,
                    {"tool_name": tool.name, "arguments": action.arguments},
                )
                result = self._execute_with_retry(
                    state,
                    action.tool_name,
                    action.arguments,
                )
                if result.success and action.idempotency_key:
                    state.executed_idempotency_keys.add(action.idempotency_key)
                    state.idempotency_results[action.idempotency_key] = result

            self._emit(
                state,
                EventType.TOOL_RESULT,
                {
                    "tool_name": tool.name,
                    "success": result.success,
                    "error": result.error,
                    "attempts": result.attempts,
                    "duplicate_suppressed": result.duplicate_suppressed,
                },
            )
            state.observations.append(
                Observation(
                    step=state.step_index,
                    tool_name=tool.name,
                    result=result,
                )
            )

            if not result.success:
                self._audit(
                    state,
                    "tool_execution",
                    tool.name,
                    "failed",
                    {
                        "error": result.error,
                        "attempts": result.attempts,
                    },
                )
                state.failure_reason = result.error or "tool execution failed"
                self._transition(state, RunStatus.FAILED, state.failure_reason)
                self._emit(
                    state,
                    EventType.TERMINATION,
                    {"status": state.status.value, "reason": state.failure_reason},
                )
                self._checkpoint(state)
                return state

            self._audit(
                state,
                "tool_execution",
                tool.name,
                "succeeded",
                {
                    "attempts": result.attempts,
                    "duplicate_suppressed": result.duplicate_suppressed,
                },
            )
            state.working[tool.name] = result.output
            state.step_index += 1
            state.version += 1

            if stop_after_steps is not None and state.step_index >= stop_after_steps:
                self._transition(
                    state,
                    RunStatus.PAUSED,
                    "execution paused at requested step boundary",
                )
                self._checkpoint(state)
                return state

            self._checkpoint(state)

        return state

    def _execute_with_retry(
        self, state: AgentState, name: str, arguments: dict
    ) -> ToolResult:
        policy = self.config.retry_policy
        last: ToolResult | None = None

        for attempt in range(1, policy.max_attempts + 1):
            if not self.circuit_breaker.allow(name):
                return ToolResult(
                    success=False,
                    error="circuit_open",
                    attempts=attempt,
                )

            result = self.tool_executor.execute(
                name,
                arguments,
                self.config.timeout_seconds,
            )
            last = ToolResult(
                success=result.success,
                output=result.output,
                error=result.error,
                attempts=attempt,
            )

            if result.success:
                self.circuit_breaker.record_success(name)
                return last

            circuit_opened = self.circuit_breaker.record_failure(name)
            if circuit_opened:
                self._emit(
                    state,
                    EventType.TERMINATION,
                    {
                        "status": "failed",
                        "reason": "circuit opened",
                        "tool_name": name,
                    },
                )
                return last

            if not policy.should_retry(result, attempt):
                return last

            next_attempt = attempt + 1
            delay = policy.backoff_seconds(next_attempt)
            self._emit(
                state,
                EventType.RETRY,
                {
                    "tool_name": name,
                    "attempt": attempt,
                    "next_attempt": next_attempt,
                    "delay_seconds": delay,
                    "reason": result.error,
                },
            )
            self.sleep_fn(delay)

        assert last is not None
        return last
