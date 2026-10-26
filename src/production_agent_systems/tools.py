from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable


from .effects import EffectContract, EffectExecutor, EffectStatus, OperationConflictError
from .models import ToolEffect, ToolResult


def _require_length(value: Any, schema: dict[str, Any], path: str, *, plural: bool) -> None:
    minimum_key = "minItems" if plural else "minLength"
    maximum_key = "maxItems" if plural else "maxLength"
    size = len(value)
    minimum = schema.get(minimum_key)
    maximum = schema.get(maximum_key)
    if minimum is not None and (
        isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0
    ):
        raise ValueError(f"invalid {minimum_key} constraint for {path}")
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0
    ):
        raise ValueError(f"invalid {maximum_key} constraint for {path}")
    if minimum is not None and size < minimum:
        raise ValueError(f"{path} must contain at least {minimum} {'items' if plural else 'characters'}")
    if maximum is not None and size > maximum:
        raise ValueError(f"{path} must contain at most {maximum} {'items' if plural else 'characters'}")


def _validate_value(value: Any, schema: dict[str, Any], path: str) -> None:
    """Validate the supported, fail-closed JSON-Schema subset used for tool inputs."""
    expected = schema.get("type")
    valid_types = {"string", "integer", "number", "boolean", "object", "array"}
    if expected not in valid_types:
        raise ValueError(f"unsupported or missing schema type for {path}: {expected!r}")

    valid = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: (
            isinstance(item, int | float)
            and not isinstance(item, bool)
            and math.isfinite(item)
        ),
        "boolean": lambda item: isinstance(item, bool),
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
    }[expected](value)
    if not valid:
        raise ValueError(f"{path} must be {expected}")

    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or not choices:
            raise ValueError(f"invalid enum constraint for {path}")
        if value not in choices:
            raise ValueError(f"{path} must be one of the declared values")

    if expected == "string":
        _require_length(value, schema, path, plural=False)
        pattern = schema.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise ValueError(f"invalid pattern constraint for {path}")
            try:
                matches = re.search(pattern, value) is not None
            except re.error as exc:
                raise ValueError(f"invalid pattern constraint for {path}") from exc
            if not matches:
                raise ValueError(f"{path} does not match the required pattern")

    elif expected == "number":
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and (
            isinstance(minimum, bool)
            or not isinstance(minimum, int | float)
            or not math.isfinite(minimum)
        ):
            raise ValueError(f"invalid minimum constraint for {path}")
        if maximum is not None and (
            isinstance(maximum, bool)
            or not isinstance(maximum, int | float)
            or not math.isfinite(maximum)
        ):
            raise ValueError(f"invalid maximum constraint for {path}")
        if minimum is not None and value < minimum:
            raise ValueError(f"{path} must be greater than or equal to {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{path} must be less than or equal to {maximum}")

    elif expected == "integer":
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and (
            isinstance(minimum, bool) or not isinstance(minimum, int)
        ):
            raise ValueError(f"invalid minimum constraint for {path}")
        if maximum is not None and (
            isinstance(maximum, bool) or not isinstance(maximum, int)
        ):
            raise ValueError(f"invalid maximum constraint for {path}")
        if minimum is not None and value < minimum:
            raise ValueError(f"{path} must be greater than or equal to {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{path} must be less than or equal to {maximum}")

    elif expected == "array":
        _require_length(value, schema, path, plural=True)
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, dict):
                raise ValueError(f"invalid item schema for {path}")
            for index, item in enumerate(value):
                _validate_value(item, item_schema, f"{path}[{index}]")
        if schema.get("uniqueItems") is True:
            rendered = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value]
            if len(rendered) != len(set(rendered)):
                raise ValueError(f"{path} must contain unique items")

    elif expected == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or any(
            not isinstance(name, str) or not isinstance(child, dict)
            for name, child in properties.items()
        ):
            raise ValueError(f"invalid properties schema for {path}")
        if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            raise ValueError(f"invalid required list for {path}")
        undeclared_required = sorted(set(required) - set(properties))
        if undeclared_required:
            raise ValueError(
                f"required fields are not declared for {path}: {', '.join(undeclared_required)}"
            )
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"missing required fields for {path}: {', '.join(missing)}")
        additional = schema.get("additionalProperties", False)
        if not isinstance(additional, bool | dict):
            raise ValueError(f"invalid additionalProperties rule for {path}")
        for name, child_value in value.items():
            child_path = f"{path}.{name}"
            if name in properties:
                _validate_value(child_value, properties[name], child_path)
            elif additional is False:
                raise ValueError(f"unknown field '{child_path}'")
            elif isinstance(additional, dict):
                _validate_value(child_value, additional, child_path)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    effect: ToolEffect
    version: str
    handler: Callable[..., Any]
    effect_contract: EffectContract | None = None

    def validate(self, arguments: dict[str, Any]) -> None:
        """Validate tool arguments; undeclared fields are rejected by default."""
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")

        properties = self.input_schema.get("properties", {})
        required = self.input_schema.get("required", [])
        if not isinstance(properties, dict) or any(
            not isinstance(name, str) or not isinstance(schema, dict)
            for name, schema in properties.items()
        ):
            raise ValueError("input_schema.properties must map names to schema objects")
        if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            raise ValueError("input_schema.required must be a list of strings")
        undeclared_required = sorted(set(required) - set(properties))
        if undeclared_required:
            raise ValueError(
                "required fields are not declared: " + ", ".join(undeclared_required)
            )
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ValueError(f"missing required arguments: {', '.join(missing)}")

        additional = self.input_schema.get("additionalProperties", False)
        if not isinstance(additional, bool | dict):
            raise ValueError("input_schema.additionalProperties must be a boolean or schema object")

        for name, value in arguments.items():
            if name in properties:
                _validate_value(value, properties[name], f"argument '{name}'")
            elif additional is False:
                raise ValueError(f"unknown argument '{name}'")
            elif isinstance(additional, dict):
                _validate_value(value, additional, f"argument '{name}'")


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
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        effect_executor: EffectExecutor | None = None,
    ) -> None:
        self.registry = registry
        self.effect_executor = effect_executor

    def execute(
        self, name: str, arguments: dict[str, Any], timeout_seconds: float | None = None
    ) -> ToolResult:
        tool = self.registry.get(name)
        try:
            tool.validate(arguments)
        except (TypeError, ValueError) as exc:
            # Invalid input must never reach a handler and is not retryable by default.
            return ToolResult(
                success=False,
                error=f"invalid_tool_input: {type(exc).__name__}: {exc}",
            )

        if tool.effect_contract is not None:
            # Use the effect ledger's timeout path, not a generic wrapper that could
            # return "timeout" while concealing the status of an external mutation.
            if self.effect_executor is None:
                raise RuntimeError(
                    f"tool {name!r} declares an effect contract but no EffectExecutor was configured"
                )
            try:
                effect_result = self.effect_executor.execute(
                    contract=tool.effect_contract,
                    arguments=arguments,
                    handler=tool.handler,
                    handler_timeout_seconds=timeout_seconds,
                )
            except OperationConflictError:
                return ToolResult(
                    success=False,
                    error="operation_conflict: operation ID was reused with different request data",
                    effect_status="conflict",
                )
            success = effect_result.status is EffectStatus.CONFIRMED
            return ToolResult(
                success=success,
                output=effect_result.output,
                error=effect_result.error
                or (None if success else f"effect_{effect_result.status.value}"),
                attempts=effect_result.attempts,
                duplicate_suppressed=effect_result.duplicate_suppressed,
                effect_status=effect_result.status.value,
                operation_id=effect_result.operation_id,
                in_progress=effect_result.in_progress,
            )

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
