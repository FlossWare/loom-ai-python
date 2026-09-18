"""Provider-neutral model invocation contracts for Loom Workers."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable


def _freeze(values: Mapping[str, str]) -> Mapping[str, str]:
    """Copy a mapping so the frozen contract cannot be mutated through it."""
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class ModelRequest:
    """Provider-neutral request supplied to a model provider."""

    prompt: str
    model: str = "default"
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("ModelRequest prompt must not be empty")
        if not self.model.strip():
            raise ValueError("ModelRequest model must not be empty")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class ModelResponse:
    """Provider-neutral model result with safe execution provenance."""

    text: str
    provider: str
    model: str
    finish_reason: str = "stop"
    metadata: Mapping[str, str] = field(default_factory=dict)
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("ModelResponse text must not be empty")
        if not self.provider.strip():
            raise ValueError("ModelResponse provider must not be empty")
        if not self.model.strip():
            raise ValueError("ModelResponse model must not be empty")
        if not self.finish_reason.strip():
            raise ValueError("ModelResponse finish_reason must not be empty")
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "provenance", _freeze(self.provenance))


@runtime_checkable
class ModelProvider(Protocol):
    """Stable synchronous provider-neutral model capability used by Workers.

    Async execution or threading bridges belong in provider adapters, not in
    this language-neutral contract.
    """

    @property
    def provider_id(self) -> str:
        """Stable provider identifier used for provenance."""

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a model response for a provider-neutral request."""
