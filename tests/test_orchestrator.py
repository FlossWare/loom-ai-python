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
    ExecutionAlreadyExistsError,
    ExecutionNotResumableError,
    Orchestrator,
    OrchestratorSubmission,
)
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerContext, WorkerOutcome, WorkerResult, WorkerStatus
from scripts.loom_mcp_server import LoomHTTPClient


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
            output={"answer": 42},
            evidence=({"source": "fixed-worker"},),
        )


def build_arbiter(worker: FixedWorker) -> Arbiter:
    def evaluate(result: WorkerResult, context: WorkerContext) -> WorkerEvaluation:
        if result.successful and context.state.get("phase") == "initial":
            return WorkerEvaluation(
                ArbiterDecision.INTERRUPT,
                reason="initial phase complete; awaiting continuation",
            )
        return WorkerEvaluation(ArbiterDecision.COMPLETE, reason=result.error)

    return Arbiter([worker], evaluate, max_retries=0)


def test_non_durable_execution_cannot_report_resumable_interrupt_as_success() -> None:
    worker = FixedWorker(WorkerStatus.SUCCESS)
    orchestrator = Orchestrator(build_arbiter(worker))

    submission = orchestrator.submit(Intent(goal="interrupt without durable storage"))

    assert submission.execution_id is None
    assert submission.result.status is WorkerStatus.FAILED
    assert (
        submission.result.metadata["termination_cause"] == "durable_state_unavailable"
    )
    assert submission.result.error == (
        "execution interrupted but durable state is not configured"
    )
    assert any(
        item.get("type") == "interruption-unavailable"
        for item in submission.result.evidence
    )


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


def test_duplicate_execution_id_is_rejected_without_replacing_prior_result(
    tmp_path,
) -> None:
    worker = FixedWorker(WorkerStatus.SUCCESS)
    store = FileExecutionStateStore(tmp_path)
    orchestrator = Orchestrator(build_arbiter(worker), execution_store=store)
    original_intent = Intent(goal="original accepted work")
    first = orchestrator.submit(original_intent, execution_id="stable-execution")

    with pytest.raises(
        ExecutionAlreadyExistsError,
        match="execution already exists: stable-execution",
    ):
        orchestrator.submit(
            Intent(goal="different work must not replace it"),
            execution_id="stable-execution",
        )

    observed = orchestrator.observe("stable-execution")
    assert first.result.status is WorkerStatus.FAILED
    assert len(worker.contexts) == 1
    assert observed.intent == original_intent
    assert observed.result is not None
    assert observed.result["output"][0]["output"] == {"answer": 42}


def test_orchestrator_continuation_preserves_context_and_appends_evidence(
    tmp_path,
) -> None:
    worker = FixedWorker(WorkerStatus.SUCCESS)
    store = FileExecutionStateStore(tmp_path)
    orchestrator = Orchestrator(build_arbiter(worker), execution_store=store)
    intent = Intent(goal="continue this execution", provenance={"source": "test"})
    orchestrator.submit(intent, execution_id="continuing-execution")
    initial = orchestrator.observe("continuing-execution")
    assert initial.lifecycle == "interrupted"
    assert not initial.terminal

    submission = orchestrator.continue_execution("continuing-execution")
    observed = orchestrator.observe("continuing-execution")

    assert submission.result.status is WorkerStatus.SUCCESS
    assert worker.contexts[1].intent == intent
    assert worker.contexts[1].evidence
    assert observed.status == "success"
    assert observed.lifecycle == "completed"
    assert observed.result is not None
    assert observed.result["worker_id"] == "arbiter"
    assert observed.result["output"][0]["worker_id"] == "fixed"
    assert observed.result["output"][0]["output"] == {"answer": 42}
    assert observed.result["status"] == "success"
    phases = [
        item["phase"]
        for item in observed.evidence
        if item.get("type") == "execution-phase"
    ]
    assert phases == ["initial", "continued"]


def test_terminal_execution_cannot_be_continued(tmp_path) -> None:
    worker = FixedWorker(WorkerStatus.SUCCESS)

    def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
        return WorkerEvaluation(ArbiterDecision.COMPLETE, reason=result.error)

    orchestrator = Orchestrator(
        Arbiter([worker], evaluate, max_retries=0),
        execution_store=FileExecutionStateStore(tmp_path),
    )
    orchestrator.submit(
        Intent(goal="complete once"),
        execution_id="terminal-execution",
    )
    before = orchestrator.observe("terminal-execution")

    assert before.terminal
    with pytest.raises(
        ExecutionNotResumableError,
        match="execution is not resumable: terminal-execution",
    ):
        orchestrator.continue_execution("terminal-execution")

    after = orchestrator.observe("terminal-execution")
    assert after == before
    assert len(worker.contexts) == 1


