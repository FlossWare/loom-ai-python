"""Authenticated HTTP adapter and independently runnable fleet Worker agent."""

from __future__ import annotations

import hmac
import json
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from loom_ai.fleet import (
    FleetCoordinator,
    FleetError,
    FleetTask,
    StaleLeaseError,
    TaskLease,
    WorkerNotRegisteredError,
)
from loom_ai.intent import Intent
from loom_ai.worker import Worker, WorkerContext, WorkerResult, WorkerStatus

MAX_FLEET_PAYLOAD_BYTES = 10 * 1024 * 1024


class FleetHTTPError(RuntimeError):
    """Raised when an authenticated fleet HTTP operation fails."""


class FleetHTTPServer:
    """HTTP binding for FleetCoordinator; protect remote deployments with TLS."""

    def __init__(
        self,
        coordinator: FleetCoordinator,
        *,
        bearer_token: str,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if not bearer_token.strip():
            raise ValueError("fleet bearer token must not be empty")
        self.coordinator = coordinator
        self.bearer_token = bearer_token
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> ThreadingHTTPServer:
        if self._server is not None:
            raise RuntimeError("fleet HTTP server is already running")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
                    "utf-8"
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                expected = f"Bearer {owner.bearer_token}"
                if hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                    return True
                self._send(HTTPStatus.UNAUTHORIZED, {"error": {"code": "auth.denied"}})
                return False

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_FLEET_PAYLOAD_BYTES:
                    raise ValueError("request payload size is invalid")
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw) if raw else {}
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
                return payload

            def _error(self, status: HTTPStatus, code: str, message: str) -> None:
                self._send(status, {"error": {"code": code, "message": message}})

            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/health":
                    self._send(HTTPStatus.OK, {"status": "ok"})
                    return
                if not self._authorized():
                    return
                if path.startswith("/tasks/"):
                    task_id = unquote(path.removeprefix("/tasks/"))
                    try:
                        self._send(
                            HTTPStatus.OK,
                            _task_payload(owner.coordinator.observe_task(task_id)),
                        )
                    except KeyError:
                        self._error(
                            HTTPStatus.NOT_FOUND, "task.not_found", "task not found"
                        )
                    return
                self._error(HTTPStatus.NOT_FOUND, "route.not_found", "not found")

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                path = urlsplit(self.path).path
                try:
                    payload = self._body()
                    if path == "/workers/register":
                        worker = owner.coordinator.register_worker(
                            _required_string(payload, "worker_id"),
                            _required_string(payload, "implementation_id"),
                            _string_list(payload, "capabilities"),
                            lease_seconds=int(payload.get("lease_seconds", 15)),
                        )
                        self._send(
                            HTTPStatus.OK,
                            {
                                "worker_id": worker.worker_id,
                                "implementation_id": worker.implementation_id,
                                "capabilities": list(worker.capabilities),
                                "lifecycle": worker.lifecycle.value,
                                "lease_seconds": worker.lease_seconds,
                            },
                        )
                        return
                    if path.startswith("/workers/") and path.endswith("/heartbeat"):
                        worker_id = unquote(
                            path.removeprefix("/workers/")
                            .removesuffix("/heartbeat")
                            .strip("/")
                        )
                        worker = owner.coordinator.heartbeat(worker_id)
                        self._send(
                            HTTPStatus.OK,
                            {
                                "worker_id": worker.worker_id,
                                "lifecycle": worker.lifecycle.value,
                            },
                        )
                        return
                    if path.startswith("/workers/") and path.endswith("/drain"):
                        worker_id = unquote(
                            path.removeprefix("/workers/")
                            .removesuffix("/drain")
                            .strip("/")
                        )
                        worker = owner.coordinator.drain_worker(worker_id)
                        self._send(
                            HTTPStatus.OK,
                            {
                                "worker_id": worker.worker_id,
                                "lifecycle": worker.lifecycle.value,
                            },
                        )
                        return
                    if path.startswith("/workers/") and path.endswith("/claim"):
                        worker_id = unquote(
                            path.removeprefix("/workers/")
                            .removesuffix("/claim")
                            .strip("/")
                        )
                        task = owner.coordinator.claim_task(worker_id)
                        self._send(
                            HTTPStatus.OK,
                            {"task": _task_payload(task) if task else None},
                        )
                        return
                    if path.startswith("/tasks/") and path.endswith("/renew"):
                        task_id = unquote(
                            path.removeprefix("/tasks/")
                            .removesuffix("/renew")
                            .strip("/")
                        )
                        lease = owner.coordinator.renew_lease(
                            task_id,
                            worker_id=_required_string(payload, "worker_id"),
                            lease_id=_required_string(payload, "lease_id"),
                            generation=int(payload["generation"]),
                        )
                        self._send(HTTPStatus.OK, _lease_payload(lease))
                        return
                    if path.startswith("/tasks/") and path.endswith("/result"):
                        task_id = unquote(
                            path.removeprefix("/tasks/")
                            .removesuffix("/result")
                            .strip("/")
                        )
                        result_value = payload.get("result")
                        if not isinstance(result_value, dict):
                            raise ValueError("result must be an object")
                        result = _result_from_payload(result_value)
                        json.dumps(
                            _result_payload(result), ensure_ascii=False, allow_nan=False
                        )
                        task = owner.coordinator.submit_result(
                            task_id,
                            worker_id=_required_string(payload, "worker_id"),
                            lease_id=_required_string(payload, "lease_id"),
                            generation=int(payload["generation"]),
                            result=result,
                            retryable=payload.get("retryable") is True,
                        )
                        self._send(HTTPStatus.OK, _task_payload(task))
                        return
                except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    self._error(HTTPStatus.BAD_REQUEST, "request.invalid", str(exc))
                    return
                except StaleLeaseError as exc:
                    self._error(HTTPStatus.CONFLICT, "lease.stale", str(exc))
                    return
                except WorkerNotRegisteredError as exc:
                    self._error(HTTPStatus.CONFLICT, "worker.unavailable", str(exc))
                    return
                except FleetError as exc:
                    self._error(HTTPStatus.CONFLICT, "fleet.conflict", str(exc))
                    return
                self._error(HTTPStatus.NOT_FOUND, "route.not_found", "not found")

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        return self._server

    def close(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        server.shutdown()
        server.server_close()


class FleetWorkerAgent:
    """Independently runnable polling Worker using the authenticated HTTP binding."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        worker: Worker,
        *,
        implementation_id: str,
        capabilities: tuple[str, ...] | list[str],
        lease_seconds: int = 15,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        if not bearer_token.strip():
            raise ValueError("fleet bearer token must not be empty")
        if lease_seconds < 1 or poll_interval_seconds <= 0:
            raise ValueError("lease and polling intervals must be positive")
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.worker = worker
        self.implementation_id = implementation_id
        self.capabilities = tuple(capabilities)
        self.lease_seconds = lease_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._registered = False

    def register(self) -> None:
        self._request(
            "POST",
            "/workers/register",
            {
                "worker_id": self.worker.worker_id,
                "implementation_id": self.implementation_id,
                "capabilities": list(self.capabilities),
                "lease_seconds": self.lease_seconds,
            },
        )
        self._registered = True

    def run_once(self) -> bool:
        """Claim and execute one task; return False when no task is available."""
        if not self._registered:
            self.register()
        self._request("POST", f"/workers/{self.worker.worker_id}/heartbeat", {})
        response = self._request("POST", f"/workers/{self.worker.worker_id}/claim", {})
        task = response.get("task")
        if task is None:
            return False
        lease = task.get("lease")
        if not isinstance(lease, dict):
            raise FleetHTTPError("claimed task did not include a lease")
        intent = _intent_from_payload(task["intent"])
        deadline_value = task.get("deadline")
        deadline = (
            datetime.fromisoformat(deadline_value)
            if isinstance(deadline_value, str)
            else None
        )
        context = WorkerContext(
            intent=intent,
            evidence=tuple(task.get("attempt_history", [])),
            deadline=deadline,
            metadata={
                "task_id": task["task_id"],
                "execution_id": task["execution_id"],
                "attempt": task["attempt"],
                "lease_generation": lease["generation"],
            },
        )
        stop_renewal = Event()
        lease_lost = Event()
        interval = max(0.05, self.lease_seconds / 3)

        def renew_while_running() -> None:
            while not stop_renewal.wait(interval):
                try:
                    self._request(
                        "POST",
                        f"/tasks/{task['task_id']}/renew",
                        {
                            "worker_id": self.worker.worker_id,
                            "lease_id": lease["lease_id"],
                            "generation": lease["generation"],
                        },
                    )
                except FleetHTTPError:
                    lease_lost.set()
                    return

        renewal = Thread(target=renew_while_running, daemon=True)
        renewal.start()
        try:
            result = self.worker.execute(context)
        except Exception as exc:
            result = WorkerResult(
                worker_id=self.worker.worker_id,
                status=WorkerStatus.FAILED,
                error="worker invocation failed",
                evidence=(
                    {"type": "worker-exception", "exception_type": type(exc).__name__},
                ),
                metadata={"retryable": True},
            )
        finally:
            stop_renewal.set()
            renewal.join(timeout=interval + 1)
        if lease_lost.is_set():
            return True
        self._request(
            "POST",
            f"/tasks/{task['task_id']}/result",
            {
                "worker_id": self.worker.worker_id,
                "lease_id": lease["lease_id"],
                "generation": lease["generation"],
                "retryable": result.metadata.get("retryable") is True,
                "result": _result_payload(result),
            },
        )
        return True

    def run_forever(self, stop_event: Event | None = None) -> None:
        """Poll until stopped, then drain so no new tasks are assigned."""
        stop = stop_event or Event()
        if not self._registered:
            self.register()
        try:
            while not stop.is_set():
                if not self.run_once():
                    stop.wait(self.poll_interval_seconds)
        finally:
            self._request("POST", f"/workers/{self.worker.worker_id}/drain", {})

    def _request(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=body if method == "POST" else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error = json.loads(exc.read().decode("utf-8"))
                message = error.get("error", {}).get("code", "HTTP error")
            except (ValueError, AttributeError):
                message = "HTTP error"
            raise FleetHTTPError(f"fleet request failed: {message}") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise FleetHTTPError("fleet request failed") from exc
        if not isinstance(decoded, dict):
            raise FleetHTTPError("fleet response must be a JSON object")
        return decoded


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be an array of strings")
    return value


def _intent_from_payload(payload: dict[str, Any]) -> Intent:
    return Intent(
        goal=_required_string(payload, "goal"),
        requirements=tuple(payload.get("requirements", [])),
        constraints=tuple(payload.get("constraints", [])),
        acceptance=tuple(payload.get("acceptance", [])),
        title=payload.get("title", "Intent"),
        intent_id=_required_string(payload, "intent_id"),
        provenance=payload.get("provenance", {}),
    )


def _intent_payload(intent: Intent) -> dict[str, Any]:
    return {
        "goal": intent.goal,
        "requirements": list(intent.requirements),
        "constraints": list(intent.constraints),
        "acceptance": list(intent.acceptance),
        "title": intent.title,
        "intent_id": intent.intent_id,
        "provenance": dict(intent.provenance),
    }


def _lease_payload(lease: TaskLease) -> dict[str, Any]:
    return {
        "task_id": lease.task_id,
        "lease_id": lease.lease_id,
        "worker_id": lease.worker_id,
        "generation": lease.generation,
        "attempt": lease.attempt,
        "expires_at": lease.expires_at.isoformat(),
    }


def _result_payload(result: WorkerResult) -> dict[str, Any]:
    return {
        "worker_id": result.worker_id,
        "status": result.status.value,
        "output": result.output,
        "evidence": [dict(item) for item in result.evidence],
        "error": result.error,
        "metadata": dict(result.metadata),
    }


def _result_from_payload(payload: dict[str, Any]) -> WorkerResult:
    return WorkerResult(
        worker_id=_required_string(payload, "worker_id"),
        status=WorkerStatus(_required_string(payload, "status")),
        output=payload.get("output"),
        evidence=tuple(payload.get("evidence", [])),
        error=payload.get("error", ""),
        metadata=payload.get("metadata", {}),
    )


def _task_payload(task: FleetTask | None) -> dict[str, Any]:
    if task is None:
        return {}
    return {
        "task_id": task.task_id,
        "execution_id": task.execution_id,
        "idempotency_key": task.idempotency_key,
        "intent": _intent_payload(task.intent),
        "required_capabilities": list(task.required_capabilities),
        "max_attempts": task.max_attempts,
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "submitted_at": task.submitted_at.isoformat(),
        "status": task.status.value,
        "attempt": task.attempt,
        "lease_generation": task.lease_generation,
        "lease": _lease_payload(task.lease) if task.lease else None,
        "result": _result_payload(task.result) if task.result else None,
        "attempt_history": [dict(item) for item in task.attempt_history],
    }
