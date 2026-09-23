#!/usr/bin/env python3
"""Thin stdio MCP adapter from Crush to the public Loom HTTP API.

This adapter owns no Loom execution state. It translates MCP tool calls into
POST /intents, GET /executions/{execution_id}, and
POST /executions/{execution_id}/continue. All execution remains in Loom.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "loom-http-adapter"
SERVER_VERSION = "0.1"
DEFAULT_TIMEOUT = 30


class LoomHTTPClient:
    def __init__(self, base_url: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> dict:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Loom HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Loom HTTP connection failed: {exc.reason}") from exc

    def submit(self, arguments: dict) -> dict:
        return self._request("POST", "/intents", arguments)

    def observe(self, execution_id: str) -> dict:
        return self._request("GET", f"/executions/{execution_id}")

    def continue_execution(self, execution_id: str) -> dict:
        return self._request("POST", f"/executions/{execution_id}/continue", {})


def tool_definitions() -> list[dict]:
    return [
        {
            "name": "loom_submit_intent",
            "description": "Submit an Intent to the public Loom HTTP API and return the execution result.",
            "inputSchema": {
                "type": "object",
                "required": ["goal"],
                "properties": {
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "requirements": {"type": "array", "items": {"type": "string"}},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "acceptance": {"type": "array", "items": {"type": "string"}},
                    "intent_id": {"type": "string"},
                    "execution_id": {"type": "string"},
                    "provenance": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
            },
        },
        {
            "name": "loom_get_execution",
            "description": "Observe durable Loom execution state by execution_id.",
            "inputSchema": {
                "type": "object",
                "required": ["execution_id"],
                "properties": {"execution_id": {"type": "string"}},
            },
        },
        {
            "name": "loom_continue_execution",
            "description": "Continue a durable Loom execution by execution_id.",
            "inputSchema": {
                "type": "object",
                "required": ["execution_id"],
                "properties": {"execution_id": {"type": "string"}},
            },
        },
    ]


def call_tool(client: LoomHTTPClient, name: str, arguments: dict) -> dict:
    if name == "loom_submit_intent":
        return client.submit(arguments)
    if name == "loom_get_execution":
        return client.observe(arguments["execution_id"])
    if name == "loom_continue_execution":
        return client.continue_execution(arguments["execution_id"])
    raise ValueError(f"unknown tool: {name}")


def result_message(request_id: object, payload: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, sort_keys=True),
                }
            ]
        },
    }


def error_message(request_id: object, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle(request: dict, client: LoomHTTPClient) -> dict | None:
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": tool_definitions()},
        }

    if method == "tools/call":
        try:
            params = request.get("params", {})
            name = params["name"]
            arguments = params.get("arguments", {})
            payload = call_tool(client, name, arguments)
            return result_message(request_id, payload)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            return error_message(request_id, -32602, str(exc))

    if request_id is None:
        return None
    return error_message(request_id, -32601, f"method not found: {method}")


def main() -> None:
    base_url = os.environ.get("LOOM_URL", "http://127.0.0.1:8000")
    timeout = int(os.environ.get("LOOM_HTTP_TIMEOUT", DEFAULT_TIMEOUT))
    client = LoomHTTPClient(base_url, timeout)

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise TypeError("MCP message must be a JSON object")
            response = handle(request, client)
        except (json.JSONDecodeError, TypeError) as exc:
            response = error_message(None, -32700, str(exc))
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
