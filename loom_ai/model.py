"""Provider-neutral model invocation contracts for Loom Workers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable


def _freeze(values: Mapping[str, str]) -> Mapping[str, str]:
    """Copy a mapping so the frozen contract cannot be mutated through it."""
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class ModelToolDefinition:
    """Provider-neutral definition of a tool a model is permitted to request."""

    name: str
    description: str
    input_schema: Mapping[str, object] = field(
        default_factory=lambda: {"type": "object"}
    )

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ModelToolDefinition name must not be empty")
        if not self.description.strip():
            raise ValueError("ModelToolDefinition description must not be empty")
        if not isinstance(self.input_schema, Mapping):
            raise ValueError("ModelToolDefinition input_schema must be an object")
        if any(not isinstance(key, str) for key in self.input_schema):
            raise ValueError("ModelToolDefinition schema keys must be strings")
        try:
            json.dumps(dict(self.input_schema), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "ModelToolDefinition input_schema must be JSON-compatible"
            ) from exc
        object.__setattr__(
            self, "input_schema", MappingProxyType(dict(self.input_schema))
        )


@dataclass(frozen=True)
class ModelToolResult:
    """Structured result returned to the provider after a trusted tool invocation."""

    call_id: str
    tool_name: str
    result: object

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("ModelToolResult call_id must not be empty")
        if not self.tool_name.strip():
            raise ValueError("ModelToolResult tool_name must not be empty")
        try:
            json.dumps(self.result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("ModelToolResult result must be JSON-compatible") from exc


@dataclass(frozen=True)
class ModelRequest:
    """Provider-neutral request supplied to a model provider.

    ``timeout_seconds`` is an optional positive finite transport budget that
    provider adapters are required to honor when supplied. Tool definitions and
    prior tool results remain structured fields rather than prompt text.
    """

    prompt: str
    model: str = "default"
    metadata: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None
    tools: tuple[ModelToolDefinition, ...] = ()
    tool_results: tuple[ModelToolResult, ...] = ()

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("ModelRequest prompt must not be empty")
        if not self.model.strip():
            raise ValueError("ModelRequest model must not be empty")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError(
                "ModelRequest timeout_seconds must be a positive finite number"
            )
        tool_names = [tool.name for tool in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("ModelRequest tool names must be unique")
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "tool_results", tuple(self.tool_results))


@dataclass(frozen=True)
class ModelToolCall:
    """Provider-neutral request to invoke one explicitly configured capability."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("ModelToolCall call_id must not be empty")
        if not self.tool_name.strip():
            raise ValueError("ModelToolCall tool_name must not be empty")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("ModelToolCall arguments must be an object")
        if any(not isinstance(key, str) for key in self.arguments):
            raise ValueError("ModelToolCall argument keys must be strings")
        try:
            json.dumps(dict(self.arguments), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("ModelToolCall arguments must be JSON-compatible") from exc
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True)
class ModelResponse:
    """Provider-neutral final response or structured capability request."""

    text: str
    provider: str
    model: str
    finish_reason: str = "stop"
    metadata: Mapping[str, str] = field(default_factory=dict)
    provenance: Mapping[str, str] = field(default_factory=dict)
    kind: str = "final"
    tool_calls: tuple[ModelToolCall, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"final", "tool_calls"}:
            raise ValueError("ModelResponse kind must be 'final' or 'tool_calls'")
        if self.kind == "final" and not self.text.strip():
            raise ValueError("final ModelResponse text must not be empty")
        if self.kind == "tool_calls" and (self.text.strip() or not self.tool_calls):
            raise ValueError("tool-call ModelResponse requires calls and no text")
        if self.kind == "final" and self.tool_calls:
            raise ValueError("final ModelResponse must not contain tool calls")
        call_ids = [call.call_id for call in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("ModelResponse tool call identifiers must be unique")
        if not self.provider.strip():
            raise ValueError("ModelResponse provider must not be empty")
        if not self.model.strip():
            raise ValueError("ModelResponse model must not be empty")
        if not self.finish_reason.strip():
            raise ValueError("ModelResponse finish_reason must not be empty")
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "provenance", _freeze(self.provenance))
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))


@runtime_checkable
class ModelProvider(Protocol):
    """Stable synchronous provider-neutral model capability used by Workers.

    Async execution or threading bridges belong in provider adapters, not in
    this language-neutral contract. When a request supplies timeout_seconds, a
    provider MUST apply it at its transport boundary and raise TimeoutError when
    the request exceeds that budget; a provider MUST NOT silently ignore it.
    """

    @property
    def provider_id(self) -> str:
        """Stable provider identifier used for provenance."""

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a model response for a provider-neutral request."""
