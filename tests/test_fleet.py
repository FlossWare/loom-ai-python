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
from loom_ai.fleet_state import FileFleetStateStore
from loom_ai.intent import Intent
from loom_ai.worker import WorkerContext, WorkerOutcome, WorkerResult, WorkerStatus


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
            evidence=(
                {"type": "worker.completed", "intent_id": context.intent.intent_id},
            ),
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
    coordinator.register_worker(
        "worker-a", "impl-a", ["repository.read"], lease_seconds=2
    )
    coordinator.register_worker(
        "worker-b", "impl-b", ["repository.read"], lease_seconds=5
    )
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
            retryable=True,
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


def test_result_worker_identity_must_match_lease_holder() -> None:
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

    with pytest.raises(FleetConflictError, match="identity does not match"):
        coordinator.submit_result(
            "task-1",
            worker_id="worker-a",
            lease_id=leased.lease.lease_id,
            generation=leased.lease.generation,
            result=WorkerResult(
                worker_id="worker-b", status=WorkerStatus.SUCCESS, output="spoofed"
            ),
        )


def test_task_deadline_rejects_late_result() -> None:
    coordinator, clock = setup_coordinator()
    coordinator.register_worker("worker-a", "impl-a", ["search.read"])
    coordinator.submit_task(
        Intent(goal="search"),
        execution_id="exec-1",
        task_id="task-1",
        required_capabilities=["search.read"],
        deadline=clock.current + timedelta(seconds=1),
    )
    leased = coordinator.claim_task("worker-a")
    assert leased is not None and leased.lease is not None
    clock.advance(2)

    with pytest.raises(StaleLeaseError, match="deadline"):
        coordinator.submit_result(
            "task-1",
            worker_id="worker-a",
            lease_id=leased.lease.lease_id,
            generation=leased.lease.generation,
            result=WorkerResult(
                worker_id="worker-a", status=WorkerStatus.SUCCESS, output="too late"
            ),
        )
    assert coordinator.observe_task("task-1").status is FleetTaskStatus.FAILED


def test_idempotency_key_deduplicates_submissions_without_explicit_task_id() -> None:
    coordinator, _ = setup_coordinator()
    intent = Intent(goal="index a document", intent_id="intent-idempotent")
    first = coordinator.submit_task(
        intent,
        execution_id="exec-idempotent",
        idempotency_key="request-123",
        required_capabilities=["index.write"],
    )
    duplicate = coordinator.submit_task(
        intent,
        execution_id="exec-idempotent",
        idempotency_key="request-123",
        required_capabilities=["index.write"],
    )

    assert duplicate == first
    assert len(coordinator._tasks) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"required_capabilities": ["admin.delete"]},
        {"max_attempts": 4},
        {"execution_id": "another-execution"},
        {"intent": Intent(goal="different goal", intent_id="intent-idempotent")},
    ],
)
def test_idempotency_key_rejects_changed_submission_payload(changes: dict) -> None:
    coordinator, _ = setup_coordinator()
    intent = Intent(goal="index a document", intent_id="intent-idempotent")
    coordinator.submit_task(
        intent,
        execution_id="exec-idempotent",
        idempotency_key="request-123",
        required_capabilities=["index.write"],
        max_attempts=3,
    )
    payload = {
        "intent": intent,
        "execution_id": "exec-idempotent",
        "idempotency_key": "request-123",
        "required_capabilities": ["index.write"],
        "max_attempts": 3,
    }
    payload.update(changes)

    with pytest.raises(FleetConflictError, match="idempotency key"):
        coordinator.submit_task(**payload)


def test_duplicate_task_id_rejects_changed_capabilities_or_retry_policy() -> None:
    coordinator, _ = setup_coordinator()
    intent = Intent(goal="run tests", intent_id="intent-duplicate")
    coordinator.submit_task(
        intent,
        execution_id="exec-duplicate",
        task_id="task-duplicate",
        idempotency_key="idem-duplicate",
        required_capabilities=["test.run"],
        max_attempts=2,
    )

    with pytest.raises(FleetConflictError, match="task identity"):
        coordinator.submit_task(
            intent,
            execution_id="exec-duplicate",
            task_id="task-duplicate",
            idempotency_key="idem-different",
            required_capabilities=["admin.run"],
            max_attempts=2,
        )


