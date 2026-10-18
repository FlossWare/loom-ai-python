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

from loom_ai.intent import Intent
from loom_ai.worker import Worker, WorkerContext, WorkerResult, WorkerStatus


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
    ) -> None:
        if heartbeat_timeout_seconds < 1:
            raise ValueError("heartbeat_timeout_seconds must be positive")
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._workers: dict[str, WorkerRegistration] = {}
        self._tasks: dict[str, FleetTask] = {}
        self._lock = RLock()

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
                return replace(worker, last_heartbeat_at=self._now())
            updated = replace(
                worker,
                last_heartbeat_at=self._now(),
                lifecycle=WorkerLifecycle.READY,
            )
            self._workers[worker_id] = updated
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
        task_id = task_id or str(uuid4())
        idempotency_key = idempotency_key or task_id
        if not task_id.strip() or not idempotency_key.strip():
            raise ValueError("task_id and idempotency_key must not be empty")
        required = tuple(sorted(set(required_capabilities)))
        if any(not capability.strip() for capability in required):
            raise ValueError("required capabilities must not contain empty values")
        with self._lock:
            existing = self._tasks.get(task_id)
            if existing is not None:
                if (
                    existing.idempotency_key == idempotency_key
                    and existing.execution_id == execution_id
                    and existing.intent.intent_id == intent.intent_id
                ):
                    return existing
                raise FleetConflictError("task identity already exists")
            task = FleetTask(
                task_id=task_id,
                execution_id=execution_id,
                idempotency_key=idempotency_key,
                intent=intent,
                required_capabilities=required,
                max_attempts=max_attempts,
                deadline=deadline,
                submitted_at=self._now(),
            )
            self._tasks[task_id] = task
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
            renewed = replace(
                lease,
                expires_at=now + timedelta(seconds=worker.lease_seconds),
            )
            self._tasks[task_id] = replace(task, lease=renewed)
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
            if task.status in _TERMINAL_TASK_STATES:
                return task
            lease = task.lease
            now = self._now()
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
                evidence=({"type": "worker-exception", "exception_type": type(exc).__name__},),
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
        return tuple(expired)

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
            return None
        return worker

    def _set_worker_lifecycle(
        self, worker_id: str, lifecycle: WorkerLifecycle
    ) -> WorkerRegistration:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise WorkerNotRegisteredError("worker is not registered")
            updated = replace(worker, lifecycle=lifecycle, last_heartbeat_at=self._now())
            self._workers[worker_id] = updated
            return updated

    def _get_task(self, task_id: str) -> FleetTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        return task

    def _deadline_expired(self, task: FleetTask, now: datetime) -> bool:
        return task.deadline is not None and now >= task.deadline.astimezone(timezone.utc)

    def _finish_deadline(self, task: FleetTask) -> None:
        result = WorkerResult(
            worker_id="fleet-coordinator",
            status=WorkerStatus.FAILED,
            error="task deadline expired before lease",
            evidence=({"type": "task-deadline-expired"},),
            metadata={"termination_cause": "deadline_exceeded"},
        )
        self._tasks[task.task_id] = replace(
            task, status=FleetTaskStatus.FAILED, result=result
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("fleet clock must return timezone-aware datetimes")
        return now.astimezone(timezone.utc)
