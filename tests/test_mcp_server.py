import asyncio
import json
import sys
from tempfile import TemporaryDirectory
from threading import Thread

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


def test_mcp_client_discovers_generic_loom_tools() -> None:
    async def run() -> None:
        async with Client(create_server(FakeClient())) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == [
                "loom_submit_intent",
                "loom_get_execution",
                "loom_continue_execution",
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
            ]

    asyncio.run(run())


def test_mcp_stdio_reaches_real_loom_http_boundary() -> None:
    with TemporaryDirectory() as state_dir:
        def evaluate(
            result: WorkerResult, _context: WorkerContext
        ) -> WorkerEvaluation:
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
                assert len(tools.tools) == 3

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
                assert observed_payload["status"] == "success"

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

    asyncio.run(run())


def test_http_client_maps_only_public_loom_endpoints() -> None:
    client = LoomHTTPClient("http://example.test")
    assert client.base_url == "http://example.test"
    assert callable(client.submit)
    assert callable(client.observe)
    assert callable(client.continue_execution)


def test_http_client_encodes_execution_id_as_one_path_segment() -> None:
    class RecordingClient(LoomHTTPClient):
        def _request(self, method, path, payload=None):
            return {"method": method, "path": path, "payload": payload}

    client = RecordingClient("http://example.test")
    observed = client.observe("execution/with spaces")
    continued = client.continue_execution("execution/with spaces")

    assert observed["path"] == "/executions/execution%2Fwith%20spaces"
    assert continued["path"] == "/executions/execution%2Fwith%20spaces/continue"
