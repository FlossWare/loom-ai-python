"""Tests for the Loom HTTP transport boundary."""

from __future__ import annotations

import json
import socketserver
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.execution_state import FileExecutionStateStore
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerContext, WorkerOutcome, WorkerResult, WorkerStatus


class _TestServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class RecordingWorker:
    worker_id = "recording"

    def execute(self, context: WorkerContext) -> WorkerResult:
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={
                "goal": context.intent.goal,
                "provenance": context.intent.provenance,
            },
            evidence=({"source": self.worker_id},),
            outcome=WorkerOutcome.SUCCEEDED,
            artifacts=({"artifact_id": "http-artifact"},),
            observations=({"kind": "http", "value": "observed"},),
            provenance={
                "implementation_id": "recording",
                "event_ids": ["http-event"],
                "attributes": {"source": "server-test"},
            },
        )


def build_server() -> LoomServer:
    def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
        return WorkerEvaluation(ArbiterDecision.COMPLETE, reason=result.error)

    return LoomServer(Arbiter([RecordingWorker()], evaluate), port=0)


def test_server_executes_intent_over_http() -> None:
    server = build_server()
    instance = _TestServer((server.host, server.port), server._handler_factory())
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://{server.host}:{instance.server_address[1]}"
        with urlopen(f"{base_url}/health") as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "application/json"
            assert json.load(response) == {"status": "ok"}

        request = Request(
            f"{base_url}/intents",
            data=json.dumps(
                {
                    "title": "Test",
                    "goal": "exercise Loom",
                    "provenance": {"source": "stage3-test"},
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            assert response.headers.get_content_type() == "application/json"
            payload = json.load(response)

        assert payload["status"] == "success"
        assert payload["outcome"] == "succeeded"
        assert payload["result"]["outcome"] == "succeeded"
        assert payload["result"]["error_code"] == ""
        assert payload["result"]["retryable"] is False
        assert payload["result"]["artifacts"] == [{"artifact_id": "http-artifact"}]
        assert payload["result"]["observations"] == [
            {"kind": "http", "value": "observed"}
        ]
        assert payload["result"]["provenance"]["event_ids"] == ["http-event"]
        assert payload["output"][0]["output"] == {
            "goal": "exercise Loom",
            "provenance": {"source": "stage3-test"},
        }
        assert payload["output"][0]["worker_id"] == "recording"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def test_server_rejects_intent_without_goal() -> None:
    server = build_server()
    instance = _TestServer((server.host, server.port), server._handler_factory())
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://{server.host}:{instance.server_address[1]}/intents",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request)
        except HTTPError as exc:
            assert exc.code == 400
            assert exc.headers.get_content_type() == "application/json"
            payload = json.load(exc)
            assert payload["error"]
            assert payload["category"] == "request.invalid"
        else:
            raise AssertionError("expected HTTP 400")
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def test_server_rejects_non_string_provenance() -> None:
    server = build_server()
    instance = _TestServer((server.host, server.port), server._handler_factory())
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://{server.host}:{instance.server_address[1]}/intents",
            data=json.dumps(
                {"goal": "exercise Loom", "provenance": {"attempt": 1}}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request)
        except HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("expected HTTP 400")
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def test_server_rejects_overlarge_payload() -> None:
    server = build_server()
    instance = _TestServer((server.host, server.port), server._handler_factory())
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        data = b"x" * (10 * 1024 * 1024 + 1024)
        request = Request(
            f"http://{server.host}:{instance.server_address[1]}/intents",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request)
        except HTTPError as exc:
            assert exc.code == 413
            assert exc.headers.get_content_type() == "application/json"
            payload = json.load(exc)
            assert payload["error"]
            assert payload["category"] == "request.payload_too_large"
        except URLError:
            # Server closed connection early upon detecting over-sized header/payload
            pass
        else:
            raise AssertionError("expected request rejection for overlarge payload")
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def test_server_persists_and_continues_execution_state() -> None:
    with TemporaryDirectory() as state_dir:

        def evaluate(result: WorkerResult, context: WorkerContext) -> WorkerEvaluation:
            if result.successful and context.state.get("phase") == "initial":
                return WorkerEvaluation(
                    ArbiterDecision.INTERRUPT,
                    reason="initial phase awaits continuation",
                )
            return WorkerEvaluation(ArbiterDecision.COMPLETE, reason=result.error)

        server = LoomServer(
            Arbiter([RecordingWorker()], evaluate),
            port=0,
            execution_store=FileExecutionStateStore(state_dir),
        )
        instance = _TestServer((server.host, server.port), server._handler_factory())
        thread = Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://{server.host}:{instance.server_address[1]}"
            intent = {
                "title": "Persistent test",
                "goal": "exercise durable execution",
                "provenance": {"source": "persistence-test"},
            }
            request = Request(
                f"{base_url}/intents",
                data=json.dumps(intent).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                initial = json.load(response)

            execution_id = initial["execution_id"]
            assert initial["status"] == "failed"
            assert initial["lifecycle"] == "interrupted"
            assert initial["outcome"] == "failed"
            assert initial["result"]["outcome"] == "failed"

            with urlopen(f"{base_url}/executions/{execution_id}") as response:
                persisted = json.load(response)

            assert persisted["execution_id"] == execution_id
            assert persisted["intent_id"]
            assert persisted["status"] == "failed"
            assert persisted["lifecycle"] == "interrupted"
            assert persisted["outcome"] == "failed"
            assert persisted["result"]["outcome"] == "failed"
            assert persisted["result"]["error_code"] == "execution_interrupted"
            assert persisted["result"]["artifacts"] == [
                {"artifact_id": "http-artifact"}
            ]
            assert persisted["result"]["provenance"]["event_ids"] == ["http-event"]
            assert any(
                item.get("phase") == "initial"
                for item in persisted["evidence"]
                if item.get("type") == "execution-phase"
            )

            continue_request = Request(
                f"{base_url}/executions/{execution_id}/continue",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(continue_request) as response:
                continued = json.load(response)

            assert continued["execution_id"] == execution_id
            assert continued["status"] == "success"
            assert continued["outcome"] == "succeeded"
            assert continued["result"]["outcome"] == "succeeded"

            with urlopen(f"{base_url}/executions/{execution_id}") as response:
                final = json.load(response)

            phases = [
                item["phase"]
                for item in final["evidence"]
                if item.get("type") == "execution-phase"
            ]
            assert phases == ["initial", "continued"]
        finally:
            instance.shutdown()
            instance.server_close()
            thread.join(timeout=2)


def test_server_cli_can_enable_durable_execution_state(monkeypatch, tmp_path) -> None:
    from loom_ai import server as server_module

    captured = {}

    def stop_after_configuration(server) -> None:
        captured["durable"] = server.orchestrator.durable_enabled
        captured["root"] = server.execution_store.root

    monkeypatch.setattr(
        server_module.LoomServer, "serve_forever", stop_after_configuration
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["loom-server", "--state-dir", str(tmp_path)],
    )

    server_module.main()

    assert captured["durable"] is True
    assert captured["root"] == tmp_path


def test_server_cli_keeps_durability_opt_in(monkeypatch) -> None:
    from loom_ai import server as server_module

    captured = {}

    def stop_after_configuration(server) -> None:
        captured["durable"] = server.orchestrator.durable_enabled

    monkeypatch.setattr(
        server_module.LoomServer, "serve_forever", stop_after_configuration
    )
    monkeypatch.setattr(sys, "argv", ["loom-server"])

    server_module.main()

    assert captured["durable"] is False


def test_server_cli_can_enable_postgres_execution_state(monkeypatch) -> None:
    from loom_ai import server as server_module

    class FakePostgresStore:
        def __init__(self):
            self.initialized = False
            self.closed = False

        def initialize(self):
            self.initialized = True

        def close(self):
            self.closed = True

    store = FakePostgresStore()
    captured = {}

    def connect(dsn, *, schema):
        captured["dsn"] = dsn
        captured["schema"] = schema
        return store

    def stop_after_configuration(server) -> None:
        captured["durable"] = server.orchestrator.durable_enabled
        captured["store"] = server.execution_store

    monkeypatch.setattr(server_module.PostgresExecutionStateStore, "connect", connect)
    monkeypatch.setattr(
        server_module.LoomServer, "serve_forever", stop_after_configuration
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "loom-server",
            "--postgres-dsn",
            "postgresql://test",
            "--postgres-schema",
            "test_runtime",
        ],
    )

    server_module.main()

    assert captured == {
        "dsn": "postgresql://test",
        "schema": "test_runtime",
        "durable": True,
        "store": store,
    }
    assert store.initialized is True
    assert store.closed is True


def test_server_cli_uses_postgres_environment_when_no_state_dir(monkeypatch) -> None:
    from loom_ai import server as server_module

    class FakePostgresStore:
        def initialize(self):
            pass

        def close(self):
            pass

    captured = {}

    def connect(dsn, *, schema):
        captured["dsn"] = dsn
        return FakePostgresStore()

    monkeypatch.setattr(server_module.PostgresExecutionStateStore, "connect", connect)
    monkeypatch.setattr(server_module.LoomServer, "serve_forever", lambda _server: None)
    monkeypatch.setenv("FLOSSWARE_LOOM_POSTGRES_DSN", "postgresql://environment")
    monkeypatch.setattr(sys, "argv", ["loom-server"])

    server_module.main()

    assert captured["dsn"] == "postgresql://environment"


def test_server_cli_can_enable_postgres_fleet_recovery(monkeypatch) -> None:
    from loom_ai import server as server_module

    class FakeExecutionStore:
        def __init__(self):
            self.initialized = False
            self.closed = False

        def initialize(self):
            self.initialized = True

        def close(self):
            self.closed = True

    class FakeFleetStore:
        def __init__(self):
            self.initialized = False
            self.closed = False
            self.snapshot = None

        def initialize(self):
            self.initialized = True

        def close(self):
            self.closed = True

        def load(self, coordinator_id):
            return self.snapshot

        def save(self, coordinator_id, snapshot):
            self.snapshot = snapshot

        def create(self, coordinator_id, snapshot):
            self.snapshot = snapshot

    execution_store = FakeExecutionStore()
    fleet_store = FakeFleetStore()
    captured = {}

    monkeypatch.setattr(
        server_module.PostgresExecutionStateStore,
        "connect",
        lambda dsn, *, schema: execution_store,
    )
    monkeypatch.setattr(
        server_module.PostgresFleetStateStore,
        "connect",
        lambda dsn, *, schema: fleet_store,
    )

    def stop_after_configuration(server) -> None:
        captured["durable"] = server.orchestrator.durable_enabled

    monkeypatch.setattr(
        server_module.LoomServer, "serve_forever", stop_after_configuration
    )
    monkeypatch.setenv("FLOSSWARE_LOOM_POSTGRES_DSN", "postgresql://execution")
    monkeypatch.setenv("FLOSSWARE_LOOM_FLEET_POSTGRES_DSN", "postgresql://fleet")
    monkeypatch.setenv("LOOM_FLEET_TOKEN", "test-token")
    monkeypatch.setattr(sys, "argv", ["loom-server"])

    server_module.main()

    assert captured["durable"] is True
    assert execution_store.initialized is True
    assert execution_store.closed is True
    assert fleet_store.initialized is True
    assert fleet_store.closed is True
    assert fleet_store.snapshot is not None
