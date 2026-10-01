from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelCompletion:
    content: str
    model: str
    usage: dict[str, int] | None = None
    metadata: dict[str, Any] | None = None


class ModelProvider(Protocol):
    def complete(self, prompt: str, *, model: str | None = None) -> ModelCompletion: ...
