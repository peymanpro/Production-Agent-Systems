from __future__ import annotations

from dataclasses import dataclass

from .events import Event, EventType


@dataclass(frozen=True)
class ReplayResult:
    run_id: str
    terminal_status: str | None
    transition_count: int
    tool_call_count: int
    retry_count: int
    consistent_sequence: bool


class ReplayEngine:
    def replay(self, events: list[Event]) -> ReplayResult:
        ordered = sorted(events, key=lambda item: item.sequence)
        consistent = [event.sequence for event in ordered] == list(
            range(1, len(ordered) + 1)
        )
        terminal = None
        transitions = 0
        tool_calls = 0
        retries = 0

        for event in ordered:
            if event.event_type is EventType.STATE_TRANSITION:
                transitions += 1
                destination = event.data["to"]
                if destination in {"completed", "failed", "waiting"}:
                    terminal = destination
            elif event.event_type is EventType.TOOL_REQUEST:
                tool_calls += 1
            elif event.event_type is EventType.RETRY:
                retries += 1

        return ReplayResult(
            run_id=ordered[0].run_id if ordered else "",
            terminal_status=terminal,
            transition_count=transitions,
            tool_call_count=tool_calls,
            retry_count=retries,
            consistent_sequence=consistent,
        )
