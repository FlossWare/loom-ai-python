from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from loom_ai.fleet import FleetCoordinator
from loom_ai.fleet_http import FleetHTTPServer, FleetWorkerAgent
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class RecordingWorker:
    def __init__(self, worker_id: str, output: str) -> None:
        self._worker_id = worker_id
        self.output = output
        self.calls = 0

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def execute(self, context: WorkerContext) -> WorkerResult:
        self.calls += 1
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={"output": self.output, "intent_id": context.intent.intent_id},
            evidence=({"type": "worker.completed"},),
        )


def start_server():
    coordinator = FleetCoordinator()
    server = FleetHTTPServer(coordinator, bearer_token="test-token", port=0)
    httpd = server.start()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return coordinator, server, thread, f"http://127.0.0.1:{server.port}"


def post_json(base_url: str, path: str, payload: dict, token: str = "test-token"):
    request = Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(base_url: str, path: str, token: str = "test-token"):
    request = Request(base_url + path, headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def test_two_independent_http_agents_claim_and_execute_matching_tasks() -> None:
    _coordinator, server, thread, base_url = start_server()
    worker_a = TestWorker("worker-a", "alpha")
    worker_b = TestWorker("worker-b", "beta")
    agent_a = FleetWorkerAgent(
        base_url,
        "test-token",
        worker_a,
        implementation_id="impl-a",
        capabilities=["cap.a"],
    )
    agent_b = FleetWorkerAgent(
        base_url,
        "test-token",
        worker_b,
        implementation_id="impl-b",
        capabilities=["cap.b"],
    )
    try:
        agent_a.register()
        agent_b.register()
        for task_id, cap in (("task-a", "cap.a"), ("task-b", "cap.b")):
            post_json(
                base_url,
                "/tasks",
                {
                    "task_id": task_id,
                    "execution_id": "exec-" + task_id,
                    "idempotency_key": task_id,
                    "intent": {
                        "goal": "execute " + task_id,
                        "intent_id": "intent-" + task_id,
                    },
                    "required_capabilities": [cap],
                    "max_attempts": 2,
                },
            )

        assert agent_a.run_once() is True
        assert agent_b.run_once() is True
        task_a = get_json(base_url, "/tasks/task-a")
        task_b = get_json(base_url, "/tasks/task-b")

        assert task_a["status"] == "succeeded"
        assert task_a["result"]["output"]["output"] == "alpha"
        assert task_b["status"] == "succeeded"
        assert task_b["result"]["output"]["output"] == "beta"
        assert worker_a.calls == 1
        assert worker_b.calls == 1
    finally:
        server.close()
        thread.join(timeout=2)


def test_fleet_http_requires_bearer_authentication() -> None:
    _coordinator, server, thread, base_url = start_server()
    try:
        request = Request(
            base_url + "/workers/register",
            data=json.dumps(
                {
                    "worker_id": "worker-a",
                    "implementation_id": "impl-a",
                    "capabilities": ["cap.a"],
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=3)
        assert error.value.code == 401
    finally:
        server.close()
        thread.join(timeout=2)


def test_agent_rejects_non_http_base_url() -> None:
    with pytest.raises(ValueError, match="HTTP"):
        FleetWorkerAgent(
            "file:///tmp/fleet",
            "test-token",
            TestWorker("worker-a", "unused"),
            implementation_id="impl-a",
            capabilities=["cap.a"],
        )


def test_http_server_rejects_non_json_result_before_commit() -> None:
    _coordinator, server, thread, base_url = start_server()
    worker = TestWorker("worker-a", "unused")
    agent = FleetWorkerAgent(
        base_url,
        "test-token",
        worker,
        implementation_id="impl-a",
        capabilities=["cap.a"],
    )
    try:
        agent.register()
        post_json(
            base_url,
            "/tasks",
            {
                "task_id": "task-nan",
                "execution_id": "exec-nan",
                "intent": {"goal": "bad result", "intent_id": "intent-nan"},
                "required_capabilities": ["cap.a"],
            },
        )
        leased = post_json(base_url, "/workers/worker-a/claim", {})
        task = leased["task"]
        lease = task["lease"]
        with pytest.raises(HTTPError) as error:
            post_json(
                base_url,
                "/tasks/task-nan/result",
                {
                    "worker_id": "worker-a",
                    "lease_id": lease["lease_id"],
                    "generation": lease["generation"],
                    "result": {
                        "worker_id": "worker-a",
                        "status": "success",
                        "output": float("nan"),
                    },
                },
            )
        assert error.value.code == 400
        assert get_json(base_url, "/tasks/task-nan")["status"] == "running"
    finally:
        server.close()
        thread.join(timeout=2)
