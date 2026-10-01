from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable

from .models import ToolEffect, ToolResult


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    effect: ToolEffect
    version: str
    handler: Callable[..., Any]

    def validate(self, arguments: dict[str, Any]) -> None:
        required = self.input_schema.get("required", [])
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ValueError(f"missing required arguments: {', '.join(missing)}")

        properties = self.input_schema.get("properties", {})
        for name, value in arguments.items():
            schema = properties.get(name)
            if schema is None:
                continue
            expected = schema.get("type")
            if expected == "string" and not isinstance(value, str):
                raise ValueError(f"argument '{name}' must be a string")
            if expected == "integer" and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                raise ValueError(f"argument '{name}' must be an integer")
            if expected == "object" and not isinstance(value, dict):
                raise ValueError(f"argument '{name}' must be an object")


class ToolRegistry:
    def __init__(self, tools: list[ToolDefinition] | None = None) -> None:
        self._tools = {tool.name: tool for tool in tools or []}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def execute(
        self, name: str, arguments: dict[str, Any], timeout_seconds: float | None = None
    ) -> ToolResult:
        tool = self.registry.get(name)
        tool.validate(arguments)

        if timeout_seconds is None:
            try:
                return ToolResult(success=True, output=tool.handler(**arguments))
            except Exception as exc:
                return ToolResult(
                    success=False, error=f"{type(exc).__name__}: {exc}"
                )

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(tool.handler, **arguments)
        try:
            return ToolResult(success=True, output=future.result(timeout=timeout_seconds))
        except FutureTimeoutError:
            future.cancel()
            return ToolResult(success=False, error="timeout")
        except Exception as exc:
            return ToolResult(
                success=False, error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
