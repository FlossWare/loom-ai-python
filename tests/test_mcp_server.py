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

        def evaluate(
            result: WorkerResult, context: WorkerContext
        ) -> WorkerEvaluation:
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
                assert observed_payload["status"] == "success"

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


def test_mcp_can_continue_execution_after_http_server_restart() -> None:
    with TemporaryDirectory() as state_dir:

        def build_server() -> LoomServer:
            def evaluate(
                result: WorkerResult, context: WorkerContext
            ) -> WorkerEvaluation:
                if result.successful and context.state.get("phase") == "initial":
                    return WorkerEvaluation(
                        ArbiterDecision.INTERRUPT,
                        reason="waiting for process-boundary continuation",
                    )
                return WorkerEvaluation(
                    ArbiterDecision.COMPLETE,
                    reason="restart recovery",
                )

            return LoomServer(
                Arbiter([RecordingWorker()], evaluate),
                host="127.0.0.1",
                port=0,
                execution_store=FileExecutionStateStore(state_dir),
            )

        def start_server(server: LoomServer) -> Thread:
            http_server = server.start()
            thread = Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            return thread

        first_server = build_server()
        first_thread = start_server(first_server)
        execution_id = ""

        async def submit() -> str:
            mcp_process = StdioServerParameters(
                command=sys.executable,
                args=["scripts/loom_mcp_server.py"],
                env={"LOOM_URL": f"http://127.0.0.1:{first_server.port}"},
            )
            async with Client(mcp_process) as client:
                submitted = await client.call_tool(
                    "loom_submit_intent",
                    {
                        "goal": "survive an HTTP server restart",
                        "intent_id": "intent-restart-preserves-provenance",
                        "provenance": {
                            "client": "mcp-restart-test",
                            "task_path": "tests/test_mcp_server.py",
                        },
                    },
                )
                payload = json.loads(submitted.content[0].text)
                return payload["execution_id"]

        try:
            execution_id = asyncio.run(submit())
            assert execution_id
        finally:
            first_server.close()
            first_thread.join(timeout=2)

        second_server = build_server()
        second_thread = start_server(second_server)

        async def observe_and_continue() -> None:
            mcp_process = StdioServerParameters(
                command=sys.executable,
                args=["scripts/loom_mcp_server.py"],
                env={"LOOM_URL": f"http://127.0.0.1:{second_server.port}"},
            )
            async with Client(mcp_process) as client:
                observed = await client.call_tool(
                    "loom_get_execution",
                    {"execution_id": execution_id},
                )
                observed_payload = json.loads(observed.content[0].text)
                assert observed_payload["execution_id"] == execution_id
                assert observed_payload["intent_id"] == (
                    "intent-restart-preserves-provenance"
                )
                assert observed_payload["status"] == "success"
                assert observed_payload["lifecycle"] == "interrupted"
                assert observed_payload["provenance"] == {
                    "client": "mcp-restart-test",
                    "task_path": "tests/test_mcp_server.py",
                }
                assert observed_payload["result"]["worker_id"] == "arbiter"
                worker_result = observed_payload["result"]["output"][0]
                assert worker_result["worker_id"] == "mcp-test-worker"
                assert worker_result["output"] == {
                    "goal": "survive an HTTP server restart"
                }
                assert worker_result["evidence"] == [{"source": "mcp-e2e"}]
                assert {"source": "mcp-e2e"} in observed_payload["result"]["evidence"]
                assert {"source": "mcp-e2e"} in observed_payload["evidence"]

                continued = await client.call_tool(
                    "loom_continue_execution",
                    {"execution_id": execution_id},
                )
                continued_payload = json.loads(continued.content[0].text)
                assert continued_payload["execution_id"] == execution_id
                assert continued_payload["status"] == "success"

        try:
            asyncio.run(observe_and_continue())
        finally:
            second_server.close()
            second_thread.join(timeout=2)
