"""Composite Worker implementation for Loom orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from loom_ai.worker import (
    Worker,
    WorkerContext,
    WorkerOutcome,
    WorkerResult,
    WorkerStatus,
)


class ArbiterDecision(str, Enum):
    """Decision applied after evaluating a Worker result."""

    COMPLETE = "complete"
    INTERRUPT = "interrupt"
    CONTINUE = "continue"
    RETRY = "retry"
    REPLAN = "replan"


@dataclass(frozen=True)
class WorkerEvaluation:
    """Evaluation that tells an Arbiter what to do next."""

    decision: ArbiterDecision
    workers: tuple[Worker, ...] = ()
    reason: str = ""


Evaluator = Callable[[WorkerResult, WorkerContext], WorkerEvaluation]


@dataclass
class Arbiter:
    """A Worker that coordinates one or more Workers.

    Arbiter deliberately exposes the same ``execute`` contract as an ordinary
    Worker. This makes nested Arbiter -> Worker -> Arbiter composition ordinary
    composition rather than a second orchestration mechanism.
    """

    workers: Iterable[Worker]
    evaluator: Evaluator
    worker_id: str = "arbiter"
    max_retries: int = 1

    def __post_init__(self) -> None:
        self.workers = list(self.workers)
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")

    def execute(self, context: WorkerContext) -> WorkerResult:
        queue = list(self.workers)
        evidence: list[dict] = []
        attempts: dict[str, int] = {}
        outputs: list[WorkerResult] = []

        index = 0
        while index < len(queue):
            control_cause = context.termination_cause
            if control_cause == "cancelled":
                evidence.append(
                    {
                        "type": "cancellation-observed",
                        "reason": context.cancellation_token.reason,
                    }
                )
                return self._result(
                    outputs, evidence, "cancelled", context.cancellation_token.reason
                )
            if control_cause == "deadline_exceeded":
                evidence.append({"type": "deadline-exceeded"})
                return self._result(
                    outputs,
                    evidence,
                    "failed",
                    "deadline exceeded",
                    termination_cause="deadline_exceeded",
                )

            worker = queue[index]
            attempts[worker.worker_id] = attempts.get(worker.worker_id, 0) + 1
            child_context = context.with_evidence(*evidence)
            result = worker.execute(child_context)
            outputs.append(result)
            evidence.extend(result.evidence)
            evidence.append(
                {
                    "type": "worker-result",
                    "worker_id": result.worker_id,
                    "status": result.status.value,
                    "arbiter_id": self.worker_id,
                }
            )

            control_cause = context.termination_cause
            if control_cause == "cancelled":
                evidence.append(
                    {
                        "type": "cancellation-observed",
                        "reason": context.cancellation_token.reason,
                    }
                )
                return self._result(
                    outputs, evidence, "cancelled", context.cancellation_token.reason
                )
            if control_cause == "deadline_exceeded":
                evidence.append({"type": "deadline-exceeded"})
                return self._result(
                    outputs,
                    evidence,
                    "failed",
                    "deadline exceeded",
                    termination_cause="deadline_exceeded",
                )
            if result.outcome is WorkerOutcome.CANCELLED:
                return self._result(outputs, evidence, "cancelled")
            if result.outcome is WorkerOutcome.DEADLINE_EXCEEDED:
                return self._result(
                    outputs,
                    evidence,
                    "failed",
                    result.error or "deadline exceeded",
                    termination_cause="deadline_exceeded",
                )
            if result.outcome is WorkerOutcome.POLICY_DENIED:
                return self._result(
                    outputs, evidence, "policy_denied", result.error or "policy denied"
                )
            if result.outcome is WorkerOutcome.PARTIALLY_SUCCEEDED:
                return self._result(
                    outputs,
                    evidence,
                    "partial",
                    str(result.metadata.get("reason") or "nested Arbiter partially succeeded"),
                )

            evaluation = self.evaluator(result, child_context)
            if evaluation.decision is ArbiterDecision.COMPLETE:
                return self._result(outputs, evidence, "complete", evaluation.reason)

            if evaluation.decision is ArbiterDecision.INTERRUPT:
                return self._result(
                    outputs,
                    evidence,
                    "interrupted",
                    evaluation.reason or "execution interrupted before completion",
                    termination_cause="interrupted",
                )

            if evaluation.decision is ArbiterDecision.RETRY:
                if result.outcome in {
                    WorkerOutcome.POLICY_DENIED,
                    WorkerOutcome.CANCELLED,
                    WorkerOutcome.DEADLINE_EXCEEDED,
                }:
                    return self._result(
                        outputs,
                        evidence,
                        "failed",
                        result.error or f"{result.outcome.value} is terminal",
                        termination_cause=(
                            "deadline_exceeded"
                            if result.outcome is WorkerOutcome.DEADLINE_EXCEEDED
                            else None
                        ),
                    )
                if result.status is WorkerStatus.FAILED and not result.retryable:
                    return self._result(
                        outputs,
                        evidence,
                        "failed",
                        result.error or "worker failure is not retryable",
                    )
                # max_retries counts retries after the initial attempt. Once
                # this attempt reaches that limit, do not execute the worker
                # one extra time merely to discover that another retry is denied.
                if attempts[worker.worker_id] >= self.max_retries + 1:
                    return self._result(
                        outputs, evidence, "failed", "retry limit exceeded"
                    )
                continue

            if evaluation.decision is ArbiterDecision.REPLAN:
                queue[index + 1 : index + 1] = list(evaluation.workers)

            index += 1

        if outputs and outputs[-1].status is WorkerStatus.FAILED:
            return self._result(
                outputs,
                evidence,
                "failed",
                "worker failed without subsequent recovery",
            )

        return self._result(outputs, evidence, "complete")

    def _result(
        self,
        outputs: list[WorkerResult],
        evidence: list[dict],
        status: str,
        reason: str = "",
        *,
        termination_cause: str | None = None,
    ) -> WorkerResult:
        result_status = {
            "complete": WorkerStatus.SUCCESS,
            "interrupted": WorkerStatus.FAILED,
            "partial": WorkerStatus.SUCCESS,
            "failed": WorkerStatus.FAILED,
            "cancelled": WorkerStatus.CANCELLED,
            "policy_denied": WorkerStatus.FAILED,
        }[status]
        return WorkerResult(
            worker_id=self.worker_id,
            status=result_status,
            output=outputs,
            evidence=tuple(evidence),
            artifacts=tuple(
                artifact for result in outputs for artifact in result.artifacts
            ),
            observations=tuple(
                observation for result in outputs for observation in result.observations
            ),
            provenance={
                "implementation_id": self.worker_id,
                "event_ids": list(
                    dict.fromkeys(
                        event_id
                        for result in outputs
                        for event_id in result.provenance.get("event_ids", [])
                    )
                ),
                "attributes": {
                    "contributors": [
                        {
                            "worker_id": result.worker_id,
                            "provenance": dict(result.provenance),
                        }
                        for result in outputs
                    ]
                },
            },
            error="" if result_status is not WorkerStatus.FAILED else reason,
            error_code=(
                "deadline_exceeded"
                if termination_cause == "deadline_exceeded"
                else "execution_interrupted"
                if termination_cause == "interrupted"
                else "policy_denied"
                if status == "policy_denied"
                else "execution_failed"
                if result_status is WorkerStatus.FAILED
                else ""
            ),
            outcome={
                "complete": WorkerOutcome.SUCCEEDED,
                "interrupted": WorkerOutcome.FAILED,
                "partial": WorkerOutcome.PARTIALLY_SUCCEEDED,
                "failed": (
                    WorkerOutcome.DEADLINE_EXCEEDED
                    if termination_cause == "deadline_exceeded"
                    else WorkerOutcome.FAILED
                ),
                "cancelled": WorkerOutcome.CANCELLED,
                "policy_denied": WorkerOutcome.POLICY_DENIED,
            }[status],
            metadata={
                "arbiter_id": self.worker_id,
                "workers": [result.worker_id for result in outputs],
                "reason": reason,
                **(
                    {"continuation_pending": True}
                    if status in {"interrupted", "partial"}
                    else {}
                ),
                **(
                    {"termination_cause": termination_cause}
                    if termination_cause is not None
                    else {}
                ),
            },
        )
