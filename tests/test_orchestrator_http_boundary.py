"""Contract tests proving HTTP delegates lifecycle work to Orchestrator."""

from __future__ import annotations

import json
import socketserver
from threading import Thread
from urllib.request import Request, urlopen

from loom_ai.execution_state import ExecutionState
from loom_ai.intent import Intent
from loom_ai.orchestrator import OrchestratorSubmission
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerResult, WorkerStatus


class _HTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class RecordingOrchestrator:
    durable_enabled = True
    execution_store = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def submit(
        self, intent: Intent, *, execution_id: str | None = None
    ) -> OrchestratorSubmission:
        self.calls.append(("submit", intent.goal))
        return OrchestratorSubmission(
            execution_id or "orchestrator-created-id",
            WorkerResult(
                worker_id="application-worker",
                status=WorkerStatus.SUCCESS,
                output={"delegated": True},
            ),
        )

    def observe(self, execution_id: str) -> ExecutionState:
        self.calls.append(("observe", execution_id))
        return ExecutionState(
            execution_id=execution_id,
            intent=Intent(goal="observed task"),
            state={"phase": "initial"},
            evidence=({"type": "execution-phase", "phase": "initial"},),
            status="success",
        )

    def continue_execution(self, execution_id: str) -> OrchestratorSubmission:
        self.calls.append(("continue", execution_id))
        return OrchestratorSubmission(
            execution_id,
            WorkerResult(
                worker_id="application-worker",
                status=WorkerStatus.SUCCESS,
                output={"continued": True},
            ),
        )


def test_http_routes_delegate_submit_observe_and_continue_to_orchestrator() -> None:
    orchestrator = RecordingOrchestrator()
    server = LoomServer(orchestrator=orchestrator, port=0)
    instance = _HTTPServer((server.host, server.port), server._handler_factory())
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{server.host}:{instance.server_address[1]}"
        request = Request(
            f"{base}/intents",
            data=json.dumps({"goal": "use the application boundary"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            submitted = json.load(response)

        execution_id = submitted["execution_id"]
        assert execution_id == "orchestrator-created-id"
        assert submitted["output"] == {"delegated": True}

        with urlopen(f"{base}/executions/{execution_id}") as response:
            observed = json.load(response)
        assert observed["execution_id"] == execution_id
        assert observed["state"]["phase"] == "initial"

        continue_request = Request(
            f"{base}/executions/{execution_id}/continue",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(continue_request) as response:
            continued = json.load(response)
        assert continued["execution_id"] == execution_id
        assert continued["output"] == {"continued": True}

        assert orchestrator.calls == [
            ("submit", "use the application boundary"),
            ("observe", execution_id),
            ("continue", execution_id),
        ]
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def test_http_exposes_cancelled_execution_status(tmp_path) -> None:
    from tempfile import TemporaryDirectory

    from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
    from loom_ai.execution_state import FileExecutionStateStore
    from loom_ai.worker import WorkerContext, WorkerStatus

    class CancelledWorker:
        worker_id = "cancelled-worker"

        def execute(self, _context: WorkerContext) -> WorkerResult:
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.CANCELLED,
                error="cancelled by caller",
            )

    def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
        return WorkerEvaluation(ArbiterDecision.COMPLETE, reason=result.error)

    with TemporaryDirectory(dir=tmp_path) as state_dir:
        server = LoomServer(
            Arbiter([CancelledWorker()], evaluate),
            execution_store=FileExecutionStateStore(state_dir),
            port=0,
        )
        instance = _HTTPServer((server.host, server.port), server._handler_factory())
        thread = Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://{server.host}:{instance.server_address[1]}"
            request = Request(
                f"{base}/intents",
                data=json.dumps({"goal": "cancel task"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                submitted = json.load(response)

            assert submitted["status"] == "cancelled"
            execution_id = submitted["execution_id"]
            with urlopen(f"{base}/executions/{execution_id}") as response:
                state = json.load(response)
            assert state["status"] == "cancelled"
            assert state["state"]["last_status"] == "cancelled"
        finally:
            instance.shutdown()
            instance.server_close()
            thread.join(timeout=2)


def test_http_reports_orchestrator_value_error_as_server_failure() -> None:
    from urllib.error import HTTPError

    class FailingOrchestrator(RecordingOrchestrator):
        def submit(
            self, intent: Intent, *, execution_id: str | None = None
        ) -> OrchestratorSubmission:
            raise ValueError("execution failed inside the application service")

    server = LoomServer(orchestrator=FailingOrchestrator(), port=0)
    instance = _HTTPServer((server.host, server.port), server._handler_factory())
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://{server.host}:{instance.server_address[1]}/intents",
            data=json.dumps({"goal": "valid request"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request)
        except HTTPError as exc:
            assert exc.code == 500
            assert "error" in json.load(exc)
        else:
            raise AssertionError("expected HTTP 500 for application failure")
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)
