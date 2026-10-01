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


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, reset_after_calls: int = 1) -> None:
        if failure_threshold < 1 or reset_after_calls < 1:
            raise ValueError("circuit breaker thresholds must be positive")
        self.failure_threshold = failure_threshold
        self.reset_after_calls = reset_after_calls
        self._failures: dict[str, int] = {}
        self._open_remaining: dict[str, int] = {}

    def allow(self, dependency: str) -> bool:
        remaining = self._open_remaining.get(dependency, 0)
        if remaining > 0:
            self._open_remaining[dependency] = remaining - 1
            return False
        return True

    def record_success(self, dependency: str) -> None:
        self._failures.pop(dependency, None)
        self._open_remaining.pop(dependency, None)

    def record_failure(self, dependency: str) -> bool:
        failures = self._failures.get(dependency, 0) + 1
        self._failures[dependency] = failures
        if failures >= self.failure_threshold:
            self._open_remaining[dependency] = self.reset_after_calls
            self._failures[dependency] = 0
            return True
        return False
