"""Bridge Orchestrator Worker calls to independently running fleet Workers."""

from __future__ import annotations

import time
from uuid import uuid4

from loom_ai.fleet import FleetCoordinator, FleetTaskStatus
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class FleetDispatchWorker:
    """Worker implementation that delegates each invocation to a fleet agent.

    The Orchestrator still owns the execution lifecycle and Arbiter decision.
    This Worker submits one correlated fleet task and waits for its terminal
    result; the fleet HTTP server is used by remote agents to claim and report it.
    """

    worker_id = "fleet-dispatch"

    def __init__(
        self,
        coordinator: FleetCoordinator,
        *,
        required_capabilities: tuple[str, ...] | list[str],
        max_attempts: int = 3,
        wait_timeout_seconds: float = 900,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if wait_timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("wait and poll intervals must be positive")
        self.coordinator = coordinator
        self.required_capabilities = tuple(sorted(set(required_capabilities)))
        if any(not capability.strip() for capability in self.required_capabilities):
            raise ValueError("required capabilities must not contain empty values")
        self.max_attempts = max_attempts
        self.wait_timeout_seconds = wait_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def execute(self, context: WorkerContext) -> WorkerResult:
        """Submit work and wait for a result, respecting cancellation and deadline."""
        execution_id = context.metadata.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id.strip():
            execution_id = str(uuid4())
        phase = context.metadata.get("phase", "initial")
        task = self.coordinator.submit_task(
            context.intent,
            execution_id=execution_id,
            task_id=str(uuid4()),
            idempotency_key=f"{execution_id}:{phase}:{uuid4()}",
            required_capabilities=self.required_capabilities,
            max_attempts=self.max_attempts,
            deadline=context.deadline,
        )
        expires_at = time.monotonic() + self.wait_timeout_seconds

        while True:
            task = self.coordinator.observe_task(task.task_id)
            if task.status in {
                FleetTaskStatus.SUCCEEDED,
                FleetTaskStatus.FAILED,
                FleetTaskStatus.CANCELLED,
            }:
                if task.result is None:
                    return WorkerResult(
                        worker_id=self.worker_id,
                        status=WorkerStatus.FAILED,
                        error="fleet task reached a terminal state without a result",
                        metadata={
                            "termination_cause": "invalid_fleet_terminal_state",
                            "fleet_task_id": task.task_id,
                        },
                    )
                return WorkerResult(
                    worker_id=task.result.worker_id,
                    status=task.result.status,
                    output=task.result.output,
                    evidence=task.result.evidence,
                    error=task.result.error,
                    metadata={
                        **task.result.metadata,
                        "fleet_task_id": task.task_id,
                        "fleet_execution_id": task.execution_id,
                        "fleet_attempt": task.attempt,
                    },
                    error_code=task.result.error_code,
                    retryable=task.result.retryable,
                    outcome=task.result.outcome,
                    artifacts=task.result.artifacts,
                    observations=task.result.observations,
                    provenance=task.result.provenance,
                )

            termination_cause = context.termination_cause
            if termination_cause == "deadline_exceeded":
                self.coordinator.cancel_task(
                    task.task_id,
                    reason="execution deadline exceeded while waiting for fleet Worker",
                    termination_cause="deadline_exceeded",
                )
                continue
            if termination_cause == "cancelled":
                self.coordinator.cancel_task(
                    task.task_id,
                    reason=context.cancellation_token.reason,
                    termination_cause="cancelled",
                )
                continue
            remaining = expires_at - time.monotonic()
            if remaining <= 0:
                self.coordinator.cancel_task(
                    task.task_id,
                    reason="fleet Worker result timed out",
                    termination_cause="worker_timeout",
                )
                continue
            time.sleep(min(self.poll_interval_seconds, remaining))
