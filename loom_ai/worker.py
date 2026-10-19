"""Canonical Loom Worker contract and execution value objects."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from threading import Event, Lock
from typing import Any, Mapping, Protocol

from loom_ai.intent import Intent


class WorkerStatus(str, Enum):
    """Legacy execution status retained for compatibility."""

    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerOutcome(str, Enum):
    """Canonical result outcome aligned with the language-neutral Loom contract."""

    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"
    POLICY_DENIED = "policy_denied"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class CancellationToken:
    """Thread-safe cooperative cancellation signal shared with active Workers."""

    def __init__(self) -> None:
        self._event = Event()
        self._lock = Lock()
        self._reason = "cancellation requested"
        self._requested_at: datetime | None = None

    def cancel(self, reason: str = "cancellation requested") -> None:
        """Request cancellation; repeated requests are safe and idempotent."""
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
                self._requested_at = datetime.now(timezone.utc)
                self._event.set()

    @property
    def requested(self) -> bool:
        """Whether cancellation has been requested."""
        return self._event.is_set()

    @property
    def reason(self) -> str:
        """The first cancellation reason supplied to this token."""
        return self._reason

    @property
    def requested_at(self) -> datetime | None:
        """UTC instant at which cancellation was first requested."""
        return self._requested_at

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for cancellation, returning whether it was requested."""
        return self._event.wait(timeout)


@dataclass(frozen=True)
class WorkerContext:
    """Explicit execution state supplied to a Worker."""

    intent: Intent
    state: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    deadline: datetime | None = None
    cancellation_token: CancellationToken = field(
        default_factory=CancellationToken,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.deadline is not None and (
            self.deadline.tzinfo is None or self.deadline.utcoffset() is None
        ):
            raise ValueError("deadline must include an explicit UTC offset")

    @property
    def cancellation_requested(self) -> bool:
        """Whether the execution's shared cancellation signal is set."""
        return self.cancellation_token.requested

    @property
    def deadline_expired(self) -> bool:
        """Whether the optional absolute deadline has elapsed."""
        return self.deadline is not None and datetime.now(timezone.utc) >= (
            self.deadline.astimezone(timezone.utc)
        )

    @property
    def termination_cause(self) -> str | None:
        """Resolve cancellation/deadline races by their first recorded instant."""
        if self.deadline_expired:
            requested_at = self.cancellation_token.requested_at
            if (
                requested_at is None
                or self.deadline is None
                or requested_at >= self.deadline.astimezone(timezone.utc)
            ):
                return "deadline_exceeded"
        if self.cancellation_requested:
            return "cancelled"
        if self.deadline_expired:
            return "deadline_exceeded"
        return None

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
    artifacts: tuple[Mapping[str, Any], ...] = ()
    observations: tuple[Mapping[str, Any], ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    retryable: bool = False
    outcome: WorkerOutcome | None = None

    def __post_init__(self) -> None:
        """Normalize legacy status to a canonical outcome and reject contradictions."""
        if not self.worker_id.strip():
            raise ValueError("worker_id must not be empty")
        outcome = self.outcome
        if outcome is None:
            termination_cause = self.metadata.get("termination_cause")
            if self.status is WorkerStatus.CANCELLED:
                outcome = WorkerOutcome.CANCELLED
            elif self.status is WorkerStatus.FAILED:
                outcome = (
                    WorkerOutcome.DEADLINE_EXCEEDED
                    if termination_cause == "deadline_exceeded"
                    else WorkerOutcome.FAILED
                )
            elif self.metadata.get("continuation_pending") is True:
                outcome = WorkerOutcome.PARTIALLY_SUCCEEDED
            else:
                outcome = WorkerOutcome.SUCCEEDED
            object.__setattr__(self, "outcome", outcome)

        compatible_statuses = {
            WorkerOutcome.SUCCEEDED: WorkerStatus.SUCCESS,
            WorkerOutcome.PARTIALLY_SUCCEEDED: WorkerStatus.SUCCESS,
            WorkerOutcome.FAILED: WorkerStatus.FAILED,
            WorkerOutcome.POLICY_DENIED: WorkerStatus.FAILED,
            WorkerOutcome.CANCELLED: WorkerStatus.CANCELLED,
            WorkerOutcome.DEADLINE_EXCEEDED: WorkerStatus.FAILED,
        }
        if compatible_statuses[outcome] is not self.status:
            raise ValueError(
                f"outcome {outcome.value!r} contradicts status {self.status.value!r}"
            )
        if outcome in {
            WorkerOutcome.FAILED,
            WorkerOutcome.POLICY_DENIED,
            WorkerOutcome.CANCELLED,
            WorkerOutcome.DEADLINE_EXCEEDED,
        } and not self.error_code:
            category = {
                WorkerOutcome.FAILED: "execution_failed",
                WorkerOutcome.POLICY_DENIED: "policy_denied",
                WorkerOutcome.CANCELLED: "cancelled",
                WorkerOutcome.DEADLINE_EXCEEDED: "deadline_exceeded",
            }[outcome]
            object.__setattr__(self, "error_code", category)

    @property
    def successful(self) -> bool:
        """Compatibility view of the legacy status; inspect outcome for partial success."""
        return self.status is WorkerStatus.SUCCESS


class Worker(Protocol):
    """Stable executable abstraction used by Loom composition."""

    @property
    def worker_id(self) -> str:
        """Stable identifier used for provenance."""

    def execute(self, context: WorkerContext) -> WorkerResult:
        """Execute against explicit state and return a structured result."""
