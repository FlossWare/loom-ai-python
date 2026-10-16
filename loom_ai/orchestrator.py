"""Application-facing orchestration service for Loom execution.

Transport adapters delegate lifecycle operations here. This Python realization
keeps execution identity, cancellation, deadlines, results, and durable state under
one application-facing authority.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from loom_ai.arbiter import Arbiter
from loom_ai.execution_state import ExecutionState, ExecutionStateStore
from loom_ai.intent import Intent
from loom_ai.worker import (
    CancellationToken,
    WorkerContext,
    WorkerResult,
    WorkerStatus,
)


@dataclass(frozen=True)
class OrchestratorSubmission:
    """Result of submitting or continuing an execution."""

    execution_id: str | None
    result: WorkerResult


@dataclass(frozen=True)
class CancellationResult:
    """Observable result of a cancellation request."""

    execution: ExecutionState
    accepted: bool
    pending: bool


class Orchestrator:
    """Own execution invocation, durable lifecycle, cancellation, and deadlines."""

    def __init__(
        self,
        arbiter: Arbiter,
        *,
        execution_store: ExecutionStateStore | None = None,
    ) -> None:
        self.arbiter = arbiter
        self.execution_store = execution_store
        self._active: dict[str, CancellationToken] = {}
        self._lock = RLock()

    @property
    def durable_enabled(self) -> bool:
        """Whether durable execution observation and continuation are available."""
        return self.execution_store is not None

    @property
    def durable(self) -> bool:
        """Compatibility alias for durable_enabled."""
        return self.durable_enabled

    def execute(self, intent: Intent) -> WorkerResult:
        """Execute an Intent without durable identity or external cancellation."""
        return self.arbiter.execute(WorkerContext(intent=intent))

    def submit(
        self,
        intent: Intent,
        *,
        execution_id: str | None = None,
        deadline: datetime | str | None = None,
    ) -> OrchestratorSubmission:
        """Submit an Intent with optional durable identity and absolute deadline."""
        absolute_deadline = parse_deadline(deadline)
        if absolute_deadline is not None and _is_expired(absolute_deadline):
            result = _deadline_result()
            if self.execution_store is None:
                return OrchestratorSubmission(None, result)
            actual_id = execution_id or str(uuid4())
            self.execution_store.save(
                _state_from_result(
                    actual_id,
                    intent,
                    result,
                    phase="initial",
                    deadline=absolute_deadline,
                )
            )
            return OrchestratorSubmission(actual_id, result)

        if self.execution_store is None:
            token = CancellationToken()
            return OrchestratorSubmission(
                None, self._execute_with_controls(intent, absolute_deadline, token)
            )

        actual_id = execution_id or str(uuid4())
        token = CancellationToken()
        with self._lock:
            metadata: dict = {"phase": "initial"}
            if absolute_deadline is not None:
                metadata["deadline"] = absolute_deadline.isoformat()
            self.execution_store.save(
                ExecutionState(
                    execution_id=actual_id,
                    intent=intent,
                    state={"phase": "initial"},
                    status="running",
                    metadata=metadata,
                )
            )
            self._active[actual_id] = token

        try:
            result = self._execute_with_controls(intent, absolute_deadline, token)
        except Exception as exc:
            with self._lock:
                if token.requested:
                    result = _cancelled_result(
                        _exception_result(exc, phase="initial"), token
                    )
                elif absolute_deadline is not None and _is_expired(absolute_deadline):
                    result = _deadline_result(
                        evidence=(
                            {
                                "type": "execution-exception",
                                "phase": "initial",
                                "exception_type": type(exc).__name__,
                            },
                        )
                    )
                else:
                    failure = _exception_result(exc, phase="initial")
                    self.execution_store.save(
                        _state_from_result(
                            actual_id,
                            intent,
                            failure,
                            phase="initial",
                            deadline=absolute_deadline,
                        )
                    )
                    self._active.pop(actual_id, None)
                    raise
                self.execution_store.save(
                    _state_from_result(
                        actual_id,
                        intent,
                        result,
                        phase="initial",
                        deadline=absolute_deadline,
                        cancellation=(
                            {"requested": True, "reason": token.reason}
                            if token.requested
                            else None
                        ),
                    )
                )
                self._active.pop(actual_id, None)
                return OrchestratorSubmission(actual_id, result)

        with self._lock:
            if token.requested and result.status is not WorkerStatus.CANCELLED:
                result = _cancelled_result(result, token)
            elif absolute_deadline is not None and _is_expired(absolute_deadline):
                result = _deadline_result(
                    evidence=result.evidence,
                    output=result.output,
                )
            cancellation = (
                {"requested": True, "reason": token.reason}
                if token.requested
                else None
            )
            self.execution_store.save(
                _state_from_result(
                    actual_id,
                    intent,
                    result,
                    phase="initial",
                    deadline=absolute_deadline,
                    cancellation=cancellation,
                )
            )
            self._active.pop(actual_id, None)
        return OrchestratorSubmission(actual_id, result)

    def observe(self, execution_id: str) -> ExecutionState:
        """Return durable execution state through the application boundary."""
        store = self._require_store()
        execution = store.load(execution_id)
        if execution is None:
            raise KeyError(f"execution not found: {execution_id}")
        return execution

    def cancel(
        self,
        execution_id: str,
        *,
        reason: str = "cancellation requested",
    ) -> CancellationResult:
        """Request cooperative cancellation and return its observable state."""
        store = self._require_store()
        with self._lock:
            execution = store.load(execution_id)
            if execution is None:
                raise KeyError(f"execution not found: {execution_id}")
            if execution.terminal:
                return CancellationResult(execution, accepted=False, pending=False)

            prior_request = execution.metadata.get("cancellation")
            token = self._active.get(execution_id)
            if isinstance(prior_request, dict) and prior_request.get("requested"):
                if token is not None:
                    token.cancel(str(prior_request.get("reason") or reason))
                return CancellationResult(
                    execution,
                    accepted=False,
                    pending=token is not None,
                )

            requested_at = datetime.now(timezone.utc).isoformat()
            cancellation = {
                "requested": True,
                "reason": reason,
                "requested_at": requested_at,
            }
            metadata = dict(execution.metadata)
            metadata["cancellation"] = cancellation
            evidence = execution.evidence + (
                {
                    "type": "cancellation-requested",
                    "reason": reason,
                    "requested_at": requested_at,
                },
            )
            state = dict(execution.state)
            state["cancellation_requested"] = True

            if token is not None:
                token.cancel(reason)
                updated = replace(
                    execution,
                    state=state,
                    evidence=evidence,
                    metadata=metadata,
                )
                store.save(updated)
                return CancellationResult(updated, accepted=True, pending=True)

            confirmed_at = datetime.now(timezone.utc).isoformat()
            evidence += (
                {"type": "cancellation-confirmed", "confirmed_at": confirmed_at},
            )
            state["last_status"] = WorkerStatus.CANCELLED.value
            metadata["cancellation"] = {
                **cancellation,
                "pending": False,
                "confirmed_at": confirmed_at,
            }
            updated = replace(
                execution,
                state=state,
                evidence=evidence,
                status="cancelled",
                metadata=metadata,
            )
            store.save(updated)
            return CancellationResult(updated, accepted=True, pending=False)

    def continue_execution(self, execution_id: str) -> OrchestratorSubmission:
        """Continue persisted context while preserving cancellation and deadlines."""
        store = self._require_store()
        with self._lock:
            execution = store.load(execution_id)
            if execution is None:
                raise KeyError(f"execution not found: {execution_id}")
            deadline = parse_deadline(execution.metadata.get("deadline"))
            prior_cancellation = execution.metadata.get("cancellation")
            if isinstance(prior_cancellation, dict) and prior_cancellation.get(
                "requested"
            ):
                result = _cancelled_result(
                    WorkerResult(
                        worker_id="orchestrator",
                        status=WorkerStatus.CANCELLED,
                        error="cancellation requested before continuation",
                    ),
                    CancellationToken(),
                )
                store.save(
                    _state_from_result(
                        execution_id,
                        execution.intent,
                        result,
                        phase="continued",
                        prior=execution,
                        deadline=deadline,
                        cancellation=prior_cancellation,
                    )
                )
                return OrchestratorSubmission(execution_id, result)
            if deadline is not None and _is_expired(deadline):
                result = _deadline_result()
                store.save(
                    _state_from_result(
                        execution_id,
                        execution.intent,
                        result,
                        phase="continued",
                        prior=execution,
                        deadline=deadline,
                    )
                )
                return OrchestratorSubmission(execution_id, result)

            token = CancellationToken()
            metadata = dict(execution.metadata)
            metadata["phase"] = "continued"
            store.save(replace(execution, status="running", metadata=metadata))
            self._active[execution_id] = token

        context = WorkerContext(
            intent=execution.intent,
            state=execution.state,
            evidence=execution.evidence,
            metadata=execution.metadata,
            deadline=deadline,
            cancellation_token=token,
        )
        try:
            result = self.arbiter.execute(context)
        except Exception as exc:
            with self._lock:
                if token.requested:
                    result = _cancelled_result(
                        _exception_result(exc, phase="continued"), token
                    )
                elif deadline is not None and _is_expired(deadline):
                    result = _deadline_result(
                        evidence=(
                            {
                                "type": "execution-exception",
                                "phase": "continued",
                                "exception_type": type(exc).__name__,
                            },
                        )
                    )
                else:
                    failure = _exception_result(exc, phase="continued")
                    store.save(
                        _state_from_result(
                            execution_id,
                            execution.intent,
                            failure,
                            phase="continued",
                            prior=execution,
                            deadline=deadline,
                        )
                    )
                    self._active.pop(execution_id, None)
                    raise
                store.save(
                    _state_from_result(
                        execution_id,
                        execution.intent,
                        result,
                        phase="continued",
                        prior=execution,
                        deadline=deadline,
                        cancellation=(
                            {"requested": True, "reason": token.reason}
                            if token.requested
                            else None
                        ),
                    )
                )
                self._active.pop(execution_id, None)
                return OrchestratorSubmission(execution_id, result)

        with self._lock:
            if token.requested and result.status is not WorkerStatus.CANCELLED:
                result = _cancelled_result(result, token)
            elif deadline is not None and _is_expired(deadline):
                result = _deadline_result(evidence=result.evidence, output=result.output)
            store.save(
                _state_from_result(
                    execution_id,
                    execution.intent,
                    result,
                    phase="continued",
                    prior=execution,
                    deadline=deadline,
                    cancellation=(
                        {"requested": True, "reason": token.reason}
                        if token.requested
                        else None
                    ),
                )
            )
            self._active.pop(execution_id, None)
        return OrchestratorSubmission(execution_id, result)

    def _execute_with_controls(
        self,
        intent: Intent,
        deadline: datetime | None,
        token: CancellationToken,
    ) -> WorkerResult:
        if deadline is not None and _is_expired(deadline):
            return _deadline_result()
        return self.arbiter.execute(
            WorkerContext(
                intent=intent,
                deadline=deadline,
                cancellation_token=token,
            )
        )

    def _require_store(self) -> ExecutionStateStore:
        if self.execution_store is None:
            raise RuntimeError("durable execution state is not configured")
        return self.execution_store


def parse_deadline(value: datetime | str | None) -> datetime | None:
    """Parse an absolute RFC 3339 deadline and normalize it to UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("deadline must not be empty")
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("deadline must be an RFC 3339 timestamp") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("deadline must include an explicit UTC offset")
    return value.astimezone(timezone.utc)


