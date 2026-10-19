"""Process-level qualification of REST execution through a fleet Worker CLI."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.execution_state import FileExecutionStateStore
import loom_ai.fleet as fleet

from loom_ai.fleet_dispatch import FleetDispatchWorker
from loom_ai.fleet_http import FleetHTTPServer
from loom_ai.fleet_state import FileFleetStateStore
from loom_ai.orchestrator import Orchestrator
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerResult


_WORKER_MODULE = """
from loom_ai.worker import WorkerResult, WorkerStatus

class EchoWorker:
    worker_id = "subprocess-worker"

    def execute(self, context):
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={
                "goal": context.intent.goal,
                "execution_id": context.metadata["execution_id"],
            },
            evidence=({"type": "subprocess-worker"},),
        )

worker = EchoWorker()
"""


def _evaluate(result: WorkerResult, _context) -> WorkerEvaluation:
    return WorkerEvaluation(
        ArbiterDecision.COMPLETE if result.successful else ArbiterDecision.REPLAN,
        reason=result.error or "process Worker result received",
    )


def _post_json(base_url: str, path: str, payload: dict) -> dict:
    request = Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=8) as response:
        return json.load(response)


def test_rest_execution_completes_in_a_separate_worker_process(tmp_path: Path) -> None:
    token = "fleet-process-integration-token"
    (tmp_path / "fleet_test_worker.py").write_text(_WORKER_MODULE, encoding="utf-8")
    coordinator = fleet.FleetCoordinator(
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

    dispatch = FleetDispatchWorker(
        coordinator,
        required_capabilities=["loom.execute"],
        wait_timeout_seconds=8,
        poll_interval_seconds=0.01,
    )
    orchestrator = Orchestrator(
        Arbiter([dispatch], _evaluate, max_retries=0),
        execution_store=FileExecutionStateStore(tmp_path / "execution-state"),
    )
    loom_server = LoomServer(orchestrator=orchestrator, host="127.0.0.1", port=0)
    loom_httpd = loom_server.start()
    loom_thread = Thread(target=loom_httpd.serve_forever, daemon=True)
    loom_thread.start()

    env = os.environ.copy()
    env["LOOM_FLEET_TOKEN"] = token
    env["LOOM_FLEET_URL"] = f"http://127.0.0.1:{fleet_server.port}"
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(tmp_path)
        if not existing_pythonpath
        else str(tmp_path) + os.pathsep + existing_pythonpath
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "loom_ai.fleet_worker_cli",
            "--worker",
            "fleet_test_worker:worker",
            "--implementation-id",
            "subprocess-worker-0.1",
            "--capability",
            "loom.execute",
            "--poll-interval",
            "0.01",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{loom_server.port}"

    try:
        registration_deadline = time.monotonic() + 8
        while True:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                raise AssertionError(
                    f"fleet Worker process exited early: {stdout}\n{stderr}"
                )
            try:
                coordinator.observe_worker("subprocess-worker")
                break
            except fleet.WorkerNotRegisteredError:
                if time.monotonic() >= registration_deadline:
                    raise AssertionError("fleet Worker process did not register")
                time.sleep(0.02)

        response = _post_json(
            base_url,
            "/intents",
            {
                "goal": "execute through a separate process",
                "intent_id": "intent-subprocess-worker",
            },
        )
        execution_id = response["execution_id"]
        assert response["status"] == "success"
        dispatched = response["result"]["output"][0]
        assert dispatched["metadata"]["fleet_execution_id"] == execution_id
        task_id = dispatched["metadata"]["fleet_task_id"]

        with urlopen(
            f"{base_url}/executions/{execution_id}", timeout=3
        ) as result_response:
            execution = json.load(result_response)
        assert execution["status"] == "success"
        assert execution["lifecycle"] == "completed"

        task = coordinator.observe_task(task_id)
        assert task.status is fleet.FleetTaskStatus.SUCCEEDED
        assert task.result is not None
        assert task.result.output == {
            "goal": "execute through a separate process",
            "execution_id": execution_id,
        }
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=3)
        loom_server.close()
        loom_thread.join(timeout=2)
        fleet_server.close()
        fleet_thread.join(timeout=2)
