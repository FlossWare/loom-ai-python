import asyncio
import json
import sys

from mcp import Client, StdioServerParameters

from scripts.loom_mcp_server import LoomHTTPClient, create_server


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