def test_http_terminal_continuation_returns_conflict_without_mutation(
    tmp_path,
) -> None:
    worker = FixedWorker(WorkerStatus.SUCCESS)

    def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
        return WorkerEvaluation(ArbiterDecision.COMPLETE, reason=result.error)

    server = LoomServer(
        Arbiter([worker], evaluate, max_retries=0),
        port=0,
        execution_store=FileExecutionStateStore(tmp_path),
    )
    instance = server.start()
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.host}:{server.port}"
    try:
        submit = Request(
            f"{base_url}/intents",
            data=json.dumps(
                {"goal": "finish once", "execution_id": "terminal-http-execution"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(submit) as response:
            first = json.load(response)
        assert first["status"] == "success"

        with urlopen(f"{base_url}/executions/terminal-http-execution") as response:
            before = json.load(response)
        assert before["lifecycle"] == "completed"

        continue_request = Request(
            f"{base_url}/executions/terminal-http-execution/continue",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as captured:
            urlopen(continue_request)
        assert captured.value.code == 409
        error = json.load(captured.value)
        assert error["execution_id"] == "terminal-http-execution"
        assert error["category"] == "execution.not_resumable"
        assert "not resumable" in error["error"]

        with urlopen(f"{base_url}/executions/terminal-http-execution") as response:
            after = json.load(response)
        assert after["lifecycle"] == "completed"
        assert after["result"] == before["result"]
        assert len(worker.contexts) == 1
    finally:
        server.close()
        thread.join(timeout=2)


def test_continuation_preserves_state_metadata_and_prior_structured_result(
    tmp_path,
) -> None:
    worker = FixedWorker(WorkerStatus.SUCCESS)
    store = FileExecutionStateStore(tmp_path)
    intent = Intent(goal="continue with saved workspace and result")
    prior_result = {
        "worker_id": "previous-worker",
        "status": "success",
        "output": {"artifact": "reviewed patch"},
        "evidence": [{"path": "src/example.py"}],
        "error": "",
        "metadata": {"stage": "solve"},
    }
    store.save(
        ExecutionState(
            execution_id="context-preservation",
            intent=intent,
            state={
                "phase": "initial",
                "workspace": "/workspace/repo",
                "repository_revision": "abc123",
            },
            evidence=({"source": "prior-stage"},),
            status="success",
            metadata={"caller": "test"},
            result=prior_result,
            lifecycle="interrupted",
        )
    )
    orchestrator = Orchestrator(build_arbiter(worker), execution_store=store)

    orchestrator.continue_execution("context-preservation")
    observed = orchestrator.observe("context-preservation")

    continued_context = worker.contexts[0]
    assert continued_context.state["workspace"] == "/workspace/repo"
    assert continued_context.state["repository_revision"] == "abc123"
    assert continued_context.state["last_execution_result"] == prior_result
    assert observed.state["workspace"] == "/workspace/repo"
    assert observed.state["repository_revision"] == "abc123"
    assert observed.metadata["caller"] == "test"
    assert observed.result is not None
    assert observed.result["worker_id"] == "arbiter"
    assert observed.result["output"][0]["worker_id"] == "fixed"
    assert observed.result["output"][0]["output"] == {"answer": 42}
    assert observed.result["status"] == "success"
    assert observed.evidence[0] == {"source": "prior-stage"}


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
        if execution_id not in {"exec-1", "exec/1"}:
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
            payload = json.load(exc)
            assert payload["error"] == "execution not found"
            assert payload["category"] == "execution.not_found"
        else:
            raise AssertionError("expected missing execution to return HTTP 404")

        cancelled = LoomHTTPClient(base_url).cancel(
            "exec-1",
            reason="test cancellation",
        )
        assert cancelled["cancellation_accepted"] is True
        assert cancelled["cancellation_pending"] is True
        assert cancelled["status"] == "running"

        encoded_observation = LoomHTTPClient(base_url).observe("exec/1")
        assert encoded_observation["execution_id"] == "exec/1"
        encoded_cancellation = LoomHTTPClient(base_url).cancel("exec/1")
        assert encoded_cancellation["execution_id"] == "exec/1"

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
            "observe",
            "cancel",
            "continue",
        ]
        assert service.calls[0] == ("submit", "delegate this", "exec-1")
    finally:
        server.close()
        thread.join(timeout=2)


def test_http_duplicate_execution_id_returns_conflict_without_rerunning(
    tmp_path,
) -> None:
    worker = FixedWorker(WorkerStatus.SUCCESS)
    server = LoomServer(
        build_arbiter(worker),
        port=0,
        execution_store=FileExecutionStateStore(tmp_path),
    )
    instance = server.start()
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    url = f"http://{server.host}:{server.port}/intents"
    payload = json.dumps(
        {"goal": "execute once", "execution_id": "http-stable-execution"}
    ).encode()
    try:
        request = Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            first = json.load(response)
        assert first["execution_id"] == "http-stable-execution"

        duplicate = Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as captured:
            urlopen(duplicate)
        assert captured.value.code == 409
        conflict = json.load(captured.value)
        assert conflict["execution_id"] == "http-stable-execution"
        assert conflict["category"] == "execution.identity_conflict"
        assert "execution already exists" in conflict["error"]
        assert len(worker.contexts) == 1
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
                artifacts=({"artifact_id": "cancel-artifact"},),
                observations=({"kind": "cancellation", "value": "pending"},),
                provenance={
                    "implementation_id": "blocking",
                    "event_ids": ["cancel-event"],
                },
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
        assert submissions[0].result.outcome is WorkerOutcome.CANCELLED
        assert submissions[0].result.artifacts == ({"artifact_id": "cancel-artifact"},)
        assert submissions[0].result.observations == (
            {"kind": "cancellation", "value": "pending"},
        )
        assert submissions[0].result.provenance["event_ids"] == ["cancel-event"]
        assert submissions[0].result.provenance["attributes"]["contributors"][0][
            "provenance"
        ]["event_ids"] == ["cancel-event"]

        state = orchestrator.observe("active-execution")
        assert state.status == "cancelled"
        assert state.metadata["cancellation"]["pending"] is False
        assert any(
            item.get("type") == "cancellation-requested" for item in state.evidence
        )
        assert any(
            item.get("type") == "cancellation-confirmed" for item in state.evidence
        )
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
    deadline = (datetime.now(timezone.utc) + timedelta(milliseconds=30)).isoformat()

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
