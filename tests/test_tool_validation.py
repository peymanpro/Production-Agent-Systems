from __future__ import annotations

from typing import Any

import pytest

from production_agent_systems import ToolDefinition, ToolEffect, ToolExecutor, ToolRegistry


def make_tool(
    schema: dict[str, Any],
    handler: Any = None,
) -> ToolDefinition:
    return ToolDefinition(
        name="sample",
        description="Test tool",
        input_schema=schema,
        effect=ToolEffect.MUTATION,
        version="1",
        handler=handler or (lambda **kwargs: kwargs),
    )


def test_rejects_undeclared_arguments_by_default() -> None:
    tool = make_tool(
        {
            "required": ["customer_id"],
            "properties": {"customer_id": {"type": "string"}},
        }
    )

    with pytest.raises(ValueError, match="unknown argument 'is_admin'"):
        tool.validate({"customer_id": "C-42", "is_admin": True})


def test_rejects_boolean_where_integer_is_required() -> None:
    tool = make_tool(
        {
            "required": ["limit"],
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 10}},
        }
    )

    with pytest.raises(ValueError, match="argument 'limit' must be integer"):
        tool.validate({"limit": True})


def test_validates_nested_objects_and_rejects_unknown_nested_fields() -> None:
    tool = make_tool(
        {
            "required": ["account"],
            "properties": {
                "account": {
                    "type": "object",
                    "required": ["account_id"],
                    "properties": {"account_id": {"type": "string", "minLength": 1}},
                }
            },
        }
    )

    with pytest.raises(ValueError, match="unknown field 'argument 'account'.is_admin'"):
        tool.validate({"account": {"account_id": "A-1", "is_admin": True}})


def test_enforces_enum_and_string_constraints() -> None:
    tool = make_tool(
        {
            "required": ["status", "request_id"],
            "properties": {
                "status": {"type": "string", "enum": ["active", "suspended"]},
                "request_id": {"type": "string", "minLength": 3, "maxLength": 12, "pattern": r"^req-"},
            },
        }
    )

    with pytest.raises(ValueError, match="declared values"):
        tool.validate({"status": "admin", "request_id": "req-123"})

    with pytest.raises(ValueError, match="required pattern"):
        tool.validate({"status": "active", "request_id": "bad-123"})


def test_validates_array_items_and_uniqueness() -> None:
    tool = make_tool(
        {
            "required": ["ids"],
            "properties": {
                "ids": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "integer", "minimum": 1},
                }
            },
        }
    )

    with pytest.raises(ValueError, match="unique items"):
        tool.validate({"ids": [1, 1]})

    with pytest.raises(ValueError, match=r"argument 'ids'\[1\] must be integer"):
        tool.validate({"ids": [1, True]})


def test_additional_properties_can_be_allowed_only_with_a_schema() -> None:
    tool = make_tool(
        {
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
            "additionalProperties": {"type": "string", "maxLength": 8},
        }
    )

    assert tool.validate({"name": "alpha", "label": "demo"}) is None
    with pytest.raises(ValueError, match="must be string"):
        tool.validate({"name": "alpha", "label": 123})


def test_executor_never_calls_handler_for_invalid_arguments() -> None:
    calls: list[dict[str, Any]] = []
    tool = make_tool(
        {
            "required": ["customer_id"],
            "properties": {"customer_id": {"type": "string"}},
        },
        handler=lambda **kwargs: calls.append(kwargs),
    )
    executor = ToolExecutor(ToolRegistry([tool]))

    result = executor.execute(
        "sample",
        {"customer_id": "C-42", "bypass_policy": True},
        timeout_seconds=0.1,
    )

    assert result.success is False
    assert result.error is not None and "unknown argument" in result.error
    assert calls == []
