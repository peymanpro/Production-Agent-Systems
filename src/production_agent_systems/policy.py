from __future__ import annotations

from dataclasses import dataclass

from .models import PolicyDecision, ToolEffect
from .tools import ToolDefinition


@dataclass(frozen=True)
class PermissionPolicy:
    allowed_tools: frozenset[str]
    mutation_requires_confirmation: bool = True

    def evaluate(
        self, tool: ToolDefinition, *, confirmed: bool = False
    ) -> PolicyDecision:
        if tool.name not in self.allowed_tools:
            return PolicyDecision.DENY
        if (
            tool.effect is ToolEffect.MUTATION
            and self.mutation_requires_confirmation
            and not confirmed
        ):
            return PolicyDecision.CONFIRMATION_REQUIRED
        return PolicyDecision.ALLOW
