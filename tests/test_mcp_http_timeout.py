from __future__ import annotations

import pytest

from scripts import loom_mcp_server


def test_http_client_reports_timeout_with_configured_limit(monkeypatch) -> None:
    def timeout(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(loom_mcp_server, "urlopen", timeout)
    client = loom_mcp_server.LoomHTTPClient("http://127.0.0.1:8000", timeout=7)

    with pytest.raises(
        RuntimeError,
        match="Loom HTTP request timed out after 7 seconds",
    ):
        client.observe("execution-1")


def test_http_client_preserves_http_error_details(monkeypatch) -> None:
    from urllib.error import HTTPError
    from io import BytesIO

    def reject(*_args, **_kwargs):
        raise HTTPError(
            "http://127.0.0.1:8000/executions/execution-1",
            503,
            "Unavailable",
            hdrs=None,
            fp=BytesIO(b'{"error":"temporarily unavailable"}'),
        )

    monkeypatch.setattr(loom_mcp_server, "urlopen", reject)
    client = loom_mcp_server.LoomHTTPClient("http://127.0.0.1:8000")

    with pytest.raises(
        RuntimeError,
        match=r'Loom HTTP 503: \{"error":"temporarily unavailable"\}',
    ):
        client.observe("execution-1")
