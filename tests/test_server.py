"""Tests for the Loom HTTP transport boundary."""

from __future__ import annotations

import json
import socketserver
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


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
            payload = json.load(response)

        assert payload["status"] == "success"
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
        except URLError:
            # Server closed connection early upon detecting over-sized header/payload
            pass
        else:
            raise AssertionError("expected request rejection for overlarge payload")
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)
