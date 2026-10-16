"""Application-facing orchestration service for Loom execution.

HTTP, MCP, and CLI/TUI adapters should delegate through this boundary. The
language-neutral Orchestrator semantics are defined by FlossWare/loom-ai; this
module is the Python realization.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from loom_ai.arbiter import Arbiter
from loom_ai.execution_state import (
    ExecutionState,
    ExecutionStateStore,
)
from loom_ai.intent import Intent
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


@dataclass(frozen=True)
class OrchestratorSubmission:
    """Result of submitting or continuing an execution."""

    execution_id: str | None
    result: WorkerResult


class Orchestrator:
    """Own execution invocation, durable lifecycle, and observable state."""

    def __init__(
        self,
        arbiter: Arbiter,
        *,
        execution_store: ExecutionStateStore | None = None,
    ) -> None:
        self.arbiter = arbiter
        self.execution_store = execution_store

    @property
    def durable_enabled(self) -> bool:
        """Whether durable execution observation and continuation are available."""
        return self.execution_store is not None

    def execute(self, intent: Intent) -> WorkerResult:
        """Execute an Intent through the configured Arbiter."""
        return self.arbiter.execute(WorkerContext(intent=intent))

    def submit(
        self,
        intent: Intent,
        *,
        execution_id: str | None = None,
    ) -> OrchestratorSubmission:
        """Submit an Intent and persist lifecycle state when configured."""
        if self.execution_store is None:
            return OrchestratorSubmission(None, self.execute(intent))

        actual_id = execution_id or str(uuid4())
        self.execution_store.save(
            ExecutionState(
                execution_id=actual_id,
                intent=intent,
                state={"phase": "initial"},
                status="running",
                metadata={"phase": "initial"},
            )
        )
        try:
            result = self.execute(intent)
        except Exception as exc:
            failure = _exception_result(exc, phase="initial")
            self.execution_store.save(
                _state_from_result(actual_id, intent, failure, phase="initial")
            )
            raise

        self.execution_store.save(
            _state_from_result(actual_id, intent, result, phase="initial")
        )
        return OrchestratorSubmission(actual_id, result)

    def observe(self, execution_id: str) -> ExecutionState:
        """Return durable execution state through the application boundary."""
        if self.execution_store is None:
            raise RuntimeError("durable execution state is not configured")
        execution = self.execution_store.load(execution_id)
        if execution is None:
            raise KeyError(f"execution not found: {execution_id}")
        return execution

    def continue_execution(self, execution_id: str) -> OrchestratorSubmission:
        """Continue an execution from its persisted context and evidence."""
        execution = self.observe(execution_id)
        context = WorkerContext(
            intent=execution.intent,
            state=execution.state,
            evidence=execution.evidence,
            metadata=execution.metadata,
        )
        try:
            result = self.arbiter.execute(context)
        except Exception as exc:
            failure = _exception_result(exc, phase="continued")
            self.execution_store.save(
                _state_from_result(
                    execution_id,
                    execution.intent,
                    failure,
                    phase="continued",
                    prior=execution,
                )
            )
            raise

        self.execution_store.save(
            _state_from_result(
                execution_id,
                execution.intent,
                result,
                phase="continued",
                prior=execution,
            )
        )
        return OrchestratorSubmission(execution_id, result)


def _state_from_result(
    execution_id: str,
    intent: Intent,
    result: WorkerResult,
    *,
    phase: str,
    prior: ExecutionState | None = None,
) -> ExecutionState:
    """Build durable state without conflating cancellation with failure."""
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
        metadata={
            "phase": phase,
            "worker_id": result.worker_id,
            "result_metadata": dict(result.metadata),
        },
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
