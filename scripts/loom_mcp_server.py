#!/usr/bin/env python3
"""Generic MCP server for the public Loom HTTP API.

The MCP server is a protocol realization, not an orchestration layer. It exposes
Loom's public execution semantics to any MCP-capable client and keeps all state,
workers, verification, and provenance in Loom.
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from mcp.server import MCPServer

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
        encoded_id = quote(execution_id, safe="")
        return self._request("GET", f"/executions/{encoded_id}")

    def continue_execution(self, execution_id: str) -> dict:
        encoded_id = quote(execution_id, safe="")
        return self._request("POST", f"/executions/{encoded_id}/continue", {})

    def cancel(self, execution_id: str, reason: str | None = None) -> dict:
        encoded_id = quote(execution_id, safe="")
        payload = {"reason": reason} if reason is not None else {}
        return self._request("POST", f"/executions/{encoded_id}/cancel", payload)

    def record_evaluation(
        self,
        execution_id: str,
        *,
        status: str,
        criteria: list[str] | None = None,
        criteria_version: str | None = None,
        evaluator: str | None = None,
        evaluator_version: str | None = None,
        evidence: list[dict] | None = None,
        provenance: dict | None = None,
    ) -> dict:
        encoded_id = quote(execution_id, safe="")
        payload = {"status": status}
        for key, value in {
            "criteria": criteria,
            "criteria_version": criteria_version,
            "evaluator": evaluator,
            "evaluator_version": evaluator_version,
            "evidence": evidence,
            "provenance": provenance,
        }.items():
            if value is not None:
                payload[key] = value
        return self._request(
            "POST", f"/executions/{encoded_id}/evaluations", payload
        )


def create_server(client: LoomHTTPClient) -> MCPServer:
    server = MCPServer(
        "loom",
        version="0.1",
        instructions=(
            "Loom is the execution service. Use these tools to submit intents, "
            "observe and continue durable executions, and request cancellation. "
            "A submission may include an absolute RFC 3339 deadline. Cancellation "
            "may remain pending until active work stops. This MCP server does not "
            "own execution state."
        ),
    )

    @server.tool(
        name="loom_submit_intent",
        description=(
            "Submit an Intent to Loom and return its execution result. "
            "Loom owns execution, workers, verification, state, and evidence."
        ),
    )
    def submit_intent(
        goal: str,
        title: str | None = None,
        requirements: list[str] | None = None,
        constraints: list[str] | None = None,
        acceptance: list[str] | None = None,
        intent_id: str | None = None,
        execution_id: str | None = None,
        deadline: str | None = None,
        provenance: dict[str, str] | None = None,
    ) -> dict:
        arguments = {
            "goal": goal,
            **({"title": title} if title is not None else {}),
            **({"requirements": requirements} if requirements is not None else {}),
            **({"constraints": constraints} if constraints is not None else {}),
            **({"acceptance": acceptance} if acceptance is not None else {}),
            **({"intent_id": intent_id} if intent_id is not None else {}),
            **({"execution_id": execution_id} if execution_id is not None else {}),
            **({"deadline": deadline} if deadline is not None else {}),
            **({"provenance": provenance} if provenance is not None else {}),
        }
        return client.submit(arguments)

    @server.tool(
        name="loom_get_execution",
        description="Observe durable Loom execution state by execution_id.",
    )
    def get_execution(execution_id: str) -> dict:
        return client.observe(execution_id)

    @server.tool(
        name="loom_continue_execution",
        description="Continue a durable Loom execution by execution_id.",
    )
    def continue_execution(execution_id: str) -> dict:
        return client.continue_execution(execution_id)

    @server.tool(
        name="loom_cancel_execution",
        description=(
            "Request cooperative cancellation of a durable Loom execution. "
            "The result distinguishes accepted cancellation from confirmed termination."
        ),
    )
    def cancel_execution(
        execution_id: str,
        reason: str | None = None,
    ) -> dict:
        return client.cancel(execution_id, reason)

    @server.tool(
        name="loom_record_evaluation",
        description=(
            "Append an attributable task evaluation to a completed Loom execution. "
            "Evaluation status is separate from execution lifecycle and Result outcome."
        ),
    )
    def record_evaluation(
        execution_id: str,
        status: str,
        criteria: list[str] | None = None,
        criteria_version: str | None = None,
        evaluator: str | None = None,
        evaluator_version: str | None = None,
        evidence: list[dict] | None = None,
        provenance: dict | None = None,
    ) -> dict:
        return client.record_evaluation(
            execution_id,
            status=status,
            criteria=criteria,
            criteria_version=criteria_version,
            evaluator=evaluator,
            evaluator_version=evaluator_version,
            evidence=evidence,
            provenance=provenance,
        )

    return server


def main() -> None:
    base_url = os.environ.get("LOOM_URL", "http://127.0.0.1:8000")
    timeout = int(os.environ.get("LOOM_HTTP_TIMEOUT", DEFAULT_TIMEOUT))
    create_server(LoomHTTPClient(base_url, timeout)).run()


if __name__ == "__main__":
    main()
