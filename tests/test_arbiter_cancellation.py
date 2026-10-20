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
        outcome=WorkerOutcome.PARTIALLY_SUCCEEDED,
        metadata={"continuation_pending": True},
    )

    assert deadline.outcome is WorkerOutcome.DEADLINE_EXCEEDED
    assert not deadline.successful
    assert interrupted.outcome is WorkerOutcome.PARTIALLY_SUCCEEDED
    assert (
        interrupted.successful
    )  # Legacy status remains compatible; outcome disambiguates it.


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


def test_worker_result_failure_gets_a_stable_default_category() -> None:
    result = WorkerResult(worker_id="uncategorized-worker", status=WorkerStatus.FAILED)

    assert result.error_code == "execution_failed"
    assert result.outcome.value == "failed"


def test_arbiter_preserves_policy_denial_and_never_retries_it() -> None:
    from loom_ai.worker import WorkerOutcome

    class DeniedWorker:
        worker_id = "denied-worker"

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, context: WorkerContext) -> WorkerResult:
            self.calls += 1
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.FAILED,
                outcome=WorkerOutcome.POLICY_DENIED,
                error="policy denied",
                error_code="policy_denied",
                retryable=True,
            )

    worker = DeniedWorker()

    def evaluator(result: WorkerResult, context: WorkerContext) -> WorkerEvaluation:
        return WorkerEvaluation(ArbiterDecision.RETRY)

    result = Arbiter(workers=[worker], evaluator=evaluator, max_retries=2).execute(
        WorkerContext(intent=Intent(goal="preserve policy denial"))
    )

    assert worker.calls == 1
    assert result.outcome is WorkerOutcome.POLICY_DENIED
    assert result.error == "policy denied"


def test_arbiter_preserves_worker_deadline_outcome() -> None:
    from loom_ai.worker import WorkerOutcome

    class DeadlineWorker:
        worker_id = "deadline-worker"

        def execute(self, context: WorkerContext) -> WorkerResult:
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.FAILED,
                outcome=WorkerOutcome.DEADLINE_EXCEEDED,
                error="worker deadline",
                error_code="deadline_exceeded",
            )

    result = Arbiter(
        workers=[DeadlineWorker()],
        evaluator=lambda result, context: WorkerEvaluation(ArbiterDecision.RETRY),
    ).execute(WorkerContext(intent=Intent(goal="preserve worker deadline")))

    assert result.outcome is WorkerOutcome.DEADLINE_EXCEEDED
    assert result.metadata["termination_cause"] == "deadline_exceeded"


def test_arbiter_preserves_artifacts_observations_and_provenance() -> None:
    class EvidenceWorker:
        worker_id = "evidence-worker"

        def execute(self, context: WorkerContext) -> WorkerResult:
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.SUCCESS,
                evidence=({"evidence_id": "e-1"},),
                artifacts=({"artifact_id": "a-1"},),
                observations=({"kind": "fact", "value": "observed"},),
                provenance={
                    "implementation_id": "fixture-worker",
                    "event_ids": ["event-1"],
                    "attributes": {"source": "test"},
                },
            )

    result = Arbiter(
        workers=[EvidenceWorker()],
        evaluator=lambda result, context: WorkerEvaluation(ArbiterDecision.COMPLETE),
    ).execute(WorkerContext(intent=Intent(goal="preserve result lineage")))

    assert result.artifacts == ({"artifact_id": "a-1"},)
    assert result.observations == ({"kind": "fact", "value": "observed"},)
    assert result.provenance["event_ids"] == ["event-1"]
    contributor = result.provenance["attributes"]["contributors"][0]
    assert contributor["provenance"]["attributes"]["source"] == "test"



def test_worker_result_requires_explicit_outcome_for_pending_continuation() -> None:
    import pytest

    with pytest.raises(ValueError, match="requires an explicit canonical outcome"):
        WorkerResult(
            worker_id="pending-worker",
            status=WorkerStatus.SUCCESS,
            metadata={"continuation_pending": True},
        )
