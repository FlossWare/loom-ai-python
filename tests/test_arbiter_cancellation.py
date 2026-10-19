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


class FailedWorker:
    worker_id = "failed-worker"

    def __init__(self, *, retryable: bool) -> None:
        self.calls = 0
        self.retryable = retryable

    def execute(self, context: WorkerContext) -> WorkerResult:
        self.calls += 1
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.FAILED,
            error="temporary failure" if self.retryable else "permanent failure",
            error_code="temporary" if self.retryable else "permanent",
            retryable=self.retryable,
        )


def test_arbiter_does_not_retry_non_retryable_failure() -> None:
    worker = FailedWorker(retryable=False)

    def evaluator(result: WorkerResult, context: WorkerContext) -> WorkerEvaluation:
        return WorkerEvaluation(ArbiterDecision.RETRY)

    result = Arbiter(workers=[worker], evaluator=evaluator, max_retries=3).execute(
        WorkerContext(intent=Intent(goal="do not retry permanent failure"))
    )

    assert worker.calls == 1
    assert result.status is WorkerStatus.FAILED
    assert result.error == "permanent failure"


def test_arbiter_retries_explicitly_retryable_failure() -> None:
    worker = FailedWorker(retryable=True)

    def evaluator(result: WorkerResult, context: WorkerContext) -> WorkerEvaluation:
        if worker.calls == 1:
            return WorkerEvaluation(ArbiterDecision.RETRY)
        return WorkerEvaluation(ArbiterDecision.COMPLETE)

    result = Arbiter(workers=[worker], evaluator=evaluator, max_retries=1).execute(
        WorkerContext(intent=Intent(goal="retry temporary failure"))
    )

    assert worker.calls == 2
    assert result.status is WorkerStatus.SUCCESS


def test_worker_result_maps_deadline_and_interruption_to_canonical_outcomes() -> None:
    from loom_ai.worker import WorkerOutcome

    deadline = WorkerResult(
        worker_id="deadline-worker",
        status=WorkerStatus.FAILED,
        error="deadline exceeded",
        error_code="deadline_exceeded",
        metadata={"termination_cause": "deadline_exceeded"},
    )
    interrupted = WorkerResult(
        worker_id="interrupted-worker",
        status=WorkerStatus.SUCCESS,
        metadata={"continuation_pending": True},
    )

    assert deadline.outcome is WorkerOutcome.DEADLINE_EXCEEDED
    assert not deadline.successful
    assert interrupted.outcome is WorkerOutcome.PARTIALLY_SUCCEEDED
    assert not interrupted.successful


def test_worker_result_rejects_contradictory_status_and_outcome() -> None:
    import pytest

    from loom_ai.worker import WorkerOutcome

    with pytest.raises(ValueError, match="contradicts status"):
        WorkerResult(
            worker_id="contradictory-worker",
            status=WorkerStatus.SUCCESS,
            outcome=WorkerOutcome.FAILED,
            error="failed",
            error_code="execution_failed",
        )


def test_worker_result_failure_requires_a_failure_category() -> None:
    import pytest

    with pytest.raises(ValueError, match="requires a failure category"):
        WorkerResult(worker_id="uncategorized-worker", status=WorkerStatus.FAILED)
