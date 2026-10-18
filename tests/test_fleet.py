from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from loom_ai.fleet import (
    FleetConflictError,
    FleetCoordinator,
    FleetTaskStatus,
    StaleLeaseError,
    WorkerLifecycle,
    WorkerNotRegisteredError,
)
from loom_ai.intent import Intent
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class EchoWorker:
    def __init__(self, worker_id: str, output: str = "done") -> None:
        self._worker_id = worker_id
        self.output = output
        self.calls = 0

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def execute(self, context: WorkerContext) -> WorkerResult:
        self.calls += 1
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output=self.output,
            evidence=({"type": "worker.completed", "intent_id": context.intent.intent_id},),
        )


def setup_coordinator() -> tuple[FleetCoordinator, FakeClock]:
    clock = FakeClock()
    return FleetCoordinator(heartbeat_timeout_seconds=10, clock=clock), clock


def test_capability_matching_leases_task_and_commits_result() -> None:
    coordinator, _ = setup_coordinator()
    coordinator.register_worker("worker-a", "impl-a", ["repository.read"])
    coordinator.register_worker("worker-b", "impl-b", ["verification.run"])
    task = coordinator.submit_task(
        Intent(goal="read a file"),
        execution_id="exec-1",
        task_id="task-1",
        required_capabilities=["repository.read"],
    )

    assert coordinator.claim_task("worker-b") is None
    leased = coordinator.claim_task("worker-a")
    assert leased is not None
    assert leased.lease is not None
    assert leased.lease.generation == 1

    completed = coordinator.execute_claimed_task("task-1", EchoWorker("worker-a"))

    assert completed.status is FleetTaskStatus.SUCCEEDED
    assert completed.result is not None
    assert completed.result.output == "done"
    assert completed.execution_id == task.execution_id
    assert completed.attempt_history[0]["worker_id"] == "worker-a"


def test_expired_lease_is_requeued_and_stale_result_is_rejected() -> None:
    coordinator, clock = setup_coordinator()
    coordinator.register_worker("worker-a", "impl-a", ["repository.read"], lease_seconds=2)
    coordinator.register_worker("worker-b", "impl-b", ["repository.read"], lease_seconds=5)
    coordinator.submit_task(
        Intent(goal="read a file"),
        execution_id="exec-1",
        task_id="task-1",
        required_capabilities=["repository.read"],
        max_attempts=2,
    )
    first = coordinator.claim_task("worker-a")
    assert first is not None and first.lease is not None

    clock.advance(3)
    expired = coordinator.expire_leases()
    assert len(expired) == 1
    assert expired[0].status is FleetTaskStatus.QUEUED

    with pytest.raises(StaleLeaseError):
        coordinator.submit_result(
            "task-1",
            worker_id="worker-a",
            lease_id=first.lease.lease_id,
            generation=first.lease.generation,
            result=WorkerResult(
                worker_id="worker-a", status=WorkerStatus.SUCCESS, output="stale"
            ),
        )

    second = coordinator.claim_task("worker-b")
    assert second is not None and second.lease is not None
    assert second.lease.generation == 2
    assert second.lease.worker_id == "worker-b"


def test_duplicate_submission_returns_existing_task_without_resetting_state() -> None:
    coordinator, _ = setup_coordinator()
    intent = Intent(goal="write report", intent_id="intent-1")
    first = coordinator.submit_task(
        intent,
        execution_id="exec-1",
        task_id="task-1",
        idempotency_key="idem-1",
        required_capabilities=["report.write"],
    )
    duplicate = coordinator.submit_task(
        intent,
        execution_id="exec-1",
        task_id="task-1",
        idempotency_key="idem-1",
        required_capabilities=["report.write"],
    )

    assert duplicate == first
    with pytest.raises(FleetConflictError):
        coordinator.submit_task(
            Intent(goal="different intent", intent_id="intent-2"),
            execution_id="exec-1",
            task_id="task-1",
            idempotency_key="idem-1",
            required_capabilities=["report.write"],
        )


