"""Minimal HTTP transport for the Loom execution substrate.

The server owns transport and request-to-Intent translation. Execution remains
owned by the supplied Arbiter and its Workers. Provider/model access is
deliberately not part of this module.
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
from loom_ai.intent import Intent
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus

MAX_PAYLOAD_BYTES = 10 * 1024 * 1024


class _LoomHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class LoomServer:
    """HTTP transport boundary for a configured Loom Arbiter."""

    def __init__(
        self, arbiter: Arbiter, *, host: str = "127.0.0.1", port: int = 8000
    ) -> None:
        self.host = host
        self.port = port
        self.arbiter = arbiter
        self._server: _LoomHTTPServer | None = None

    def execute(self, intent: Intent) -> WorkerResult:
        """Execute an Intent through the configured Arbiter."""
        return self.arbiter.execute(WorkerContext(intent=intent))

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
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
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
                    if not isinstance(payload, dict):
                        raise TypeError("request body must be a JSON object")

                    provenance = payload.get("provenance", {})
                    if not isinstance(provenance, dict):
                        raise TypeError("provenance must be a JSON object")
                    if not all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in provenance.items()
                    ):
                        raise TypeError("provenance keys and values must be strings")

                    intent = Intent(
                        title=payload.get("title", "Intent"),
                        goal=payload["goal"],
                        requirements=tuple(payload.get("requirements", ())),
                        constraints=tuple(payload.get("constraints", ())),
                        acceptance=tuple(payload.get("acceptance", ())),
                        intent_id=payload.get("intent_id") or str(uuid4()),
                        provenance=provenance,
                    )
                    result = owner.execute(intent)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                    return

                self._send(HTTPStatus.OK, _result_payload(result))

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        return Handler


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
