import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import URLError
from urllib.request import urlopen

from mcp import Client, StdioServerParameters

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.execution_state import FileExecutionStateStore
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus
from scripts.loom_mcp_server import LoomHTTPClient, create_server


class RecordingWorker:
    worker_id = "mcp-test-worker"

    def execute(self, context: WorkerContext) -> WorkerResult:
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={"goal": context.intent.goal},
            evidence=({"source": "mcp-e2e"},),
        )


class FakeClient:
    def submit(self, arguments):
        return {
            "execution_id": "exec-1",
            "status": "success",
            "goal": arguments["goal"],
        }

    def observe(self, execution_id):
        return {"execution_id": execution_id, "status": "success"}

    def continue_execution(self, execution_id):
        return {
            "execution_id": execution_id,
            "status": "success",
            "continued": True,
        }

    def cancel(self, execution_id, reason=None):
        return {
            "execution_id": execution_id,
            "status": "cancellation_requested",
            "reason": reason,
        }

    def record_evaluation(self, execution_id, **arguments):
        return {
            "execution_id": execution_id,
            "status": arguments["status"],
            "evaluation": {"status": arguments["status"]},
        }


def test_mcp_client_discovers_generic_loom_tools() -> None:
    async def run() -> None:
        async with Client(create_server(FakeClient())) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == [
                "loom_submit_intent",
                "loom_get_execution",
                "loom_continue_execution",
                "loom_cancel_execution",
                "loom_record_evaluation",
            ]
            assert client.server_info.name == "loom"

    asyncio.run(run())


def test_mcp_stdio_protocol_with_external_process() -> None:
    async def run() -> None:
        server = StdioServerParameters(
            command=sys.executable,
            args=["scripts/loom_mcp_server.py"],
        )
        async with Client(server) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == [
                "loom_submit_intent",
                "loom_get_execution",
                "loom_continue_execution",
                "loom_cancel_execution",
                "loom_record_evaluation",
            ]

    asyncio.run(run())


def test_mcp_stdio_reaches_real_loom_http_boundary() -> None:
    with TemporaryDirectory() as state_dir:

        def evaluate(result: WorkerResult, context: WorkerContext) -> WorkerEvaluation:
            if result.successful and context.state.get("phase") == "initial":
                return WorkerEvaluation(
                    ArbiterDecision.INTERRUPT,
                    reason="initial MCP phase complete",
                )
            return WorkerEvaluation(
                ArbiterDecision.COMPLETE,
                reason="mcp e2e",
            )

        server = LoomServer(
            Arbiter([RecordingWorker()], evaluate),
            host="127.0.0.1",
            port=0,
            execution_store=FileExecutionStateStore(state_dir),
        )
        http_server = server.start()
        thread = Thread(target=http_server.serve_forever, daemon=True)
        thread.start()

        async def run() -> None:
            mcp_process = StdioServerParameters(
                command=sys.executable,
                args=["scripts/loom_mcp_server.py"],
                env={"LOOM_URL": f"http://127.0.0.1:{server.port}"},
            )
            async with Client(mcp_process) as client:
                tools = await client.list_tools()
                assert len(tools.tools) == 5

                submitted = await client.call_tool(
                    "loom_submit_intent",
                    {"goal": "exercise the real MCP boundary"},
                )
                submitted_payload = json.loads(submitted.content[0].text)
                execution_id = submitted_payload["execution_id"]
                assert execution_id

                observed = await client.call_tool(
                    "loom_get_execution",
                    {"execution_id": execution_id},
                )
                observed_payload = json.loads(observed.content[0].text)
                assert observed_payload["execution_id"] == execution_id
                assert observed_payload["status"] == "failed"

                continued = await client.call_tool(
                    "loom_continue_execution",
                    {"execution_id": execution_id},
                )
                continued_payload = json.loads(continued.content[0].text)
                assert continued_payload["execution_id"] == execution_id
                assert continued_payload["status"] == "success"

                evaluated = await client.call_tool(
                    "loom_record_evaluation",
                    {
                        "execution_id": execution_id,
                        "status": "met",
                        "criteria": ["task completed"],
                        "evaluator": "mcp-integration-test",
                    },
                )
                evaluated_payload = json.loads(evaluated.content[0].text)
                assert evaluated_payload["lifecycle"] == "completed"
                assert evaluated_payload["evaluation"]["status"] == "met"

                cancelled = await client.call_tool(
                    "loom_cancel_execution",
                    {"execution_id": execution_id, "reason": "integration test"},
                )
                cancelled_payload = json.loads(cancelled.content[0].text)
                assert cancelled_payload["execution_id"] == execution_id
                assert cancelled_payload["cancellation_accepted"] is False
                assert cancelled_payload["cancellation_pending"] is False

        try:
            asyncio.run(run())
        finally:
            server.close()
            thread.join(timeout=2)