def test_retryable_failure_requeues_with_prior_attempt_evidence() -> None:
    coordinator, _ = setup_coordinator()
    coordinator.register_worker("worker-a", "impl-a", ["test.run"])
    task = coordinator.submit_task(
        Intent(goal="run tests"),
        execution_id="exec-1",
        task_id="task-1",
        required_capabilities=["test.run"],
        max_attempts=2,
    )
    leased = coordinator.claim_task("worker-a")
    assert leased is not None and leased.lease is not None
    requeued = coordinator.submit_result(
        task.task_id,
        worker_id="worker-a",
        lease_id=leased.lease.lease_id,
        generation=leased.lease.generation,
        result=WorkerResult(
            worker_id="worker-a",
            status=WorkerStatus.FAILED,
            error="temporary runner error",
            metadata={"retryable": True},
        ),
        retryable=True,
    )

    assert requeued.status is FleetTaskStatus.QUEUED
    assert requeued.result is None
    assert requeued.attempt_history[0]["error"] == "temporary runner error"
    assert coordinator.claim_task("worker-a").attempt == 2


def test_terminal_result_is_immutable_under_duplicate_submission() -> None:
    coordinator, _ = setup_coordinator()
    coordinator.register_worker("worker-a", "impl-a", ["search.read"])
    coordinator.submit_task(
        Intent(goal="search"),
        execution_id="exec-1",
        task_id="task-1",
        required_capabilities=["search.read"],
    )
    leased = coordinator.claim_task("worker-a")
    assert leased is not None and leased.lease is not None
    success = WorkerResult(
        worker_id="worker-a", status=WorkerStatus.SUCCESS, output={"matches": 1}
    )
    completed = coordinator.submit_result(
        "task-1",
        worker_id="worker-a",
        lease_id=leased.lease.lease_id,
        generation=leased.lease.generation,
        result=success,
    )
    duplicate = coordinator.submit_result(
        "task-1",
        worker_id="worker-a",
        lease_id="stale-lease",
        generation=0,
        result=WorkerResult(
            worker_id="worker-a", status=WorkerStatus.FAILED, error="overwrite"
        ),
    )

    assert completed.status is FleetTaskStatus.SUCCEEDED
    assert duplicate.result == success
    assert duplicate.status is FleetTaskStatus.SUCCEEDED


def test_stale_heartbeat_marks_worker_unhealthy() -> None:
    coordinator, clock = setup_coordinator()
    coordinator.register_worker("worker-a", "impl-a", ["search.read"])
    clock.advance(11)

    assert coordinator.claim_task("worker-a") is None
    assert coordinator.observe_worker("worker-a").lifecycle is WorkerLifecycle.UNHEALTHY
    with pytest.raises(WorkerNotRegisteredError, match="not available"):
        coordinator.heartbeat("worker-a")


def test_draining_worker_receives_no_new_leases() -> None:
    coordinator, _ = setup_coordinator()
    coordinator.register_worker("worker-a", "impl-a", ["search.read"])
    coordinator.drain_worker("worker-a")
    coordinator.submit_task(
        Intent(goal="search"),
        execution_id="exec-1",
        task_id="task-1",
        required_capabilities=["search.read"],
    )

    assert coordinator.claim_task("worker-a") is None
    assert coordinator.observe_worker("worker-a").lifecycle is WorkerLifecycle.DRAINING


def test_two_workers_claim_distinct_tasks_concurrently() -> None:
    coordinator, _ = setup_coordinator()
    coordinator.register_worker("worker-a", "impl-a", ["cap.a"])
    coordinator.register_worker("worker-b", "impl-b", ["cap.b"])
    coordinator.submit_task(
        Intent(goal="task a"),
        execution_id="exec-a",
        task_id="task-a",
        required_capabilities=["cap.a"],
    )
    coordinator.submit_task(
        Intent(goal="task b"),
        execution_id="exec-b",
        task_id="task-b",
        required_capabilities=["cap.b"],
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(coordinator.claim_task, "worker-a"),
            pool.submit(coordinator.claim_task, "worker-b"),
        ]
        claims = [future.result() for future in futures]

    assert {claim.task_id for claim in claims if claim is not None} == {
        "task-a",
        "task-b",
    }
    assert len({claim.lease.lease_id for claim in claims if claim and claim.lease}) == 2
