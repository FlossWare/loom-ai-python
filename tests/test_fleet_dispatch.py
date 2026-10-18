"""End-to-end qualification for Orchestrator-to-fleet execution."""

from __future__ import annotations

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from urllib.request import Request, urlopen

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.execution_state import FileExecutionStateStore
from loom_ai.fleet import FleetCoordinator, FleetTaskStatus
from loom_ai.fleet_dispatch import FleetDispatchWorker
from loom_ai.fleet_http import FleetHTTPServer, FleetWorkerAgent
from loom_ai.fleet_state import FileFleetStateStore
from loom_ai.intent import Intent
from loom_ai.orchestrator import Orchestrator
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class _RemoteWorker:
    worker_id = "remote-worker"

    def execute(self, context: WorkerContext) -> WorkerResult:
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={"goal": context.intent.goal, "task_id": context.metadata["task_id"]},
            evidence=({"type": "remote-worker-completed"},),
        )


def _evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
    return WorkerEvaluation(
        ArbiterDecision.COMPLETE if result.successful else ArbiterDecision.REPLAN,
        reason=result.error or "fleet worker result received",
    )


def _post_json(base_url: str, path: str, payload: dict) -> dict:
    request = Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def test_rest_intent_executes_through_orchestrator_and_remote_fleet_worker(
    tmp_path: Path,
) -> None:
    token = "fleet-integration-test-token"
    coordinator = FleetCoordinator(
        state_store=FileFleetStateStore(tmp_path / "fleet-state")
    )
    fleet_server = FleetHTTPServer(
        coordinator,
        bearer_token=token,
        host="127.0.0.1",
        port=0,
    )
    fleet_httpd = fleet_server.start()
    fleet_thread = Thread(target=fleet_httpd.serve_forever, daemon=True)
    fleet_thread.start()

    agent = FleetWorkerAgent(
        f"http://127.0.0.1:{fleet_server.port}",
        token,
        _RemoteWorker(),
        implementation_id="test-remote-worker",
        capabilities=["loom.execute"],
        lease_seconds=3,
        poll_interval_seconds=0.01,
    )
    stop_agent = Event()

    def run_agent() -> None:
        while not stop_agent.is_set():
            agent.run_once()
            stop_agent.wait(0.01)

    agent_thread = Thread(target=run_agent, daemon=True)
    agent_thread.start()

    dispatch = FleetDispatchWorker(
        coordinator,
        required_capabilities=["loom.execute"],
        wait_timeout_seconds=3,
        poll_interval_seconds=0.01,
    )
    orchestrator = Orchestrator(
        Arbiter([dispatch], _evaluate, max_retries=0),
        execution_store=FileExecutionStateStore(tmp_path / "execution-state"),
    )
    loom_server = LoomServer(orchestrator=orchestrator, host="127.0.0.1", port=0)
    httpd = loom_server.start()
    http_thread = Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()
    base_url = f"http://127.0.0.1:{loom_server.port}"

    try:
        response = _post_json(
            base_url,
            "/intents",
            {
                "goal": "inspect a repository through a remote worker",
                "intent_id": "intent-fleet-rest",
            },
        )
        execution_id = response["execution_id"]
        assert response["status"] == "success"
        assert response["result"]["status"] == "success"
        assert response["result"]["metadata"]["fleet_execution_id"] == execution_id
        task_id = response["result"]["metadata"]["fleet_task_id"]

        request = Request(f"{base_url}/executions/{execution_id}")
        with urlopen(request, timeout=2) as result_response:
            execution = json.load(result_response)
        assert execution["lifecycle"] == "completed"
        assert execution["status"] == "success"

        task = coordinator.observe_task(task_id)
        assert task.status is FleetTaskStatus.SUCCEEDED
        assert task.execution_id == execution_id
        assert task.result is not None
        assert task.result.output["goal"] == "inspect a repository through a remote worker"
    finally:
        stop_agent.set()
        agent_thread.join(timeout=2)
        loom_server.close()
        http_thread.join(timeout=2)
        fleet_server.close()
        fleet_thread.join(timeout=2)


def test_fleet_dispatch_times_out_without_leaving_a_live_lease(tmp_path: Path) -> None:
    coordinator = FleetCoordinator(
        state_store=FileFleetStateStore(tmp_path / "fleet-state")
    )
    dispatch = FleetDispatchWorker(
        coordinator,
        required_capabilities=["loom.execute"],
        wait_timeout_seconds=0.05,
        poll_interval_seconds=0.005,
    )

    result = dispatch.execute(
        WorkerContext(
            intent=Intent(goal="must time out", intent_id="intent-timeout"),
            metadata={"execution_id": "execution-timeout", "phase": "initial"},
        )
    )

    assert result.status is WorkerStatus.FAILED
    assert result.metadata["termination_cause"] == "worker_timeout"
    task = coordinator.observe_task(result.metadata["fleet_task_id"])
    assert task.status is FleetTaskStatus.FAILED
    assert task.lease is None