def test_mcp_client_invokes_all_loom_tools() -> None:
    async def run() -> None:
        async with Client(create_server(FakeClient())) as client:
            submitted = await client.call_tool(
                "loom_submit_intent",
                {"goal": "do real work"},
            )
            assert submitted.is_error is False
            submitted_payload = json.loads(submitted.content[0].text)
            assert submitted_payload["execution_id"] == "exec-1"

            observed = await client.call_tool(
                "loom_get_execution",
                {"execution_id": "exec-1"},
            )
            observed_payload = json.loads(observed.content[0].text)
            assert observed_payload["execution_id"] == "exec-1"

            continued = await client.call_tool(
                "loom_continue_execution",
                {"execution_id": "exec-1"},
            )
            continued_payload = json.loads(continued.content[0].text)
            assert continued_payload["continued"] is True

            cancelled = await client.call_tool(
                "loom_cancel_execution",
                {"execution_id": "exec-1", "reason": "test cancellation"},
            )
            cancelled_payload = json.loads(cancelled.content[0].text)
            assert cancelled_payload["status"] == "cancellation_requested"
            assert cancelled_payload["reason"] == "test cancellation"

            evaluated = await client.call_tool(
                "loom_record_evaluation",
                {"execution_id": "exec-1", "status": "met"},
            )
            evaluated_payload = json.loads(evaluated.content[0].text)
            assert evaluated_payload["evaluation"]["status"] == "met"

    asyncio.run(run())


def test_http_client_maps_only_public_loom_endpoints() -> None:
    client = LoomHTTPClient("http://example.test")
    assert client.base_url == "http://example.test"
    assert callable(client.submit)
    assert callable(client.observe)
    assert callable(client.continue_execution)
    assert callable(client.cancel)
    assert callable(client.record_evaluation)


def test_http_client_encodes_execution_id_as_one_path_segment() -> None:
    class RecordingClient(LoomHTTPClient):
        def _request(self, method, path, payload=None):
            return {"method": method, "path": path, "payload": payload}

    client = RecordingClient("http://example.test")
    observed = client.observe("execution/with spaces")
    continued = client.continue_execution("execution/with spaces")
    cancelled = client.cancel("execution/with spaces", "stop now")
    evaluation = client.record_evaluation(
        "execution/with spaces",
        status="met",
        criteria=["complete"],
    )

    assert observed["path"] == "/executions/execution%2Fwith%20spaces"
    assert continued["path"] == "/executions/execution%2Fwith%20spaces/continue"
    assert cancelled["path"] == "/executions/execution%2Fwith%20spaces/cancel"
    assert cancelled["payload"] == {"reason": "stop now"}
    assert evaluation["path"] == "/executions/execution%2Fwith%20spaces/evaluations"
    assert evaluation["payload"] == {"status": "met", "criteria": ["complete"]}


