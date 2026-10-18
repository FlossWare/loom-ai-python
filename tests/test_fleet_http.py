"""Tests for the authenticated HTTP fleet-worker boundary."""

from __future__ import annotations

import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.fleet import FleetCoordinator
from loom_ai.intent import Intent
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class _SmokeWorker:
    worker_id = "http-smoke"

    def execute(self, context: WorkerContext) -> WorkerResult:
        return WorkerResult(worker_id=self.worker_id, status=WorkerStatus.SUCCESS)


def _evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
    return WorkerEvaluation(ArbiterDecision.COMPLETE, reason=result.error)


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    token: str | None = "fleet-test-token",
) -> dict:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urlopen(request, timeout=2) as response:
        return json.load(response)


def test_fleet_http_register_claim_report_and_observe() -> None:
    coordinator = FleetCoordinator()
    server = LoomServer(
        Arbiter([_SmokeWorker()], _evaluate, max_retries=0),
        fleet_coordinator=coordinator,
        fleet_token="fleet-test-token",
        port=0,
    )
    http_server = server.start()
    thread = Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.host}:{server.port}"
    try:
        registered = _request(
            base_url,
            "/fleet/workers/register",
            method="POST",
            payload={
                "worker_id": "worker-a",
                "implementation_id": "test-worker",
                "capabilities": ["repository.read"],
            },
        )
        assert registered["worker"]["worker_id"] == "worker-a"
        assert registered["worker"]["lifecycle"] == "ready"

        coordinator.submit_task(
            Intent(goal="read repository", intent_id="intent-http"),
            execution_id="execution-http",
            task_id="task-http",
            required_capabilities=["repository.read"],
        )
        claimed = _request(
            base_url,
            "/fleet/workers/worker-a/claim",
            method="POST",
            payload={},
        )
        task = claimed["task"]
        assert task["task_id"] == "task-http"
        lease = task["lease"]
        assert lease["worker_id"] == "worker-a"

        completed = _request(
            base_url,
            "/fleet/tasks/task-http/result",
            method="POST",
            payload={
                "worker_id": "worker-a",
                "lease_id": lease["lease_id"],
                "generation": lease["generation"],
                "status": "success",
                "output": {"documents": 3},
                "evidence": [{"type": "http-worker"}],
                "metadata": {"source": "test"},
            },
        )
        assert completed["task"]["status"] == "succeeded"
        assert completed["task"]["result"]["output"] == {"documents": 3}

        observed = _request(base_url, "/fleet/tasks/task-http")
        assert observed["task"]["status"] == "succeeded"
        worker = _request(base_url, "/fleet/workers/worker-a")
        assert worker["worker"]["worker_id"] == "worker-a"
    finally:
        server.close()
        thread.join(timeout=2)


def test_fleet_http_requires_bearer_token() -> None:
    server = LoomServer(
        Arbiter([_SmokeWorker()], _evaluate, max_retries=0),
        fleet_coordinator=FleetCoordinator(),
        fleet_token="fleet-test-token",
        port=0,
    )
    http_server = server.start()
    thread = Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://{server.host}:{server.port}/fleet/workers/register",
            data=json.dumps(
                {
                    "worker_id": "worker-a",
                    "implementation_id": "test-worker",
                    "capabilities": [],
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=2)
        assert error.value.code == 401
        assert json.load(error.value)["category"] == "fleet.unauthorized"
    finally:
        server.close()
        thread.join(timeout=2)


def test_fleet_http_requires_token_when_fleet_is_configured() -> None:
    with pytest.raises(ValueError, match="fleet_token is required"):
        LoomServer(
            Arbiter([_SmokeWorker()], _evaluate, max_retries=0),
            fleet_coordinator=FleetCoordinator(),
            port=0,
        )
