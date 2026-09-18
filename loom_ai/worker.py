"""Canonical Loom Worker contract and execution value objects."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Protocol

from loom_ai.intent import Intent


class WorkerStatus(str, Enum):
    """Outcome of a Worker invocation."""

    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WorkerContext:
    """Explicit execution state supplied to a Worker."""

    intent: Intent
    state: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def with_state(self, **updates: Any) -> "WorkerContext":
        """Return a context with explicit state updates."""
        state = dict(self.state)
        state.update(updates)
        return replace(self, state=state)

    def with_evidence(self, *items: Mapping[str, Any]) -> "WorkerContext":
        """Return a context with additional evidence."""
        return replace(self, evidence=self.evidence + tuple(items))


@dataclass(frozen=True)
class WorkerResult:
    """Structured result returned by a Worker."""

    worker_id: str
    status: WorkerStatus
    output: Any = None
    evidence: tuple[Mapping[str, Any], ...] = ()
    error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def successful(self) -> bool:
        return self.status is WorkerStatus.SUCCESS


class Worker(Protocol):
    """Stable executable abstraction used by Loom composition."""

    @property
    def worker_id(self) -> str:
        """Stable identifier used for provenance."""

    def execute(self, context: WorkerContext) -> WorkerResult:
        """Execute against explicit state and return a structured result."""
