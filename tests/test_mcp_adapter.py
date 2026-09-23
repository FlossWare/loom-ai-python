from scripts.loom_mcp_adapter import handle, tool_definitions


class FakeClient:
    def submit(self, arguments):
        return {"execution_id": "exec-1", "status": "success", "goal": arguments["goal"]}

    def observe(self, execution_id):
        return {"execution_id": execution_id, "status": "success"}

    def continue_execution(self, execution_id):
        return {"execution_id": execution_id, "status": "success", "continued": True}


def test_mcp_initialization_and_tools() -> None:
    client = FakeClient()
    initialized = handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        client,
    )
    assert initialized["result"]["capabilities"] == {"tools": {}}
    assert handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        client,
    ) is None

    listed = handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        client,
    )
    assert [tool["name"] for tool in listed["result"]["tools"]] == [
        "loom_submit_intent",
        "loom_get_execution",
        "loom_continue_execution",
    ]
    assert listed["result"]["tools"] == tool_definitions()


def test_mcp_tools_use_public_http_client_boundary() -> None:
    client = FakeClient()
    submitted = handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "loom_submit_intent",
                "arguments": {"goal": "do real work"},
            },
        },
        client,
    )
    assert '"execution_id": "exec-1"' in submitted["result"]["content"][0]["text"]

    observed = handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "loom_get_execution",
                "arguments": {"execution_id": "exec-1"},
            },
        },
        client,
    )
    assert '"execution_id": "exec-1"' in observed["result"]["content"][0]["text"]

    continued = handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "loom_continue_execution",
                "arguments": {"execution_id": "exec-1"},
            },
        },
        client,
    )
    assert '"continued": true' in continued["result"]["content"][0]["text"]
