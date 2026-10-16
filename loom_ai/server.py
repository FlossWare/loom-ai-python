"""HTTP transport adapter for the Loom Orchestrator application boundary.

This module owns HTTP parsing and response serialization only. Execution,
durable lifecycle, and continuation are delegated to Orchestrator.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.execution_state import (
    ExecutionState,
    ExecutionStateError,
    ExecutionStateStore,
)
from loom_ai.intent import Intent
from loom_ai.orchestrator import Orchestrator
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus

MAX_PAYLOAD_BYTES = 10 * 1024 * 1024


class _LoomHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class LoomServer:
    """HTTP transport adapter for a configured Loom Orchestrator."""

    def __init__(
        self,
        arbiter: Arbiter | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        execution_store: ExecutionStateStore | None = None,
        orchestrator: Orchestrator | None = None,
    ) -> None:
        if orchestrator is not None:
            if arbiter is not None or execution_store is not None:
                raise ValueError(
                    "pass either orchestrator or arbiter/execution_store, not both"
                )
            self.orchestrator = orchestrator
        else:
            if arbiter is None:
                raise ValueError("an Arbiter or Orchestrator is required")
            self.orchestrator = Orchestrator(
                arbiter,
                execution_store=execution_store,
            )
        self.host = host
        self.port = port
        self._server: _LoomHTTPServer | None = None

    @property
    def arbiter(self) -> Arbiter:
        """Compatibility access to the Arbiter owned by Orchestrator."""
        return self.orchestrator.arbiter

    @property
    def execution_store(self) -> ExecutionStateStore | None:
        """Compatibility access to the store owned by Orchestrator."""
        return self.orchestrator.execution_store

    def execute(self, intent: Intent) -> WorkerResult:
        """Compatibility wrapper; execution authority remains Orchestrator."""
        return self.orchestrator.execute(intent)

    def execute_persisted(self, intent: Intent, execution_id: str) -> WorkerResult:
        """Compatibility wrapper for a durable Orchestrator submission."""
        if not self.orchestrator.durable_enabled:
            raise RuntimeError("durable execution state is not configured")
        return self.orchestrator.submit(intent, execution_id=execution_id).result

    def continue_execution(self, execution_id: str) -> WorkerResult:
        """Compatibility wrapper for Orchestrator continuation."""
        return self.orchestrator.continue_execution(execution_id).result

    def start(self) -> _LoomHTTPServer:
        """Bind the HTTP server and return its running server instance."""
        if self._server is not None:
            raise RuntimeError("Loom server is already running")
        handler = self._handler_factory()
        self._server = _LoomHTTPServer((self.host, self.port), handler)
        self.port = self._server.server_address[1]
        return self._server

    def close(self) -> None:
        """Stop and close a running HTTP server."""
        server = self._server
        if server is None:
            return
        self._server = None
        server.shutdown()
        server.server_close()

    def serve_forever(self) -> None:
        """Serve requests until interrupted."""
        server = self._server or self.start()
        try:
            server.serve_forever()
        finally:
            if self._server is server:
                self._server = None
                server.server_close()

    def _handler_factory(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, default=_json_default).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._send(HTTPStatus.OK, {"status": "ok"})
                    return
                if self.path.startswith("/executions/"):
                    execution_id = self.path.removeprefix("/executions/")
                    if not execution_id or not owner.orchestrator.durable_enabled:
                        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    try:
                        execution = owner.orchestrator.observe(execution_id)
                    except KeyError as exc:
                        self._send(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                        return
                    except ExecutionStateError as exc:
                        self._send(
                            HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
                        )
                        return
                    self._send(HTTPStatus.OK, _execution_payload(execution))
                    return
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path.startswith("/executions/") and self.path.endswith(
                    "/continue"
                ):
                    execution_id = (
                        self.path.removeprefix("/executions/")
                        .removesuffix("/continue")
                        .strip("/")
                    )
                    if not execution_id or not owner.orchestrator.durable_enabled:
                        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    try:
                        submission = owner.orchestrator.continue_execution(execution_id)
                    except KeyError as exc:
                        self._send(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                        return
                    except ExecutionStateError as exc:
                        self._send(
                            HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
                        )
                        return
                    self._send(
                        HTTPStatus.OK,
                        {
                            "execution_id": submission.execution_id,
                            **_result_payload(submission.result),
                        },
                    )
                    return

                if self.path != "/intents":
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return

                try:
                    length = self.headers.get("Content-Length")
                    if length is None:
                        raise ValueError("Content-Length is required")
                    length = int(length)
                    if length < 0:
                        raise ValueError("Content-Length must be non-negative")
                    if length > MAX_PAYLOAD_BYTES:
                        self._send(
                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            {"error": "payload too large"},
                        )
                        return

                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    intent, requested_execution_id = _parse_intent(payload)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                    return

                try:
                    submission = owner.orchestrator.submit(
                        intent,
                        execution_id=requested_execution_id,
                    )
                except Exception as exc:  # pragma: no cover
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                    return

                response = _result_payload(submission.result)
                if submission.execution_id is not None:
                    response["execution_id"] = submission.execution_id
                self._send(HTTPStatus.OK, response)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        return Handler


def _parse_intent(payload: Any) -> tuple[Intent, str | None]:
    """Validate an HTTP Intent representation and translate it to the contract."""
    if not isinstance(payload, dict):
        raise TypeError("request body must be a JSON object")

    title = payload.get("title", "Intent")
    goal = payload.get("goal")
    if not isinstance(title, str):
        raise TypeError("title must be a string")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal must be a non-empty string")

    sequences: dict[str, tuple[str, ...]] = {}
    for field in ("requirements", "constraints", "acceptance"):
        values = payload.get(field, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise TypeError(f"{field} must be an array of strings")
        sequences[field] = tuple(values)

    intent_id = payload.get("intent_id")
    if intent_id is not None and not isinstance(intent_id, str):
        raise TypeError("intent_id must be a string")
    provenance = payload.get("provenance", {})
    if not isinstance(provenance, dict):
        raise TypeError("provenance must be a JSON object")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in provenance.items()
    ):
        raise TypeError("provenance keys and values must be strings")

    execution_id = payload.get("execution_id")
    if execution_id is not None and not isinstance(execution_id, str):
        raise TypeError("execution_id must be a string")

    intent = Intent(
        title=title,
        goal=goal,
        requirements=sequences["requirements"],
        constraints=sequences["constraints"],
        acceptance=sequences["acceptance"],
        intent_id=intent_id or str(uuid4()),
        provenance=provenance,
    )
    return intent, execution_id or None


def _execution_payload(execution: ExecutionState) -> dict[str, Any]:
    """Serialize durable observable execution state."""
    return {
        "execution_id": execution.execution_id,
        "intent_id": execution.intent.intent_id,
        "status": execution.status,
        "state": execution.state,
        "evidence": execution.evidence,
        "metadata": execution.metadata,
        "representation_version": execution.representation_version,
        "provenance": execution.intent.provenance,
    }


def _result_payload(result: WorkerResult) -> dict[str, Any]:
    return {
        "worker_id": result.worker_id,
        "status": result.status.value,
        "output": result.output,
        "evidence": result.evidence,
        "error": result.error,
        "metadata": result.metadata,
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def main() -> None:
    """Run a transport-only server for manual health checks."""
    parser = argparse.ArgumentParser(description="Run the Loom HTTP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    class NoOpWorker:
        worker_id = "server"

        def execute(self, context: WorkerContext) -> WorkerResult:
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.SUCCESS,
                output={"intent_id": context.intent.intent_id},
            )

    def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
        if result.successful:
            return WorkerEvaluation(
                ArbiterDecision.COMPLETE, reason="transport smoke test"
            )
        return WorkerEvaluation(
            ArbiterDecision.REPLAN, reason=result.error or "worker failed"
        )

    arbiter = Arbiter([NoOpWorker()], evaluate, max_retries=0)
    LoomServer(arbiter, host=args.host, port=args.port).serve_forever()


if __name__ == "__main__":
    main()
