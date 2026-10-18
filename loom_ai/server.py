"""HTTP transport adapter for the Loom Orchestrator application boundary.

This module owns HTTP parsing and response serialization only. Execution,
durable lifecycle, and continuation are delegated to Orchestrator.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.evaluation import EvaluationRecord, EvaluationStatus
from loom_ai.execution_state import (
    ExecutionState,
    ExecutionStateError,
    ExecutionStateStore,
    FileExecutionStateStore,
)
from loom_ai.fleet import (
    FleetConflictError,
    FleetCoordinator,
    FleetError,
    StaleLeaseError,
    WorkerNotRegisteredError,
)
from loom_ai.intent import Intent
from loom_ai.orchestrator import (
    ExecutionAlreadyExistsError,
    ExecutionNotResumableError,
    Orchestrator,
    parse_deadline,
)
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
        fleet_coordinator: FleetCoordinator | None = None,
        fleet_token: str | None = None,
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
        if fleet_coordinator is not None and (fleet_token is None or not fleet_token.strip()):
            raise ValueError("fleet_token is required when fleet coordination is enabled")
        self.fleet_coordinator = fleet_coordinator
        self.fleet_token = fleet_token
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
                request_path = urlsplit(self.path).path
                if self._fleet_request("GET", request_path):
                    return
                if request_path == "/health":
                    self._send(HTTPStatus.OK, {"status": "ok"})
                    return
                if request_path.startswith("/executions/"):
                    execution_id = unquote(request_path.removeprefix("/executions/"))
                    if not execution_id:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload("route.not_found", "not found"),
                        )
                        return
                    if not owner.orchestrator.durable_enabled:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload(
                                "execution.durability_unavailable",
                                "durable execution state is not configured",
                            ),
                        )
                        return
                    try:
                        execution = owner.orchestrator.observe(execution_id)
                    except KeyError:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload(
                                "execution.not_found", "execution not found"
                            ),
                        )
                        return
                    except ExecutionStateError as exc:
                        self._send(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            _error_payload("execution.state_error", str(exc)),
                        )
                        return
                    self._send(HTTPStatus.OK, _execution_payload(execution))
                    return
                self._send(
                    HTTPStatus.NOT_FOUND,
                    _error_payload("route.not_found", "not found"),
                )

            def do_POST(self) -> None:  # noqa: N802
                request_path = urlsplit(self.path).path
                if self._fleet_request("POST", request_path):
                    return
                if request_path.startswith("/executions/") and request_path.endswith(
                    "/evaluations"
                ):
                    execution_id = (
                        request_path.removeprefix("/executions/")
                        .removesuffix("/evaluations")
                        .strip("/")
                    )
                    execution_id = unquote(execution_id)
                    if not execution_id:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload("route.not_found", "not found"),
                        )
                        return
                    if not owner.orchestrator.durable_enabled:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload(
                                "execution.durability_unavailable",
                                "durable execution state is not configured",
                            ),
                        )
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if length < 0:
                            raise ValueError("Content-Length must be non-negative")
                        if length > MAX_PAYLOAD_BYTES:
                            self._send(
                                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                _error_payload(
                                    "request.payload_too_large", "payload too large"
                                ),
                            )
                            return
                        raw_body = self.rfile.read(length).decode("utf-8")
                        payload = json.loads(raw_body) if raw_body else {}
                        if not isinstance(payload, dict):
                            raise TypeError("request body must be a JSON object")
                        status = EvaluationStatus(
                            payload.get("status", "not_evaluated")
                        )
                        criteria = payload.get("criteria", [])
                        evidence = payload.get("evidence", [])
                        provenance = payload.get("provenance", {})
                        if not isinstance(criteria, list) or not all(
                            isinstance(item, str) for item in criteria
                        ):
                            raise TypeError("criteria must be an array of strings")
                        if not isinstance(evidence, list) or not all(
                            isinstance(item, dict) for item in evidence
                        ):
                            raise TypeError("evidence must be an array of objects")
                        if not isinstance(provenance, dict):
                            raise TypeError("provenance must be an object")
                        optional_strings = (
                            "criteria_version",
                            "evaluator",
                            "evaluator_version",
                            "result_reference",
                        )
                        for field_name in optional_strings:
                            if payload.get(field_name) is not None and not isinstance(
                                payload[field_name], str
                            ):
                                raise TypeError(f"{field_name} must be a string")
                        evaluation = EvaluationRecord(
                            status=status,
                            criteria=tuple(criteria),
                            criteria_version=payload.get("criteria_version"),
                            evaluator=payload.get("evaluator"),
                            evaluator_version=payload.get("evaluator_version"),
                            result_reference=payload.get("result_reference"),
                            evidence=tuple(evidence),
                            provenance=provenance,
                        )
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        self._send(
                            HTTPStatus.BAD_REQUEST,
                            _error_payload("request.invalid", str(exc)),
                        )
                        return
                    try:
                        execution = owner.orchestrator.record_evaluation(
                            execution_id, evaluation
                        )
                    except KeyError:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload(
                                "execution.not_found", "execution not found"
                            ),
                        )
                        return
                    except (TypeError, ValueError) as exc:
                        self._send(
                            HTTPStatus.BAD_REQUEST,
                            _error_payload("evaluation.invalid", str(exc)),
                        )
                        return
                    except ExecutionStateError as exc:
                        self._send(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            _error_payload("execution.state_error", str(exc)),
                        )
                        return
                    self._send(HTTPStatus.OK, _execution_payload(execution))
                    return

                if request_path.startswith("/executions/") and request_path.endswith(
                    "/cancel"
                ):
                    execution_id = (
                        request_path.removeprefix("/executions/")
                        .removesuffix("/cancel")
                        .strip("/")
                    )
                    execution_id = unquote(execution_id)
                    if not execution_id:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload("route.not_found", "not found"),
                        )
                        return
                    if not owner.orchestrator.durable_enabled:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload(
                                "execution.durability_unavailable",
                                "durable execution state is not configured",
                            ),
                        )
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if length < 0:
                            raise ValueError("Content-Length must be non-negative")
                        if length > MAX_PAYLOAD_BYTES:
                            self._send(
                                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                _error_payload(
                                    "request.payload_too_large", "payload too large"
                                ),
                            )
                            return
                        raw_body = self.rfile.read(length).decode("utf-8")
                        payload = json.loads(raw_body) if raw_body else {}
                        if not isinstance(payload, dict):
                            raise TypeError("request body must be a JSON object")
                        reason = payload.get("reason", "cancellation requested")
                        if not isinstance(reason, str) or not reason.strip():
                            raise ValueError("reason must be a non-empty string")
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        self._send(
                            HTTPStatus.BAD_REQUEST,
                            _error_payload("request.invalid", str(exc)),
                        )
                        return
                    try:
                        cancellation = owner.orchestrator.cancel(
                            execution_id,
                            reason=reason,
                        )
                    except KeyError as exc:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload("execution.not_found", str(exc)),
                        )
                        return
                    except ExecutionStateError as exc:
                        self._send(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            _error_payload("execution.state_error", str(exc)),
                        )
                        return
                    response = _execution_payload(cancellation.execution)
                    response["cancellation_accepted"] = cancellation.accepted
                    response["cancellation_pending"] = cancellation.pending
                    self._send(HTTPStatus.OK, response)
                    return

                if request_path.startswith("/executions/") and request_path.endswith(
                    "/continue"
                ):
                    execution_id = (
                        request_path.removeprefix("/executions/")
                        .removesuffix("/continue")
                        .strip("/")
                    )
                    execution_id = unquote(execution_id)
                    if not execution_id:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload("route.not_found", "not found"),
                        )
                        return
                    if not owner.orchestrator.durable_enabled:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload(
                                "execution.durability_unavailable",
                                "durable execution state is not configured",
                            ),
                        )
                        return
                    try:
                        submission = owner.orchestrator.continue_execution(execution_id)
                    except ExecutionNotResumableError as exc:
                        self._send(
                            HTTPStatus.CONFLICT,
                            _error_payload(
                                "execution.not_resumable",
                                str(exc),
                                execution_id=execution_id,
                            ),
                        )
                        return
                    except KeyError as exc:
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            _error_payload("execution.not_found", str(exc)),
                        )
                        return
                    except ExecutionStateError as exc:
                        self._send(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            _error_payload("execution.state_error", str(exc)),
                        )
                        return
                    except Exception as exc:  # pragma: no cover
                        self._send(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            _error_payload("server.internal_error", str(exc)),
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

                if request_path != "/intents":
                    self._send(
                        HTTPStatus.NOT_FOUND,
                        _error_payload("route.not_found", "not found"),
                    )
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
                            _error_payload(
                                "request.payload_too_large", "payload too large"
                            ),
                        )
                        return

                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    intent, requested_execution_id, deadline = _parse_intent(payload)
                    if (
                        owner.orchestrator.durable_enabled
                        and requested_execution_id is None
                    ):
                        requested_execution_id = str(uuid4())
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        _error_payload("request.invalid", str(exc)),
                    )
                    return

                try:
                    submission = owner.orchestrator.submit(
                        intent,
                        execution_id=requested_execution_id,
                        deadline=deadline,
                    )
                except ExecutionAlreadyExistsError as exc:
                    self._send(
                        HTTPStatus.CONFLICT,
                        _error_payload(
                            "execution.identity_conflict",
                            str(exc),
                            execution_id=requested_execution_id,
                        ),
                    )
                    return
                except Exception as exc:  # pragma: no cover
                    error_payload = _error_payload("server.internal_error", str(exc))
                    if (
                        owner.orchestrator.durable_enabled
                        and requested_execution_id is not None
                    ):
                        error_payload["execution_id"] = requested_execution_id
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, error_payload)
                    return

                response = _result_payload(submission.result)
                if submission.execution_id is not None:
                    response["execution_id"] = submission.execution_id
                self._send(HTTPStatus.OK, response)

            def _fleet_request(self, method: str, path: str) -> bool:
                if not path.startswith("/fleet/"):
                    return False
                coordinator = owner.fleet_coordinator
                if coordinator is None:
                    self._send(
                        HTTPStatus.NOT_FOUND,
                        _error_payload("fleet.unavailable", "fleet coordination is not configured"),
                    )
                    return True
                expected = owner.fleet_token
                supplied = self.headers.get("Authorization", "")
                if expected is None or not hmac.compare_digest(
                    supplied.encode("utf-8"), f"Bearer {expected}".encode("utf-8")
                ):
                    self._send(
                        HTTPStatus.UNAUTHORIZED,
                        _error_payload("fleet.unauthorized", "valid Bearer token required"),
                    )
                    return True

                try:
                    payload: dict[str, Any] = {}
                    if method == "POST":
                        raw_length = self.headers.get("Content-Length", "0")
                        length = int(raw_length)
                        if length < 0:
                            raise ValueError("Content-Length must be non-negative")
                        if length > MAX_PAYLOAD_BYTES:
                            self._send(
                                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                _error_payload("request.payload_too_large", "payload too large"),
                            )
                            return True
                        raw_body = self.rfile.read(length).decode("utf-8") if length else ""
                        decoded = json.loads(raw_body) if raw_body else {}
                        if not isinstance(decoded, dict):
                            raise TypeError("request body must be a JSON object")
                        payload = decoded

                    if method == "POST" and path == "/fleet/workers/register":
                        worker_id = _required_string(payload, "worker_id")
                        implementation_id = _required_string(payload, "implementation_id")
                        capabilities = payload.get("capabilities", [])
                        if not isinstance(capabilities, list) or not all(
                            isinstance(item, str) for item in capabilities
                        ):
                            raise TypeError("capabilities must be an array of strings")
                        lease_seconds = payload.get("lease_seconds", 15)
                        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
                            raise TypeError("lease_seconds must be an integer")
                        registration = coordinator.register_worker(
                            worker_id,
                            implementation_id,
                            capabilities,
                            lease_seconds=lease_seconds,
                        )
                        self._send(HTTPStatus.OK, {"worker": asdict(registration)})
                        return True

                    parts = [unquote(part) for part in path.strip("/").split("/")]
                    if method == "GET" and len(parts) == 3 and parts[:2] == ["fleet", "workers"]:
                        self._send(
                            HTTPStatus.OK,
                            {"worker": asdict(coordinator.observe_worker(parts[2]))},
                        )
                        return True
                    if method == "GET" and len(parts) == 3 and parts[:2] == ["fleet", "tasks"]:
                        self._send(
                            HTTPStatus.OK,
                            {"task": asdict(coordinator.observe_task(parts[2]))},
                        )
                        return True
                    if method == "POST" and len(parts) == 4 and parts[:2] == ["fleet", "workers"]:
                        worker_id, operation = parts[2], parts[3]
                        if operation == "heartbeat":
                            self._send(
                                HTTPStatus.OK,
                                {"worker": asdict(coordinator.heartbeat(worker_id))},
                            )
                            return True
                        if operation == "claim":
                            task = coordinator.claim_task(worker_id)
                            self._send(
                                HTTPStatus.OK,
                                {"task": asdict(task) if task is not None else None},
                            )
                            return True
                    if method == "POST" and len(parts) == 4 and parts[:2] == ["fleet", "tasks"] and parts[3] == "result":
                        task_id = parts[2]
                        worker_id = _required_string(payload, "worker_id")
                        lease_id = _required_string(payload, "lease_id")
                        generation = payload.get("generation")
                        if isinstance(generation, bool) or not isinstance(generation, int):
                            raise TypeError("generation must be an integer")
                        status = WorkerStatus(_required_string(payload, "status"))
                        evidence = payload.get("evidence", [])
                        metadata = payload.get("metadata", {})
                        if not isinstance(evidence, list) or not all(
                            isinstance(item, dict) for item in evidence
                        ):
                            raise TypeError("evidence must be an array of objects")
                        if not isinstance(metadata, dict):
                            raise TypeError("metadata must be an object")
                        error = payload.get("error", "")
                        if not isinstance(error, str):
                            raise TypeError("error must be a string")
                        retryable = payload.get("retryable", False)
                        if not isinstance(retryable, bool):
                            raise TypeError("retryable must be a boolean")
                        result = WorkerResult(
                            worker_id=worker_id,
                            status=status,
                            output=payload.get("output"),
                            evidence=tuple(evidence),
                            error=error,
                            metadata=metadata,
                        )
                        task = coordinator.submit_result(
                            task_id,
                            worker_id=worker_id,
                            lease_id=lease_id,
                            generation=generation,
                            result=result,
                            retryable=retryable,
                        )
                        self._send(HTTPStatus.OK, {"task": asdict(task)})
                        return True
                    self._send(
                        HTTPStatus.NOT_FOUND,
                        _error_payload("route.not_found", "fleet route not found"),
                    )
                    return True
                except (KeyError, WorkerNotRegisteredError) as exc:
                    self._send(
                        HTTPStatus.NOT_FOUND,
                        _error_payload("fleet.not_found", str(exc)),
                    )
                    return True
                except (FleetConflictError, StaleLeaseError) as exc:
                    self._send(
                        HTTPStatus.CONFLICT,
                        _error_payload("fleet.conflict", str(exc)),
                    )
                    return True
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        _error_payload("request.invalid", str(exc)),
                    )
                    return True
                except FleetError as exc:
                    self._send(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        _error_payload("fleet.state_error", str(exc)),
                    )
                    return True

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        return Handler


def _parse_intent(payload: Any) -> tuple[Intent, str | None, datetime | None]:
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
    deadline_value = payload.get("deadline")
    if deadline_value is not None and not isinstance(deadline_value, str):
        raise TypeError("deadline must be an RFC 3339 timestamp")
    deadline = parse_deadline(deadline_value)

    intent = Intent(
        title=title,
        goal=goal,
        requirements=sequences["requirements"],
        constraints=sequences["constraints"],
        acceptance=sequences["acceptance"],
        intent_id=intent_id or str(uuid4()),
        provenance=provenance,
    )
    return intent, execution_id or None, deadline


def _execution_payload(execution: ExecutionState) -> dict[str, Any]:
    """Serialize durable observable execution state."""
    return {
        "execution_id": execution.execution_id,
        "intent_id": execution.intent.intent_id,
        "status": execution.status,
        "lifecycle": execution.lifecycle,
        "evaluation": execution.evaluation.to_dict(),
        "evaluations": [item.to_dict() for item in execution.evaluations],
        "state": execution.state,
        "evidence": execution.evidence,
        "metadata": execution.metadata,
        "result": execution.result,
        "representation_version": execution.representation_version,
        "provenance": execution.intent.provenance,
    }


def _result_payload(result: WorkerResult) -> dict[str, Any]:
    structured_result = {
        "worker_id": result.worker_id,
        "status": result.status.value,
        "output": result.output,
        "evidence": result.evidence,
        "error": result.error,
        "metadata": result.metadata,
    }
    lifecycle = (
        "interrupted"
        if result.metadata.get("continuation_pending") is True
        else "completed"
    )
    return {
        **structured_result,
        "status": result.status.value,
        "lifecycle": lifecycle,
        "evaluation": EvaluationRecord().to_dict(),
        "result": structured_result,
    }


def _required_string(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _error_payload(category: str, message: str, **details: Any) -> dict[str, Any]:
    """Build a backward-compatible error body with a stable machine category."""
    return {"category": category, "error": message, **details}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
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
    parser.add_argument(
        "--fleet-state-dir",
        help="directory for durable fleet coordinator snapshots",
    )
    parser.add_argument(
        "--state-dir",
        help=(
            "directory for durable execution snapshots; without this option, "
            "execution observation and continuation are unavailable"
        ),
    )
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
    execution_store = (
        FileExecutionStateStore(args.state_dir) if args.state_dir else None
    )
    fleet_token = os.environ.get("LOOM_FLEET_TOKEN")
    fleet_coordinator = None
    if args.fleet_state_dir:
        if not fleet_token:
            parser.error("--fleet-state-dir requires LOOM_FLEET_TOKEN to be set")
        from loom_ai.fleet_state import FileFleetStateStore

        fleet_coordinator = FleetCoordinator(
            state_store=FileFleetStateStore(args.fleet_state_dir)
        )
    LoomServer(
        arbiter,
        host=args.host,
        port=args.port,
        execution_store=execution_store,
        fleet_coordinator=fleet_coordinator,
        fleet_token=fleet_token if fleet_coordinator is not None else None,
    ).serve_forever()


if __name__ == "__main__":
    main()
