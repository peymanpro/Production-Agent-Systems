from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import json


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    DECISION = "decision"
    POLICY_DECISION = "policy_decision"
    TOOL_REQUEST = "tool_request"
    TOOL_RESULT = "tool_result"
    STATE_TRANSITION = "state_transition"
    RETRY = "retry"
    CHECKPOINT = "checkpoint"
    RECOVERY = "recovery"
    TERMINATION = "termination"


@dataclass(frozen=True)
class Event:
    sequence: int
    run_id: str
    event_type: EventType
    data: dict[str, Any]
    timestamp: str

    @classmethod
    def create(
        cls, sequence: int, run_id: str, event_type: EventType, data: dict[str, Any]
    ) -> Event:
        return cls(
            sequence=sequence,
            run_id=run_id,
            event_type=event_type,
            data=data,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["event_type"] = self.event_type.value
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, default=str)


class EventSink:
    def append(self, event: Event) -> None:
        raise NotImplementedError

    def events(self, run_id: str | None = None) -> list[Event]:
        raise NotImplementedError


class InMemoryEventSink(EventSink):
    def __init__(self) -> None:
        self._events: list[Event] = []

    def append(self, event: Event) -> None:
        self._events.append(event)

    def events(self, run_id: str | None = None) -> list[Event]:
        if run_id is None:
            return list(self._events)
        return [event for event in self._events if event.run_id == run_id]

    def export_jsonl(self, run_id: str | None = None) -> str:
        return "\n".join(event.to_json() for event in self.events(run_id))
