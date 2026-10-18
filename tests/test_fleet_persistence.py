from __future__ import annotations

from datetime import datetime, timezone

import pytest

from loom_ai.fleet import (
    FleetCoordinator,
    FleetTaskStatus,
    StaleLeaseError,
    WorkerLifecycle,
)
from loom_ai.fleet_state import FileFleetStateStore
from loom_ai.intent import Intent
from loom_ai.worker import WorkerResult, WorkerStatus


def test_queued_task_survives_restart_and_worker_must_re_register(tmp_path) -> None:
    store = FileFleetStateStore(tmp_path)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    first = FleetCoordinator(clock=lambda: now, state_store=store, coordinator_id="test")
    first.register_worker("worker-a", "impl-a", ["repository.read"])
    task = first.submit_task(
        Intent(goal="read repository", intent_id="intent-restart"),
        execution_id="exec-restart",
        task_id="task-restart",
        required_capabilities=["repository.read"],
    )

    recovered = FleetCoordinator(
        clock=lambda: now, state_store=store, coordinator_id="test"
    )

    assert recovered.observe_task(task.task_id).status is FleetTaskStatus.QUEUED
    assert recovered.observe_task(task.task_id).intent.intent_id == "intent-restart"
    assert recovered.observe_worker("worker-a").lifecycle is WorkerLifecycle.UNHEALTHY


def test_restart_invalidates_old_lease_and_fences_stale_result(tmp_path) -> None:
    store = FileFleetStateStore(tmp_path)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    first = FleetCoordinator(clock=lambda: now, state_store=store, coordinator_id="test")
    first.register_worker("worker-a", "impl-a", ["repository.read"])
    first.submit_task(
        Intent(goal="read repository", intent_id="intent-active"),
        execution_id="exec-active",
        task_id="task-active",
        required_capabilities=["repository.read"],
        max_attempts=3,
    )
    old_task = first.claim_task("worker-a")
    assert old_task is not None and old_task.lease is not None
    old_lease = old_task.lease

    recovered = FleetCoordinator(
        clock=lambda: now, state_store=store, coordinator_id="test"
    )
    restored = recovered.observe_task("task-active")
    assert restored.status is FleetTaskStatus.QUEUED
    assert restored.lease is None
    assert restored.attempt_history[-1]["status"] == "coordinator_restart_lease_invalidated"

    recovered.register_worker("worker-a", "impl-a", ["repository.read"])
    new_task = recovered.claim_task("worker-a")
    assert new_task is not None and new_task.lease is not None
    assert new_task.lease.generation > old_lease.generation

    with pytest.raises(StaleLeaseError):
        recovered.submit_result(
            "task-active",
            worker_id="worker-a",
            lease_id=old_lease.lease_id,
            generation=old_lease.generation,
            result=WorkerResult(worker_id="worker-a", status=WorkerStatus.SUCCESS, output="stale"),
        )


def test_terminal_result_and_attempt_history_survive_restart(tmp_path) -> None:
    store = FileFleetStateStore(tmp_path)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    first = FleetCoordinator(clock=lambda: now, state_store=store, coordinator_id="test")
    first.register_worker("worker-a", "impl-a", ["repository.read"])
    first.submit_task(
        Intent(goal="read repository", intent_id="intent-terminal"),
        execution_id="exec-terminal",
        task_id="task-terminal",
        required_capabilities=["repository.read"],
    )
    leased = first.claim_task("worker-a")
    assert leased is not None and leased.lease is not None
    first.submit_result(
        "task-terminal",
        worker_id="worker-a",
        lease_id=leased.lease.lease_id,
        generation=leased.lease.generation,
        result=WorkerResult(
            worker_id="worker-a", status=WorkerStatus.SUCCESS, output={"documents": 3},
            evidence=({"type": "test"},), metadata={"source": "test"},
        ),
    )

    recovered = FleetCoordinator(
        clock=lambda: now, state_store=store, coordinator_id="test"
    )
    restored = recovered.observe_task("task-terminal")
    assert restored.status is FleetTaskStatus.SUCCEEDED
    assert restored.result is not None
    assert restored.result.output == {"documents": 3}
    assert restored.result.metadata == {"source": "test"}
    assert restored.attempt_history[0]["status"] == "success"