def test_cancel_running_task_fences_late_worker_result() -> None:
    coordinator = FleetCoordinator()
    coordinator.register_worker("worker-a", "impl-a", ["repository.read"])
    coordinator.submit_task(
        Intent(goal="read repository", intent_id="intent-cancel"),
        execution_id="execution-cancel",
        task_id="task-cancel",
        required_capabilities=["repository.read"],
    )
    leased = coordinator.claim_task("worker-a")
    assert leased is not None and leased.lease is not None
    lease = leased.lease

    cancelled = coordinator.cancel_task("task-cancel", reason="caller cancelled")

    assert cancelled.status is FleetTaskStatus.CANCELLED
    assert cancelled.lease is None
    assert cancelled.result is not None
    assert cancelled.result.status is WorkerStatus.CANCELLED
    assert cancelled.result.metadata["termination_cause"] == "cancelled"

    late = coordinator.submit_result(
        "task-cancel",
        worker_id="worker-a",
        lease_id=lease.lease_id,
        generation=lease.generation,
        result=WorkerResult(
            worker_id="worker-a",
            status=WorkerStatus.SUCCESS,
            output={"stale": True},
        ),
    )
    assert late.status is FleetTaskStatus.CANCELLED
    assert late.result is not None
    assert late.result.status is WorkerStatus.CANCELLED


def test_canonical_worker_result_fields_survive_fleet_coordinator_restart(
    tmp_path,
) -> None:
    store = FileFleetStateStore(tmp_path / "fleet-state")
    coordinator = FleetCoordinator(state_store=store)
    coordinator.register_worker("worker-a", "impl-a", ["search.read"])
    coordinator.submit_task(
        Intent(goal="preserve result contract", intent_id="intent-contract"),
        execution_id="execution-contract",
        task_id="task-contract",
        required_capabilities=["search.read"],
    )
    lease_task = coordinator.claim_task("worker-a")
    assert lease_task is not None and lease_task.lease is not None
    result = WorkerResult(
        worker_id="worker-a",
        status=WorkerStatus.FAILED,
        outcome=WorkerOutcome.FAILED,
        error="remote failure",
        error_code="remote_unavailable",
        retryable=True,
        artifacts=({"artifact_id": "artifact-1"},),
        observations=({"kind": "health", "value": "degraded"},),
        provenance={
            "implementation_id": "impl-a",
            "event_ids": ["event-1"],
            "attributes": {"region": "test"},
        },
    )
    completed = coordinator.submit_result(
        "task-contract",
        worker_id="worker-a",
        lease_id=lease_task.lease.lease_id,
        generation=lease_task.lease.generation,
        result=result,
    )
    assert completed.status is FleetTaskStatus.FAILED

    restored = FleetCoordinator(state_store=store)
    restored_task = restored.observe_task("task-contract")

    assert restored_task.result is not None
    assert restored_task.result.outcome is WorkerOutcome.FAILED
    assert restored_task.result.error_code == "remote_unavailable"
    assert restored_task.result.retryable is True
    assert restored_task.result.artifacts == ({"artifact_id": "artifact-1"},)
    assert restored_task.result.observations == (
        {"kind": "health", "value": "degraded"},
    )
    assert restored_task.result.provenance == {
        "implementation_id": "impl-a",
        "event_ids": ["event-1"],
        "attributes": {"region": "test"},
    }


@pytest.mark.parametrize(
    ("worker_status", "outcome", "expected_status"),
    [
        (WorkerStatus.FAILED, WorkerOutcome.POLICY_DENIED, FleetTaskStatus.FAILED),
        (WorkerStatus.FAILED, WorkerOutcome.DEADLINE_EXCEEDED, FleetTaskStatus.FAILED),
        (WorkerStatus.CANCELLED, WorkerOutcome.CANCELLED, FleetTaskStatus.CANCELLED),
    ],
)
def test_fleet_never_retries_canonical_terminal_outcomes(
    worker_status: WorkerStatus,
    outcome: WorkerOutcome,
    expected_status: FleetTaskStatus,
) -> None:
    coordinator, _ = setup_coordinator()
    coordinator.register_worker("worker-a", "impl-a", ["operation.run"])
    coordinator.submit_task(
        Intent(goal="terminal outcome", intent_id=f"intent-{outcome.value}"),
        execution_id=f"execution-{outcome.value}",
        task_id=f"task-{outcome.value}",
        required_capabilities=["operation.run"],
        max_attempts=3,
    )
    leased = coordinator.claim_task("worker-a")
    assert leased is not None and leased.lease is not None
    result = WorkerResult(
        worker_id="worker-a",
        status=worker_status,
        outcome=outcome,
        error="terminal result",
        retryable=True,
    )

    task = coordinator.submit_result(
        leased.task_id,
        worker_id="worker-a",
        lease_id=leased.lease.lease_id,
        generation=leased.lease.generation,
        result=result,
        retryable=True,
    )

    assert task.status is expected_status
    assert task.result == result
    assert task.lease is None