def _is_expired(deadline: datetime) -> bool:
    return datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc)


def _deadline_result(
    *,
    evidence: tuple[dict, ...] = (),
    output: object = None,
) -> WorkerResult:
    """Return an explicit deadline failure without starting new work."""
    return WorkerResult(
        worker_id="orchestrator",
        status=WorkerStatus.FAILED,
        output=output,
        evidence=evidence + ({"type": "deadline-exceeded"},),
        error="deadline exceeded",
        metadata={"termination_cause": "deadline_exceeded"},
    )


def _cancelled_result(result: WorkerResult, token: CancellationToken) -> WorkerResult:
    """Return a cancellation result after active work has actually stopped."""
    evidence = result.evidence + (
        {"type": "cancellation-observed", "reason": token.reason},
    )
    return WorkerResult(
        worker_id=result.worker_id,
        status=WorkerStatus.CANCELLED,
        output=result.output,
        evidence=evidence,
        error="",
        metadata={**result.metadata, "cancellation_requested": True},
    )


def _state_from_result(
    execution_id: str,
    intent: Intent,
    result: WorkerResult,
    *,
    phase: str,
    prior: ExecutionState | None = None,
    deadline: datetime | None = None,
    cancellation: dict | None = None,
) -> ExecutionState:
    """Build durable state while preserving deadline and cancellation metadata."""
    prior_evidence = prior.evidence if prior is not None else ()
    evidence = (
        prior_evidence
        + ({"type": "execution-phase", "phase": phase, "execution_id": execution_id},)
        + result.evidence
    )
    status = {
        WorkerStatus.SUCCESS: "success",
        WorkerStatus.FAILED: "failed",
        WorkerStatus.CANCELLED: "cancelled",
    }[result.status]
    metadata = dict(prior.metadata) if prior is not None else {}
    metadata.update(
        {
            "phase": phase,
            "worker_id": result.worker_id,
            "result_metadata": dict(result.metadata),
        }
    )
    if deadline is not None:
        metadata["deadline"] = deadline.isoformat()
    if cancellation is not None:
        metadata["cancellation"] = {
            **cancellation,
            "pending": result.status is not WorkerStatus.CANCELLED,
        }
    termination_cause = result.metadata.get("termination_cause")
    if termination_cause is not None:
        metadata["termination_cause"] = termination_cause
    return ExecutionState(
        execution_id=execution_id,
        intent=intent,
        state={
            "last_worker": result.worker_id,
            "last_status": result.status.value,
            "phase": phase,
        },
        evidence=evidence,
        status=status,
        metadata=metadata,
    )


def _exception_result(exc: Exception, *, phase: str) -> WorkerResult:
    """Create safe durable failure evidence before propagating an exception."""
    exception_type = type(exc).__name__
    return WorkerResult(
        worker_id="orchestrator",
        status=WorkerStatus.FAILED,
        error=f"execution raised {exception_type}",
        evidence=(
            {
                "type": "execution-exception",
                "phase": phase,
                "exception_type": exception_type,
            },
        ),
        metadata={"exception_type": exception_type},
    )
