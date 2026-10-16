"""Application service for Loom execution lifecycle and durable state.

Transport adapters translate requests into domain objects and delegate here.
The service is the single owner of Arbiter invocation and execution persistence.
"""

from __future__ import annotations

from loom_ai.arbiter import Arbiter
from loom_ai.execution_state import (
    ExecutionState,
    ExecutionStateStore,
)
from loom_ai.intent import Intent
from loom_ai.worker import WorkerContext, WorkerResult


class Orchestrator:
    """Own submission, observation, continuation, and durable lifecycle state."""

    def __init__(
        self,
        arbiter: Arbiter,
        *,
        execution_store: ExecutionStateStore | None = None,
    ) -> None:
        self._arbiter = arbiter
        self._execution_store = execution_store

    @property
    def durable(self) -> bool:
        """Whether this service can persist and observe execution identities."""
        return self._execution_store is not None

    def execute(self, intent: Intent) -> WorkerResult:
        """Execute an Intent without creating a durable execution record."""
        return self._arbiter.execute(WorkerContext(intent=intent))

    def submit(self, intent: Intent, execution_id: str) -> WorkerResult:
        """Submit an Intent, persisting lifecycle state when a store is configured."""
        if self._execution_store is None:
            return self.execute(intent)

        self._execution_store.save(
            ExecutionState(
                execution_id=execution_id,
                intent=intent,
                status="running",
                metadata={"phase": "initial"},
            )
        )
        result = self.execute(intent)
        self._execution_store.save(
            self._state_from_result(execution_id, intent, result, phase="initial")
        )
        return result

    def observe(self, execution_id: str) -> ExecutionState:
        """Return durable state for an execution or raise KeyError when absent."""
        store = self._require_store()
        execution = store.load(execution_id)
        if execution is None:
            raise KeyError(f"execution not found: {execution_id}")
        return execution

    def continue_execution(self, execution_id: str) -> WorkerResult:
        """Resume execution from persisted domain state and append new evidence."""
        store = self._require_store()
        execution = store.load(execution_id)
        if execution is None:
            raise KeyError(f"execution not found: {execution_id}")

        context = WorkerContext(
            intent=execution.intent,
            state=execution.state,
            evidence=execution.evidence,
            metadata=execution.metadata,
        )
        result = self._arbiter.execute(context)
        store.save(
            self._state_from_result(
                execution_id,
                execution.intent,
                result,
                phase="continued",
                prior=execution,
            )
        )
        return result

    def _require_store(self) -> ExecutionStateStore:
        if self._execution_store is None:
            raise RuntimeError("durable execution state is not configured")
        return self._execution_store

    @staticmethod
    def _state_from_result(
        execution_id: str,
        intent: Intent,
        result: WorkerResult,
        *,
        phase: str,
        prior: ExecutionState | None = None,
    ) -> ExecutionState:
        """Build durable observable state for one execution phase."""
        prior_evidence = prior.evidence if prior is not None else ()
        evidence = (
            prior_evidence
            + (
                {
                    "type": "execution-phase",
                    "phase": phase,
                    "execution_id": execution_id,
                },
            )
            + result.evidence
        )
        return ExecutionState(
            execution_id=execution_id,
            intent=intent,
            state={
                "last_worker": result.worker_id,
                "last_status": result.status.value,
                "phase": phase,
            },
            evidence=evidence,
            # Preserve the terminal domain status, especially cancellation.
            status=result.status.value,
            metadata={
                "phase": phase,
                "worker_id": result.worker_id,
                "result_metadata": dict(result.metadata),
            },
        )
