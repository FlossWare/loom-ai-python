"""Provider-neutral fleet scheduling, lease fencing, and result acceptance.

This module is the in-memory reference realization of the distributed Worker
contract. Durable and remote transports are separate adapters over these
semantics; this class alone does not qualify a multi-host deployment.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Mapping
from uuid import uuid4

from loom_ai.fleet_state import FleetStateError, FleetStateStore
from loom_ai.intent import Intent
from loom_ai.worker import Worker, WorkerContext, WorkerOutcome, WorkerResult, WorkerStatus


class FleetError(RuntimeError):
    """Base error for invalid fleet operations."""


class FleetConflictError(FleetError):
    """Raised when a stable task or Worker identity is reused inconsistently."""


class WorkerNotRegisteredError(FleetError):
    """Raised when an operation references an unknown or unavailable Worker."""


class StaleLeaseError(FleetError):
    """Raised when a Worker uses an expired or superseded lease."""


class WorkerLifecycle(str, Enum):
    """Observable lifecycle of a registered fleet Worker."""

    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"
    UNHEALTHY = "unhealthy"


class FleetTaskStatus(str, Enum):
    """Task state independent of any particular queue or transport."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_TASK_STATES = frozenset(
    {FleetTaskStatus.SUCCEEDED, FleetTaskStatus.FAILED, FleetTaskStatus.CANCELLED}
)


@dataclass(frozen=True)
class WorkerRegistration:
    """Stable identity and capability advertisement for one Worker."""

    worker_id: str
    implementation_id: str
    capabilities: tuple[str, ...]
    lifecycle: WorkerLifecycle
    last_heartbeat_at: datetime
    lease_seconds: int


@dataclass(frozen=True)
class TaskLease:
    """Finite lease protected by a monotonically increasing fencing generation."""

    task_id: str
    lease_id: str
    worker_id: str
    generation: int
    attempt: int
    expires_at: datetime


@dataclass(frozen=True)
class FleetTask:
    """Task state and correlated result/evidence maintained by the coordinator."""

    task_id: str
    execution_id: str
    idempotency_key: str
    intent: Intent
    required_capabilities: tuple[str, ...]
    max_attempts: int
    deadline: datetime | None
    submitted_at: datetime
    status: FleetTaskStatus = FleetTaskStatus.QUEUED
    attempt: int = 0
    lease_generation: int = 0
    lease: TaskLease | None = None
    result: WorkerResult | None = None
    attempt_history: tuple[Mapping[str, Any], ...] = ()


