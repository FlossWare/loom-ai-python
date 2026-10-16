"""Regression tests for Arbiter lifecycle outcome propagation."""

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.intent import Intent
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class CancelledWorker:
    worker_id = "cancelled-worker"

    def execute(self, context: WorkerContext) -> WorkerResult:
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.CANCELLED,
            error="cancelled by request",
        )


def test_arbiter_preserves_cancelled_status() -> None:
    evaluator_calls = []

    def evaluator(result: WorkerResult, context: WorkerContext) -> WorkerEvaluation:
        evaluator_calls.append(result)
        return WorkerEvaluation(ArbiterDecision.COMPLETE)

    arbiter = Arbiter(workers=[CancelledWorker()], evaluator=evaluator)
    result = arbiter.execute(WorkerContext(intent=Intent(goal="exercise cancellation")))

    assert result.status is WorkerStatus.CANCELLED
    assert result.error == ""
    assert result.output[0].status is WorkerStatus.CANCELLED
    assert result.metadata["reason"] == ""
    assert evaluator_calls == []


class SuccessfulWorker:
    worker_id = "successful-worker"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, context: WorkerContext) -> WorkerResult:
        self.calls += 1
        return WorkerResult(worker_id=self.worker_id, status=WorkerStatus.SUCCESS)


def test_arbiter_retry_limit_counts_retries_after_initial_attempt() -> None:
    worker = SuccessfulWorker()

    def evaluator(result: WorkerResult, context: WorkerContext) -> WorkerEvaluation:
        return WorkerEvaluation(ArbiterDecision.RETRY)

    arbiter = Arbiter(workers=[worker], evaluator=evaluator, max_retries=1)
    result = arbiter.execute(WorkerContext(intent=Intent(goal="exercise retry limit")))

    assert worker.calls == 2
    assert len(result.output) == 2
    assert result.status is WorkerStatus.FAILED
    assert result.error == "retry limit exceeded"
