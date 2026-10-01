from __future__ import annotations

from dataclasses import dataclass

from .models import ToolResult


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    retryable_errors: frozenset[str] = frozenset({"timeout"})

    def should_retry(self, result: ToolResult, attempt: int) -> bool:
        return (
            not result.success
            and attempt < self.max_attempts
            and result.error in self.retryable_errors
        )

    def backoff_seconds(self, next_attempt: int) -> float:
        return min(0.25, 0.01 * (2 ** max(0, next_attempt - 2)))
