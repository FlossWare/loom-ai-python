"""Tests for the Orchestrator application-service boundary."""

from __future__ import annotations

import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.execution_state import ExecutionState, FileExecutionStateStore
from loom_ai.intent import Intent
from loom_ai.orchestrator import Orchestrator, OrchestratorSubmission
from loom_ai.server import LoomServer
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
            output={"answer": 42},
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
    assert observed.result["worker_id"] == "fixed"
    assert observed.result["status"] == "success"
    assert observed.evidence[0] == {"source": "prior-stage"}


class RecordingOrchestrator:
    """Service double proving HTTP uses the application-service contract."""

    durable = True
    durable_enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.intent = Intent(goal="observed execution")

    def submit(
        self, intent: Intent, *, execution_id: str | None = None
    ) -> OrchestratorSubmission:
        self.calls.append(("submit", intent.goal, execution_id))
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
            result={
                "worker_id": "service",
                "status": "success",
                "output": {"answer": 42},
                "evidence": [],
                "error": "",
                "metadata": {},
            },
        )

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
                {"goal": "delegate this", "execution_id": "exec-1"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            submitted = json.load(response)
        assert submitted["execution_id"] == "exec-1"

        with urlopen(f"{base_url}/executions/exec-1") as response:
            observed = json.load(response)
        assert observed["execution_id"] == "exec-1"
        assert observed["result"]["output"] == {"answer": 42}

        try:
            urlopen(f"{base_url}/executions/missing")
        except HTTPError as exc:
            assert exc.code == 404
            assert json.load(exc)["error"] == "execution not found"
        else:
            raise AssertionError("expected missing execution to return HTTP 404")

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
    assert state.result is not None
    assert state.result["status"] == "failed"
    assert any(
        item.get("type") == "execution-exception"
        and item.get("exception_type") == "ValueError"
        for item in state.evidence
    )


def test_http_application_value_error_is_not_misreported_as_bad_request() -> None:
    class FailingOrchestrator(RecordingOrchestrator):
        def submit(
            self, intent: Intent, *, execution_id: str | None = None
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