def test_mcp_can_continue_execution_after_loom_process_restart() -> None:
    with TemporaryDirectory() as state_dir:
        repository_root = Path(__file__).resolve().parents[1]
        server_script = Path(state_dir) / "loom_process.py"
        server_script.write_text(
            """
import sys
from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.execution_state import FileExecutionStateStore
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus

class RecordingWorker:
    worker_id = "mcp-process-worker"

    def execute(self, context: WorkerContext) -> WorkerResult:
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={"goal": context.intent.goal},
            evidence=({"source": "mcp-process-restart"},),
        )

def evaluate(result: WorkerResult, context: WorkerContext) -> WorkerEvaluation:
    if result.successful and context.state.get("phase") == "initial":
        return WorkerEvaluation(
            ArbiterDecision.INTERRUPT,
            reason="waiting for process-boundary continuation",
        )
    return WorkerEvaluation(ArbiterDecision.COMPLETE, reason="restart recovery")

server = LoomServer(
    Arbiter([RecordingWorker()], evaluate),
    host="127.0.0.1",
    port=int(sys.argv[2]),
    execution_store=FileExecutionStateStore(sys.argv[1]),
)
server.serve_forever()
""",
            encoding="utf-8",
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"

        def start_loom_process() -> subprocess.Popen:
            env = os.environ.copy()
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                str(repository_root)
                if not existing_pythonpath
                else str(repository_root) + os.pathsep + existing_pythonpath
            )
            process = subprocess.Popen(
                [sys.executable, str(server_script), state_dir, str(port)],
                cwd=repository_root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            startup_deadline = time.monotonic() + 8
            while time.monotonic() < startup_deadline:
                if process.poll() is not None:
                    _, stderr = process.communicate(timeout=1)
                    raise AssertionError(
                        f"Loom process exited during startup: {stderr}"
                    )
                try:
                    with urlopen(f"{base_url}/health", timeout=0.2) as response:
                        if response.status == 200:
                            return process
                except URLError:
                    time.sleep(0.05)
            process.terminate()
            process.wait(timeout=3)
            raise AssertionError("Loom process did not become healthy")

        def stop_loom_process(process: subprocess.Popen) -> None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

        loom_process = start_loom_process()

        async def run() -> None:
            nonlocal loom_process
            mcp_process = StdioServerParameters(
                command=sys.executable,
                args=[str(repository_root / "scripts/loom_mcp_server.py")],
                env={"LOOM_URL": base_url},
            )
            async with Client(mcp_process) as client:
                submitted = await client.call_tool(
                    "loom_submit_intent",
                    {
                        "goal": "survive a real Loom process restart",
                        "intent_id": "intent-process-restart",
                        "provenance": {
                            "client": "mcp-process-restart-test",
                            "task_path": "tests/test_mcp_server.py",
                        },
                    },
                )
                submitted_payload = json.loads(submitted.content[0].text)
                execution_id = submitted_payload["execution_id"]
                assert execution_id
                assert submitted_payload["lifecycle"] == "interrupted"

                stop_loom_process(loom_process)
                loom_process = start_loom_process()

                observed = await client.call_tool(
                    "loom_get_execution",
                    {"execution_id": execution_id},
                )
                observed_payload = json.loads(observed.content[0].text)
                assert observed_payload["execution_id"] == execution_id
                assert observed_payload["intent_id"] == "intent-process-restart"
                assert observed_payload["lifecycle"] == "interrupted"
                assert observed_payload["provenance"] == {
                    "client": "mcp-process-restart-test",
                    "task_path": "tests/test_mcp_server.py",
                }
                assert observed_payload["result"]["output"][0]["worker_id"] == (
                    "mcp-process-worker"
                )
                assert {"source": "mcp-process-restart"} in observed_payload["evidence"]

                continued = await client.call_tool(
                    "loom_continue_execution",
                    {"execution_id": execution_id},
                )
                continued_payload = json.loads(continued.content[0].text)
                assert continued_payload["execution_id"] == execution_id
                assert continued_payload["status"] == "success"

        try:
            asyncio.run(run())
        finally:
            stop_loom_process(loom_process)


def test_mcp_submit_forwards_optional_intent_fields_without_loss() -> None:
    class RecordingClient(FakeClient):
        def __init__(self) -> None:
            self.submitted_arguments = None

        def submit(self, arguments):
            self.submitted_arguments = arguments
            return {"execution_id": "exec-forwarded", "status": "accepted"}

    recording = RecordingClient()

    async def run() -> None:
        async with Client(create_server(recording)) as client:
            submitted = await client.call_tool(
                "loom_submit_intent",
                {
                    "goal": "preserve intent contract across MCP",
                    "title": "MCP field forwarding",
                    "requirements": ["keep requirements"],
                    "constraints": ["keep constraints"],
                    "acceptance": ["keep acceptance"],
                    "intent_id": "intent-forwarding",
                    "execution_id": "execution-forwarding",
                    "deadline": "2030-01-02T03:04:05Z",
                    "provenance": {
                        "client": "test-client",
                        "task_path": "tests/test_mcp_server.py",
                    },
                },
            )
            assert submitted.is_error is False
            payload = json.loads(submitted.content[0].text)
            assert payload["execution_id"] == "exec-forwarded"

    asyncio.run(run())

    assert recording.submitted_arguments == {
        "goal": "preserve intent contract across MCP",
        "title": "MCP field forwarding",
        "requirements": ["keep requirements"],
        "constraints": ["keep constraints"],
        "acceptance": ["keep acceptance"],
        "intent_id": "intent-forwarding",
        "execution_id": "execution-forwarding",
        "deadline": "2030-01-02T03:04:05Z",
        "provenance": {
            "client": "test-client",
            "task_path": "tests/test_mcp_server.py",
        },
    }
