"""Tests for the Orchestrator application-service boundary."""

from __future__ import annotations

import json
from threading import Thread
from urllib.request import Request, urlopen

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.execution_state import ExecutionState, FileExecutionStateStore
from loom_ai.intent import Intent
from loom_ai.orchestrator import Orchestrator
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

    result = orchestrator.submit(intent, "cancelled-execution")
    observed = orchestrator.observe("cancelled-execution")

    assert result.status is WorkerStatus.CANCELLED
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
    orchestrator.submit(intent, "continuing-execution")

    result = orchestrator.continue_execution("continuing-execution")
    observed = orchestrator.observe("continuing-execution")

    assert result.status is WorkerStatus.SUCCESS
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

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.intent = Intent(goal="observed execution")

    def submit(self, intent: Intent, execution_id: str) -> WorkerResult:
        self.calls.append(("submit", intent.goal, execution_id))
        return WorkerResult(worker_id="service", status=WorkerStatus.SUCCESS)

    def observe(self, execution_id: str) -> ExecutionState:
        self.calls.append(("observe", execution_id))
        return ExecutionState(
            execution_id=execution_id,
            intent=self.intent,
            status="success",
        )

    def continue_execution(self, execution_id: str) -> WorkerResult:
        self.calls.append(("continue", execution_id))
        return WorkerResult(worker_id="service", status=WorkerStatus.SUCCESS)


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
            data=json.dumps({"goal": "delegate this", "execution_id": "exec-1"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            submitted = json.load(response)
        assert submitted["execution_id"] == "exec-1"

        with urlopen(f"{base_url}/executions/exec-1") as response:
            observed = json.load(response)
        assert observed["execution_id"] == "exec-1"

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
            "continue",
        ]
        assert service.calls[0] == ("submit", "delegate this", "exec-1")
    finally:
        server.close()
        thread.join(timeout=2)
