"""Tests for the Orchestrator application-service boundary."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.execution_state import ExecutionState, FileExecutionStateStore
from loom_ai.intent import Intent
from loom_ai.orchestrator import (
    CancellationResult,
    Orchestrator,
    OrchestratorSubmission,
)
from loom_ai.server import LoomServer
from scripts.loom_mcp_server import LoomHTTPClient
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class FixedWorker:
    worker_id = "fixed"

    def __init__(self, status: WorkerStatus) -> None:
        self.status = status
        self.contexts: list[WorkerContext] = []

    def execute(self, context: WorkerContext) -> WorkerResult:
        self.contexts.append(context)
        return WorkerResult(
            worker_id=self.worker_id,
            status=self.status,
            evidence=({"source": "fixed-worker"},),
        )


def build_arbiter(worker: FixedWorker) -> Arbiter:
    def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
        return WorkerEvaluation(ArbiterDecision.COMPLETE, reason=result.error)

    return Arbiter([worker], evaluate, max_retries=0)


def test_orchestrator_persists_cancellation_as_distinct_terminal_status(
    tmp_path,
) -> None:
    worker = FixedWorker(WorkerStatus.CANCELLED)
    store = FileExecutionStateStore(tmp_path)
    orchestrator = Orchestrator(build_arbiter(worker), execution_store=store)
    intent = Intent(goal="cancel this execution")

    submission = orchestrator.submit(intent, execution_id="cancelled-execution")
    observed = orchestrator.observe("cancelled-execution")

    assert submission.result.status is WorkerStatus.CANCELLED
    assert observed.status == "cancelled"
    assert observed.state["last_status"] == "cancelled"
    assert observed.terminal
    assert {"source": "fixed-worker"} in observed.evidence


def test_orchestrator_continuation_preserves_context_and_appends_evidence(
    tmp_path,
) -> None:
    worker = FixedWorker(WorkerStatus.SUCCESS)
    store = FileExecutionStateStore(tmp_path)
    orchestrator = Orchestrator(build_arbiter(worker), execution_store=store)
    intent = Intent(goal="continue this execution", provenance={"source": "test"})
    orchestrator.submit(intent, execution_id="continuing-execution")

    submission = orchestrator.continue_execution("continuing-execution")
    observed = orchestrator.observe("continuing-execution")

    assert submission.result.status is WorkerStatus.SUCCESS
    assert worker.contexts[1].intent == intent
    assert worker.contexts[1].evidence
    assert observed.status == "success"
    phases = [
        item["phase"]
        for item in observed.evidence
        if item.get("type") == "execution-phase"
    ]
    assert phases == ["initial", "continued"]


class RecordingOrchestrator:
    """Service double proving HTTP uses the application-service contract."""

    durable = True
    durable_enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.deadlines: list[datetime | None] = []
        self.intent = Intent(goal="observed execution")

    def submit(
        self,
        intent: Intent,
        *,
        execution_id: str | None = None,
        deadline: datetime | None = None,
    ) -> OrchestratorSubmission:
        self.calls.append(("submit", intent.goal, execution_id))
        self.deadlines.append(deadline)
        return OrchestratorSubmission(
            execution_id,
            WorkerResult(worker_id="service", status=WorkerStatus.SUCCESS),
        )

    def observe(self, execution_id: str) -> ExecutionState:
        self.calls.append(("observe", execution_id))
        if execution_id != "exec-1":
            raise KeyError(f"execution not found: {execution_id}")
        return ExecutionState(
            execution_id=execution_id,
            intent=self.intent,
            status="success",
        )

    def cancel(
        self, execution_id: str, *, reason: str = "cancellation requested"
    ) -> CancellationResult:
        self.calls.append(("cancel", execution_id))
        execution = ExecutionState(
            execution_id=execution_id,
            intent=self.intent,
            state={"phase": "initial", "cancellation_requested": True},
            status="running",
            metadata={"cancellation": {"requested": True, "reason": reason}},
        )
        return CancellationResult(execution, accepted=True, pending=True)

    def continue_execution(self, execution_id: str) -> OrchestratorSubmission:
        self.calls.append(("continue", execution_id))
        return OrchestratorSubmission(
            execution_id,
            WorkerResult(worker_id="service", status=WorkerStatus.SUCCESS),
        )


def test_http_routes_delegate_lifecycle_operations_to_orchestrator() -> None:
    service = RecordingOrchestrator()
    server = LoomServer(orchestrator=service, port=0)
    instance = server.start()
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.host}:{server.port}"

    try:
        request = Request(
            f"{base_url}/intents",
            data=json.dumps(
                {
                    "goal": "delegate this",
                    "execution_id": "exec-1",
                    "deadline": (
                        datetime.now(timezone.utc) + timedelta(seconds=60)
                    ).isoformat(),
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            submitted = json.load(response)
        assert submitted["execution_id"] == "exec-1"
        assert service.deadlines[0] is not None
        assert service.deadlines[0].tzinfo is not None

        with urlopen(f"{base_url}/executions/exec-1") as response:
            observed = json.load(response)
        assert observed["execution_id"] == "exec-1"

        try:
            urlopen(f"{base_url}/executions/missing")
        except HTTPError as exc:
            assert exc.code == 404
            assert json.load(exc)["error"] == "execution not found"
        else:
            raise AssertionError("expected missing execution to return HTTP 404")

        cancelled = LoomHTTPClient(base_url).cancel(
            "exec-1",
            reason="test cancellation",
        )
        assert cancelled["cancellation_accepted"] is True
        assert cancelled["cancellation_pending"] is True
        assert cancelled["status"] == "running"

        request = Request(
            f"{base_url}/executions/exec-1/continue",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            continued = json.load(response)
        assert continued["execution_id"] == "exec-1"

        assert [call[0] for call in service.calls] == [
            "submit",
            "observe",
            "observe",
            "cancel",
            "continue",
        ]
        assert service.calls[0] == ("submit", "delegate this", "exec-1")
    finally:
        server.close()
        thread.join(timeout=2)


class RaisingWorker:
    worker_id = "raising-worker"

    def execute(self, _context: WorkerContext) -> WorkerResult:
        raise ValueError("private implementation detail")


def test_unexpected_worker_exception_is_persisted_as_failure(tmp_path) -> None:
    def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
        return WorkerEvaluation(ArbiterDecision.COMPLETE, reason=result.error)

    orchestrator = Orchestrator(
        Arbiter([RaisingWorker()], evaluate, max_retries=0),
        execution_store=FileExecutionStateStore(tmp_path),
    )
    with pytest.raises(ValueError, match="private implementation detail"):
        orchestrator.submit(
            Intent(goal="persist execution failure"),
            execution_id="execution-failed",
        )

    state = orchestrator.observe("execution-failed")
    assert state.status == "failed"
    assert state.metadata["worker_id"] == "orchestrator"
    assert any(
        item.get("type") == "execution-exception"
        and item.get("exception_type") == "ValueError"
        for item in state.evidence
    )


def test_http_application_value_error_is_not_misreported_as_bad_request() -> None:
    class FailingOrchestrator(RecordingOrchestrator):
        def submit(
            self,
            intent: Intent,
            *,
            execution_id: str | None = None,
            deadline: datetime | None = None,
        ) -> OrchestratorSubmission:
            raise ValueError("failure inside the application service")

    server = LoomServer(orchestrator=FailingOrchestrator(), port=0)
    instance = server.start()
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://{server.host}:{server.port}/intents",
            data=json.dumps({"goal": "valid request"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as captured:
            urlopen(request)
        assert captured.value.code == 500
        payload = json.load(captured.value)
        assert "error" in payload
        assert payload["execution_id"]
    finally:
        server.close()
        thread.join(timeout=2)


def test_expired_deadline_is_persisted_without_starting_a_worker(tmp_path) -> None:
    worker = FixedWorker(WorkerStatus.SUCCESS)
    store = FileExecutionStateStore(tmp_path)
    orchestrator = Orchestrator(build_arbiter(worker), execution_store=store)
    deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    submission = orchestrator.submit(
        Intent(goal="must not start"),
        execution_id="expired-execution",
        deadline=deadline,
    )
    state = orchestrator.observe("expired-execution")

    assert worker.contexts == []
    assert submission.result.status is WorkerStatus.FAILED
    assert submission.result.metadata["termination_cause"] == "deadline_exceeded"
    assert state.status == "failed"
    assert state.metadata["termination_cause"] == "deadline_exceeded"
    assert state.metadata["deadline"] == deadline


def test_active_cancellation_is_pending_until_worker_returns(tmp_path) -> None:
    started = Event()
    release = Event()

    class BlockingWorker:
        worker_id = "blocking"

        def execute(self, _context: WorkerContext) -> WorkerResult:
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test worker was not released")
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.SUCCESS,
            )

    orchestrator = Orchestrator(
        build_arbiter(BlockingWorker()),
        execution_store=FileExecutionStateStore(tmp_path),
    )
    submissions: list[OrchestratorSubmission] = []
    failures: list[Exception] = []

    def submit() -> None:
        try:
            submissions.append(
                orchestrator.submit(
                    Intent(goal="cancel active execution"),
                    execution_id="active-execution",
                )
            )
        except Exception as exc:
            failures.append(exc)

    thread = Thread(target=submit, daemon=True)
    thread.start()
    try:
        assert started.wait(timeout=2)
        request = orchestrator.cancel(
            "active-execution",
            reason="operator requested cancellation",
        )
        assert request.accepted
        assert request.pending
        assert request.execution.status == "running"
        assert request.execution.metadata["cancellation"]["requested"]

        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert failures == []
        assert submissions[0].result.status is WorkerStatus.CANCELLED

        state = orchestrator.observe("active-execution")
        assert state.status == "cancelled"
        assert state.metadata["cancellation"]["pending"] is False
        assert any(item.get("type") == "cancellation-requested" for item in state.evidence)
        assert any(item.get("type") == "cancellation-confirmed" for item in state.evidence)
    finally:
        release.set()
        thread.join(timeout=3)


def test_deadline_expiring_during_worker_prevents_success(tmp_path) -> None:
    class SlowWorker:
        worker_id = "slow"

        def execute(self, _context: WorkerContext) -> WorkerResult:
            Event().wait(timeout=0.15)
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.SUCCESS,
            )

    orchestrator = Orchestrator(
        build_arbiter(SlowWorker()),
        execution_store=FileExecutionStateStore(tmp_path),
    )
    deadline = (
        datetime.now(timezone.utc) + timedelta(milliseconds=30)
    ).isoformat()

    submission = orchestrator.submit(
        Intent(goal="deadline expires while executing"),
        execution_id="late-execution",
        deadline=deadline,
    )
    state = orchestrator.observe("late-execution")

    assert submission.result.status is WorkerStatus.FAILED
    assert submission.result.metadata["termination_cause"] == "deadline_exceeded"
    assert state.status == "failed"
    assert state.metadata["termination_cause"] == "deadline_exceeded"
