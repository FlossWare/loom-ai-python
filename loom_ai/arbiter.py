"""Composite Worker implementation for Loom orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from loom_ai.worker import Worker, WorkerContext, WorkerResult, WorkerStatus


class ArbiterDecision(str, Enum):
    """Decision applied after evaluating a Worker result."""

    COMPLETE = "complete"
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

            if result.status is WorkerStatus.CANCELLED:
                return self._result(outputs, evidence, "cancelled")

            evaluation = self.evaluator(result, child_context)
            if evaluation.decision is ArbiterDecision.COMPLETE:
                return self._result(outputs, evidence, "complete", evaluation.reason)

            if evaluation.decision is ArbiterDecision.RETRY:
                if attempts[worker.worker_id] > self.max_retries + 1:
                    return self._result(
                        outputs, evidence, "failed", "retry limit exceeded"
                    )
                continue

            if evaluation.decision is ArbiterDecision.REPLAN:
                queue[index + 1 : index + 1] = list(evaluation.workers)

            index += 1

        return self._result(outputs, evidence, "complete")

    def _result(
        self,
        outputs: list[WorkerResult],
        evidence: list[dict],
        status: str,
        reason: str = "",
    ) -> WorkerResult:
        successful = status == "complete"
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS if successful else WorkerStatus.FAILED,
            output=outputs,
            evidence=tuple(evidence),
            error="" if successful else reason,
            metadata={
                "arbiter_id": self.worker_id,
                "workers": [result.worker_id for result in outputs],
                "reason": reason,
            },
        )