class FleetCoordinator:
    """Thread-safe reference coordinator for registration, leases, and results.

    The injected clock makes expiry and race tests deterministic. Task delivery
    is at least once; a committed terminal result is immutable and duplicate
    submissions return the recorded task rather than executing again.
    """

    def __init__(
        self,
        *,
        heartbeat_timeout_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
        state_store: FleetStateStore | None = None,
        coordinator_id: str = "default",
    ) -> None:
        if heartbeat_timeout_seconds < 1:
            raise ValueError("heartbeat_timeout_seconds must be positive")
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not coordinator_id.strip():
            raise ValueError("coordinator_id must not be empty")
        self._workers: dict[str, WorkerRegistration] = {}
        self._tasks: dict[str, FleetTask] = {}
        self._lock = RLock()
        self._state_store = state_store
        self._coordinator_id = coordinator_id
        self._persistence_error: Exception | None = None
        if state_store is not None:
            snapshot = state_store.load(coordinator_id)
            if snapshot is None:
                self._persist_locked()
            else:
                self._restore_snapshot(snapshot)
                self._persist_locked()

    def _persist_locked(self) -> None:
        """Persist state or fail-stop after an uncertain durability outcome."""
        if self._state_store is None:
            return
        if self._persistence_error is not None:
            raise FleetError(
                "coordinator stopped after a persistence failure"
            ) from self._persistence_error
        try:
            self._state_store.save(self._coordinator_id, self._snapshot_locked())
        except Exception as exc:
            self._persistence_error = exc
            raise FleetError(
                "fleet state persistence failed; coordinator is fail-stopped"
            ) from exc

    def _snapshot_locked(self) -> dict[str, Any]:
        def stamp(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        workers = [
            {
                "worker_id": worker.worker_id,
                "implementation_id": worker.implementation_id,
                "capabilities": list(worker.capabilities),
                "lifecycle": worker.lifecycle.value,
                "last_heartbeat_at": stamp(worker.last_heartbeat_at),
                "lease_seconds": worker.lease_seconds,
            }
            for worker in self._workers.values()
        ]
        tasks = []
        for task in self._tasks.values():
            intent_value = task.intent
            lease_value = task.lease
            result_value = task.result
            tasks.append(
                {
                    "task_id": task.task_id,
                    "execution_id": task.execution_id,
                    "idempotency_key": task.idempotency_key,
                    "intent": {
                        "goal": intent_value.goal,
                        "requirements": list(intent_value.requirements),
                        "constraints": list(intent_value.constraints),
                        "acceptance": list(intent_value.acceptance),
                        "title": intent_value.title,
                        "intent_id": intent_value.intent_id,
                        "provenance": dict(intent_value.provenance),
                    },
                    "required_capabilities": list(task.required_capabilities),
                    "max_attempts": task.max_attempts,
                    "deadline": stamp(task.deadline),
                    "submitted_at": stamp(task.submitted_at),
                    "status": task.status.value,
                    "attempt": task.attempt,
                    "lease_generation": task.lease_generation,
                    "lease": (
                        None
                        if lease_value is None
                        else {
                            "task_id": lease_value.task_id,
                            "lease_id": lease_value.lease_id,
                            "worker_id": lease_value.worker_id,
                            "generation": lease_value.generation,
                            "attempt": lease_value.attempt,
                            "expires_at": stamp(lease_value.expires_at),
                        }
                    ),
                    "result": (
                        None
                        if result_value is None
                        else {
                            "worker_id": result_value.worker_id,
                            "status": result_value.status.value,
                            "output": result_value.output,
                            "evidence": list(result_value.evidence),
                            "error": result_value.error,
                            "error_code": result_value.error_code,
                            "retryable": result_value.retryable,
                            "outcome": result_value.outcome.value,
                            "artifacts": list(result_value.artifacts),
                            "observations": list(result_value.observations),
                            "provenance": dict(result_value.provenance),
                            "metadata": dict(result_value.metadata),
                        }
                    ),
                    "attempt_history": list(task.attempt_history),
                }
            )
        return {"workers": workers, "tasks": tasks}

    def _restore_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Restore validated state while invalidating all pre-restart leases."""
        try:
            if not isinstance(snapshot.get("workers"), list) or not isinstance(
                snapshot.get("tasks"), list
            ):
                raise ValueError("snapshot requires worker and task lists")
            now = self._now()

            def parse_time(value: str | None) -> datetime | None:
                if value is None:
                    return None
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("timestamp has no timezone")
                return parsed.astimezone(timezone.utc)

            workers: dict[str, WorkerRegistration] = {}
            for raw in snapshot["workers"]:
                worker_id = raw["worker_id"]
                if worker_id in workers:
                    raise ValueError("duplicate worker identity")
                workers[worker_id] = WorkerRegistration(
                    worker_id=worker_id,
                    implementation_id=raw["implementation_id"],
                    capabilities=tuple(raw["capabilities"]),
                    lifecycle=WorkerLifecycle.UNHEALTHY,
                    last_heartbeat_at=now,
                    lease_seconds=int(raw["lease_seconds"]),
                )

            tasks: dict[str, FleetTask] = {}
            for raw in snapshot["tasks"]:
                task_id = raw["task_id"]
                if task_id in tasks:
                    raise ValueError("duplicate task identity")
                raw_intent = raw["intent"]
                intent_value = Intent(
                    goal=raw_intent["goal"],
                    requirements=tuple(raw_intent["requirements"]),
                    constraints=tuple(raw_intent["constraints"]),
                    acceptance=tuple(raw_intent["acceptance"]),
                    title=raw_intent["title"],
                    intent_id=raw_intent["intent_id"],
                    provenance=dict(raw_intent["provenance"]),
                )
                raw_result = raw.get("result")
                result_value = (
                    None
                    if raw_result is None
                    else WorkerResult(
                        worker_id=raw_result["worker_id"],
                        status=WorkerStatus(raw_result["status"]),
                        output=raw_result.get("output"),
                        evidence=tuple(raw_result.get("evidence", ())),
                        error=raw_result.get("error", ""),
                        error_code=raw_result.get("error_code", ""),
                        retryable=raw_result.get("retryable", False),
                        outcome=(
                            WorkerOutcome(raw_result["outcome"])
                            if raw_result.get("outcome") is not None
                            else None
                        ),
                        artifacts=tuple(raw_result.get("artifacts", ())),
                        observations=tuple(raw_result.get("observations", ())),
                        provenance=dict(raw_result.get("provenance", {})),
                        metadata=dict(raw_result.get("metadata", {})),
                    )
                )
                status = FleetTaskStatus(raw["status"])
                history = tuple(raw.get("attempt_history", ()))
                attempt = int(raw["attempt"])
                maximum = int(raw["max_attempts"])
                deadline = parse_time(raw.get("deadline"))
                if status is FleetTaskStatus.RUNNING:
                    old_lease = raw.get("lease")
                    if old_lease is None:
                        raise ValueError("running task lacks lease")
                    history += (
                        {
                            "attempt": attempt,
                            "worker_id": old_lease["worker_id"],
                            "generation": int(old_lease["generation"]),
                            "status": "coordinator_restart_lease_invalidated",
                        },
                    )
                    if deadline is not None and now >= deadline:
                        status = FleetTaskStatus.FAILED
                        result_value = WorkerResult(
                            worker_id="fleet-coordinator",
                            status=WorkerStatus.FAILED,
                            error="task deadline expired during coordinator restart",
                            evidence=(
                                {"type": "task-deadline-expired"},
                                {"type": "coordinator-restart-lease-invalidated"},
                            ),
                            metadata={"termination_cause": "deadline_exceeded"},
                        )
                    elif attempt < maximum:
                        status, result_value = FleetTaskStatus.QUEUED, None
                    else:
                        status = FleetTaskStatus.FAILED
                        result_value = WorkerResult(
                            worker_id="fleet-coordinator",
                            status=WorkerStatus.FAILED,
                            error="active lease invalidated after attempts exhausted",
                            evidence=(
                                {"type": "coordinator-restart-lease-invalidated"},
                            ),
                            metadata={"termination_cause": "attempts_exhausted"},
                        )
                elif (
                    status is FleetTaskStatus.QUEUED
                    and deadline is not None
                    and now >= deadline
                ):
                    status = FleetTaskStatus.FAILED
                    result_value = WorkerResult(
                        worker_id="fleet-coordinator",
                        status=WorkerStatus.FAILED,
                        error="task deadline expired while coordinator was stopped",
                        evidence=({"type": "task-deadline-expired"},),
                        metadata={"termination_cause": "deadline_exceeded"},
                    )
                tasks[task_id] = FleetTask(
                    task_id=task_id,
                    execution_id=raw["execution_id"],
                    idempotency_key=raw["idempotency_key"],
                    intent=intent_value,
                    required_capabilities=tuple(raw["required_capabilities"]),
                    max_attempts=maximum,
                    deadline=deadline,
                    submitted_at=parse_time(raw["submitted_at"]),
                    status=status,
                    attempt=attempt,
                    lease_generation=int(raw["lease_generation"]),
                    lease=None,
                    result=result_value,
                    attempt_history=history,
                )
            self._workers, self._tasks = workers, tasks
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise FleetStateError("persisted fleet snapshot is invalid") from exc

    def register_worker(
        self,
        worker_id: str,
        implementation_id: str,
        capabilities: tuple[str, ...] | list[str],
        *,
        lease_seconds: int = 15,
    ) -> WorkerRegistration:
        """Register or refresh a stable Worker identity."""
        if not worker_id.strip() or not implementation_id.strip():
            raise ValueError("worker_id and implementation_id must not be empty")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if any(not isinstance(item, str) for item in capabilities):
            raise ValueError("capabilities must be strings")
        normalized = tuple(sorted(set(capabilities)))
        if any(not item.strip() for item in normalized):
            raise ValueError("capabilities must not contain empty values")
        now = self._now()
        with self._lock:
            existing = self._workers.get(worker_id)
            if existing is not None and existing.implementation_id != implementation_id:
                raise FleetConflictError(
                    "worker_id is already registered to another implementation"
                )
            registration = WorkerRegistration(
                worker_id=worker_id,
                implementation_id=implementation_id,
                capabilities=normalized,
                lifecycle=WorkerLifecycle.READY,
                last_heartbeat_at=now,
                lease_seconds=lease_seconds,
            )
            self._workers[worker_id] = registration
            self._persist_locked()
            return registration

    def heartbeat(self, worker_id: str) -> WorkerRegistration:
        """Refresh liveness without reviving an expired task lease."""
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise WorkerNotRegisteredError("worker is not registered")
            if worker.lifecycle in {WorkerLifecycle.STOPPED, WorkerLifecycle.UNHEALTHY}:
                raise WorkerNotRegisteredError("worker is not available")
            if worker.lifecycle is WorkerLifecycle.DRAINING:
                updated = replace(worker, last_heartbeat_at=self._now())
                self._workers[worker_id] = updated
                self._persist_locked()
                return updated
            updated = replace(
                worker,
                last_heartbeat_at=self._now(),
                lifecycle=WorkerLifecycle.READY,
            )
            self._workers[worker_id] = updated
            self._persist_locked()
            return updated

    def drain_worker(self, worker_id: str) -> WorkerRegistration:
        """Stop new assignments while allowing active leases to finish."""
        return self._set_worker_lifecycle(worker_id, WorkerLifecycle.DRAINING)

    def stop_worker(self, worker_id: str) -> WorkerRegistration:
        """Mark a Worker stopped; active leases expire or are explicitly released."""
        return self._set_worker_lifecycle(worker_id, WorkerLifecycle.STOPPED)

    def submit_task(
        self,
        intent: Intent,
        *,
        execution_id: str,
        required_capabilities: tuple[str, ...] | list[str],
        task_id: str | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        deadline: datetime | None = None,
    ) -> FleetTask:
        """Create a task or return the existing task for an identical submission."""
        if not execution_id.strip():
            raise ValueError("execution_id must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if deadline is not None and (
            deadline.tzinfo is None or deadline.utcoffset() is None
        ):
            raise ValueError("deadline must include an explicit UTC offset")
        if task_id is not None and not task_id.strip():
            raise ValueError("task_id must not be empty")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        required = tuple(sorted(set(required_capabilities)))
        if any(not capability.strip() for capability in required):
            raise ValueError("required capabilities must not contain empty values")

        with self._lock:
            if idempotency_key is not None:
                prior = next(
                    (
                        item
                        for item in self._tasks.values()
                        if item.idempotency_key == idempotency_key
                    ),
                    None,
                )
                if prior is not None:
                    same_submission = (
                        prior.execution_id == execution_id
                        and prior.intent == intent
                        and prior.required_capabilities == required
                        and prior.max_attempts == max_attempts
                        and prior.deadline == deadline
                    )
                    if not same_submission or (
                        task_id is not None and task_id != prior.task_id
                    ):
                        raise FleetConflictError(
                            "idempotency key was reused for a different submission"
                        )
                    return prior

            actual_task_id = task_id or str(uuid4())
            actual_idempotency_key = idempotency_key or actual_task_id
            existing = self._tasks.get(actual_task_id)
            if existing is not None:
                if (
                    existing.idempotency_key == actual_idempotency_key
                    and existing.execution_id == execution_id
                    and existing.intent == intent
                    and existing.required_capabilities == required
                    and existing.max_attempts == max_attempts
                    and existing.deadline == deadline
                ):
                    return existing
                raise FleetConflictError("task identity already exists")

            task = FleetTask(
                task_id=actual_task_id,
                execution_id=execution_id,
                idempotency_key=actual_idempotency_key,
                intent=intent,
                required_capabilities=required,
                max_attempts=max_attempts,
                deadline=deadline,
                submitted_at=self._now(),
            )
            self._tasks[actual_task_id] = task
            self._persist_locked()
            return task

    def claim_task(self, worker_id: str) -> FleetTask | None:
        """Lease the oldest eligible queued task to a ready, live Worker."""
        with self._lock:
            self.expire_leases()
            worker = self._available_worker(worker_id)
            if worker is None:
                return None
            candidates = sorted(
                (
                    task
                    for task in self._tasks.values()
                    if task.status is FleetTaskStatus.QUEUED
                    and set(task.required_capabilities).issubset(worker.capabilities)
                ),
                key=lambda task: (task.submitted_at, task.task_id),
            )
            for task in candidates:
                if task.deadline is not None and self._now() >= task.deadline:
                    self._finish_deadline(task)
                    continue
                attempt = task.attempt + 1
                generation = task.lease_generation + 1
                lease = TaskLease(
                    task_id=task.task_id,
                    lease_id=str(uuid4()),
                    worker_id=worker_id,
                    generation=generation,
                    attempt=attempt,
                    expires_at=self._now() + timedelta(seconds=worker.lease_seconds),
                )
                leased = replace(
                    task,
                    status=FleetTaskStatus.RUNNING,
                    attempt=attempt,
                    lease_generation=generation,
                    lease=lease,
                )
                self._tasks[task.task_id] = leased
                self._persist_locked()
                return leased
            return None

    def renew_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_id: str,
        generation: int,
    ) -> TaskLease:
        """Renew only the current unexpired lease holder's lease."""
        with self._lock:
            task = self._get_task(task_id)
            lease = task.lease
            now = self._now()
            if self._deadline_expired(task, now):
                self._finish_deadline(task)
                raise StaleLeaseError("task deadline has expired")
            if (
                task.status is not FleetTaskStatus.RUNNING
                or lease is None
                or lease.worker_id != worker_id
                or lease.lease_id != lease_id
                or lease.generation != generation
                or now >= lease.expires_at
            ):
                self.expire_leases()
                raise StaleLeaseError("lease is expired or no longer current")
            worker = self._available_worker(worker_id)
            if worker is None:
                raise WorkerNotRegisteredError("worker is not available")
            self._workers[worker_id] = replace(worker, last_heartbeat_at=now)
            renewed = replace(
                lease,
                expires_at=now + timedelta(seconds=worker.lease_seconds),
            )
            self._tasks[task_id] = replace(task, lease=renewed)
            self._persist_locked()
            return renewed

    def submit_result(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_id: str,
        generation: int,
        result: WorkerResult,
        retryable: bool = False,
    ) -> FleetTask:
        """Commit a result from the current lease, or return an existing terminal result."""
        with self._lock:
            task = self._get_task(task_id)
            if result.worker_id != worker_id:
                raise FleetConflictError(
                    "result Worker identity does not match lease holder"
                )
            if task.status in _TERMINAL_TASK_STATES:
                return task
            lease = task.lease
            now = self._now()
            if self._deadline_expired(task, now):
                self._finish_deadline(task)
                raise StaleLeaseError("task deadline has expired")
            if (
                task.status is not FleetTaskStatus.RUNNING
                or lease is None
                or lease.worker_id != worker_id
                or lease.lease_id != lease_id
                or lease.generation != generation
                or now >= lease.expires_at
            ):
                self.expire_leases()
                raise StaleLeaseError("result submitted without the current lease")
            history = task.attempt_history + (
                {
                    "attempt": task.attempt,
                    "worker_id": worker_id,
                    "generation": generation,
                    "status": result.status.value,
                    "error": result.error,
                    "evidence": tuple(result.evidence),
                },
            )
            if result.status is WorkerStatus.SUCCESS:
                updated = replace(
                    task,
                    status=FleetTaskStatus.SUCCEEDED,
                    lease=None,
                    result=result,
                    attempt_history=history,
                )
            elif result.status is WorkerStatus.CANCELLED:
                updated = replace(
                    task,
                    status=FleetTaskStatus.CANCELLED,
                    lease=None,
                    result=result,
                    attempt_history=history,
                )
            elif (
                retryable
                and task.attempt < task.max_attempts
                and not self._deadline_expired(task, now)
            ):
                updated = replace(
                    task,
                    status=FleetTaskStatus.QUEUED,
                    lease=None,
                    result=None,
                    attempt_history=history,
                )
            else:
                updated = replace(
                    task,
                    status=FleetTaskStatus.FAILED,
                    lease=None,
                    result=result,
                    attempt_history=history,
                )
            self._tasks[task_id] = updated
            self._persist_locked()
            return updated

    def execute_claimed_task(self, task_id: str, worker: Worker) -> FleetTask:
        """Execute a leased task through the normal Worker contract."""
        with self._lock:
            task = self._get_task(task_id)
            lease = task.lease
            if task.status is not FleetTaskStatus.RUNNING or lease is None:
                raise StaleLeaseError("task has no active lease")
            if worker.worker_id != lease.worker_id:
                raise StaleLeaseError("Worker identity does not match lease")
            context = WorkerContext(
                intent=task.intent,
                evidence=task.attempt_history,
                deadline=task.deadline,
                metadata={
                    "task_id": task.task_id,
                    "execution_id": task.execution_id,
                    "attempt": task.attempt,
                    "lease_generation": lease.generation,
                },
            )
        try:
            result = worker.execute(context)
        except Exception as exc:
            result = WorkerResult(
                worker_id=worker.worker_id,
                status=WorkerStatus.FAILED,
                error="worker invocation failed",
                evidence=(
                    {
                        "type": "worker-exception",
                        "exception_type": type(exc).__name__,
                    },
                ),
                metadata={"retryable": True},
            )
        return self.submit_result(
            task_id,
            worker_id=lease.worker_id,
            lease_id=lease.lease_id,
            generation=lease.generation,
            result=result,
            retryable=result.metadata.get("retryable") is True,
        )

    def expire_leases(self) -> tuple[FleetTask, ...]:
        """Expire stale leases and requeue only when policy still permits retry."""
        now = self._now()
        expired: list[FleetTask] = []
        with self._lock:
            for task in tuple(self._tasks.values()):
                lease = task.lease
                if (
                    task.status is not FleetTaskStatus.RUNNING
                    or lease is None
                    or now < lease.expires_at
                ):
                    continue
                history = task.attempt_history + (
                    {
                        "attempt": task.attempt,
                        "worker_id": lease.worker_id,
                        "generation": lease.generation,
                        "status": "lease_expired",
                    },
                )
                if task.attempt < task.max_attempts and not self._deadline_expired(
                    task, now
                ):
                    updated = replace(
                        task,
                        status=FleetTaskStatus.QUEUED,
                        lease=None,
                        result=None,
                        attempt_history=history,
                    )
                else:
                    failure = WorkerResult(
                        worker_id="fleet-coordinator",
                        status=WorkerStatus.FAILED,
                        error="worker lease expired",
                        evidence=({"type": "worker-lease-expired"},),
                        metadata={"termination_cause": "lease_expired"},
                    )
                    updated = replace(
                        task,
                        status=FleetTaskStatus.FAILED,
                        lease=None,
                        result=failure,
                        attempt_history=history,
                    )
                self._tasks[task.task_id] = updated
                expired.append(updated)
            for worker_id, worker in tuple(self._workers.items()):
                if (
                    worker.lifecycle is WorkerLifecycle.READY
                    and now - worker.last_heartbeat_at
                    > timedelta(seconds=self.heartbeat_timeout_seconds)
                ):
                    self._workers[worker_id] = replace(
                        worker, lifecycle=WorkerLifecycle.UNHEALTHY
                    )
            # Persist lease expirations and heartbeat-based lifecycle changes
            # together, even when no task lease expired.
            self._persist_locked()
        return tuple(expired)

    def cancel_task(
        self,
        task_id: str,
        *,
        reason: str = "cancellation requested",
        termination_cause: str = "cancelled",
    ) -> FleetTask:
        """Terminate queued or running work and fence any later Worker result."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must not be empty")
        if not isinstance(termination_cause, str) or termination_cause not in {
            "cancelled",
            "deadline_exceeded",
            "worker_timeout",
        }:
            raise ValueError("unsupported task termination cause")
        with self._lock:
            task = self._get_task(task_id)
            if task.status in _TERMINAL_TASK_STATES:
                return task
            history = task.attempt_history
            if task.status is FleetTaskStatus.RUNNING and task.lease is not None:
                history += (
                    {
                        "attempt": task.attempt,
                        "worker_id": task.lease.worker_id,
                        "generation": task.lease.generation,
                        "status": "coordinator_terminated_lease",
                        "termination_cause": termination_cause,
                    },
                )
            cancelled = termination_cause == "cancelled"
            result = WorkerResult(
                worker_id="fleet-coordinator",
                status=WorkerStatus.CANCELLED if cancelled else WorkerStatus.FAILED,
                error=reason,
                evidence=(
                    {"type": "fleet-task-terminated", "cause": termination_cause},
                ),
                metadata={"termination_cause": termination_cause},
            )
            updated = replace(
                task,
                status=FleetTaskStatus.CANCELLED
                if cancelled
                else FleetTaskStatus.FAILED,
                lease=None,
                result=result,
                attempt_history=history,
            )
            self._tasks[task_id] = updated
            self._persist_locked()
            return updated

    def observe_task(self, task_id: str) -> FleetTask:
        """Return the current task snapshot by stable identity."""
        with self._lock:
            return self._get_task(task_id)

    def observe_worker(self, worker_id: str) -> WorkerRegistration:
        """Return the current registration snapshot."""
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise WorkerNotRegisteredError("worker is not registered")
            return worker

    def _available_worker(self, worker_id: str) -> WorkerRegistration | None:
        worker = self._workers.get(worker_id)
        if worker is None:
            raise WorkerNotRegisteredError("worker is not registered")
        if worker.lifecycle is not WorkerLifecycle.READY:
            return None
        if self._now() - worker.last_heartbeat_at > timedelta(
            seconds=self.heartbeat_timeout_seconds
        ):
            self._workers[worker_id] = replace(
                worker, lifecycle=WorkerLifecycle.UNHEALTHY
            )
            self._persist_locked()
            return None
        return worker

    def _set_worker_lifecycle(
        self, worker_id: str, lifecycle: WorkerLifecycle
    ) -> WorkerRegistration:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise WorkerNotRegisteredError("worker is not registered")
            updated = replace(
                worker, lifecycle=lifecycle, last_heartbeat_at=self._now()
            )
            self._workers[worker_id] = updated
            self._persist_locked()
            return updated

    def _get_task(self, task_id: str) -> FleetTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        return task

    def _deadline_expired(self, task: FleetTask, now: datetime) -> bool:
        return task.deadline is not None and now >= task.deadline.astimezone(
            timezone.utc
        )

    def _finish_deadline(self, task: FleetTask) -> None:
        result = WorkerResult(
            worker_id="fleet-coordinator",
            status=WorkerStatus.FAILED,
            error="task deadline expired before lease",
            evidence=({"type": "task-deadline-expired"},),
            metadata={"termination_cause": "deadline_exceeded"},
        )
        self._tasks[task.task_id] = replace(
            task, status=FleetTaskStatus.FAILED, lease=None, result=result
        )
        self._persist_locked()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("fleet clock must return timezone-aware datetimes")
        return now.astimezone(timezone.utc)
