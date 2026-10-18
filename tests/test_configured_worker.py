"""Tests for activating and enforcing configured model Workers."""

from __future__ import annotations

import pytest

from loom_ai import (
    CancellationToken,
    FakeModelProvider,
    Intent,
    ModelResponse,
    WorkerContext,
    WorkerStatus,
)
from loom_ai.capability_runtime import CapabilityInvocationError
from loom_ai.configured_worker import WorkerActivationError, activate_model_worker
from loom_ai.model import ModelRequest
from loom_ai.worker_configuration import validate_worker_configuration


def raw_config(**overrides):
    value = {
        "schema_version": "0.1",
        "worker_id": "configured-coder",
        "purpose": "Perform bounded implementation work",
        "model_gateway": {"gateway_ref": "local-gateway", "selection_ref": "coding"},
        "instructions": ["Prefer minimal, tested changes"],
        "capabilities": ["repository.read"],
        "policy": {"policy_ref": "safe", "approval_required": True},
        "limits": {
            "max_attempts": 2,
            "max_duration_seconds": 5,
            "max_output_bytes": 512,
        },
    }
    value.update(overrides)
    return value


def validated(value=None, *, capabilities=None):
    return validate_worker_configuration(
        value or raw_config(),
        gateway_selections={"local-gateway": {"coding"}},
        available_capabilities=capabilities or {"repository.read"},
        policy_registry={
            "safe": {
                "approval_required": True,
                "allowed_capabilities": ["repository.read"],
                "denied_capabilities": [],
            }
        },
    )


class RecordingProvider:
    provider_id = "recording"

    def __init__(self, texts):
        self.texts = list(texts)
        self.requests = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        text = self.texts.pop(0)
        if isinstance(text, Exception):
            raise text
        return ModelResponse(text=text, provider=self.provider_id, model=request.model)


def test_activation_resolves_logical_gateway_and_selection() -> None:
    config = validated()
    provider = FakeModelProvider()

    worker = activate_model_worker(
        config,
        model_selections={("local-gateway", "coding"): (provider, "fake-model")},
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="write a test")))

    assert result.status is WorkerStatus.SUCCESS
    assert result.metadata["configured_worker_id"] == "configured-coder"
    assert result.metadata["attempts"] == 1
    assert result.metadata["output_bytes"] <= 512


def test_activation_fails_closed_when_selection_has_no_binding() -> None:
    with pytest.raises(WorkerActivationError, match="no runtime binding"):
        activate_model_worker(validated(), model_selections={})


def test_configured_instructions_are_sent_to_provider() -> None:
    provider = RecordingProvider(["short response"])
    worker = activate_model_worker(
        validated(),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="write a test")))

    assert result.status is WorkerStatus.SUCCESS
    assert "Perform bounded implementation work" in provider.requests[0].prompt
    assert "Prefer minimal, tested changes" in provider.requests[0].prompt
    assert "write a test" in provider.requests[0].prompt
    assert 4.9 < provider.requests[0].timeout_seconds <= 5.0


def test_output_limit_is_enforced_in_utf8_bytes() -> None:
    raw = raw_config()
    raw["limits"]["max_output_bytes"] = 64
    provider = RecordingProvider(["é" * 40])
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="make output")))

    assert result.status is WorkerStatus.FAILED
    assert result.error == "configured output limit exceeded"
    assert result.metadata["termination_cause"] == "output_limit"
    assert (
        next(
            item
            for item in result.evidence
            if item.get("type") == "configured-output-limit"
        )["actual_bytes"]
        == 80
    )


def test_failed_calls_are_retried_only_to_configured_attempt_limit() -> None:
    provider = RecordingProvider(
        [RuntimeError("temporary"), RuntimeError("still failing"), "unused"]
    )
    worker = activate_model_worker(
        validated(),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="recover")))

    assert result.status is WorkerStatus.FAILED
    assert result.metadata["attempts"] == 2
    assert len(provider.requests) == 2


def test_duration_budget_rejects_slow_synchronous_provider_result(monkeypatch) -> None:
    import loom_ai.configured_worker as configured_worker_module

    clock = iter([0.0, 0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(configured_worker_module, "monotonic", lambda: next(clock))
    provider = RecordingProvider(["too late"])
    raw = raw_config()
    raw["limits"]["max_duration_seconds"] = 1
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="timeout")))

    assert result.status is WorkerStatus.FAILED
    assert result.error == "configured duration limit exceeded"
    assert result.metadata["termination_cause"] == "duration_limit"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_model_request_rejects_invalid_timeout(timeout) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ModelRequest(prompt="test", timeout_seconds=timeout)


def test_retry_provider_timeout_uses_remaining_budget(monkeypatch) -> None:
    import loom_ai.configured_worker as configured_worker_module

    clock = iter([0.0, 0.0, 0.0, 3.0, 3.0, 3.0, 4.0, 4.0])
    monkeypatch.setattr(configured_worker_module, "monotonic", lambda: next(clock))
    provider = RecordingProvider([RuntimeError("temporary"), "recovered"])
    worker = activate_model_worker(
        validated(),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="retry safely")))

    assert result.status is WorkerStatus.SUCCESS
    assert [request.timeout_seconds for request in provider.requests] == [5.0, 2.0]


def test_cancellation_during_provider_call_overrides_success() -> None:
    token = CancellationToken()

    class CancellingProvider(RecordingProvider):
        def generate(self, request: ModelRequest) -> ModelResponse:
            token.cancel("cancelled during model call")
            return super().generate(request)

    provider = CancellingProvider(["apparently successful response"])
    worker = activate_model_worker(
        validated(),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
    )

    result = worker.execute(
        WorkerContext(
            intent=Intent(goal="honor cancellation"),
            cancellation_token=token,
        )
    )

    assert result.status is WorkerStatus.CANCELLED
    assert result.metadata["termination_cause"] == "cancelled"
    assert any(
        item.get("type") == "configured-worker-termination"
        and item.get("cause") == "cancelled"
        for item in result.evidence
    )


def test_configured_worker_invokes_declared_tool_through_trusted_binding() -> None:
    raw = raw_config()
    raw["tools"] = [{"capability_ref": "repository.read", "required": True}]
    calls = []
    approvals = []
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (FakeModelProvider(), "fake")},
        capability_bindings={
            "repository.read": lambda args: calls.append(args) or "file contents"
        },
        approval_authorizer=lambda _config, capability, _args: (
            approvals.append(capability) or True
        ),
    )

    result = worker.invoke_tool("repository.read", {"path": "README.md"})

    assert result == "file contents"
    assert calls == [{"path": "README.md"}]
    assert approvals == ["repository.read"]


def test_configured_worker_does_not_invoke_capability_without_approval() -> None:
    raw = raw_config()
    raw["tools"] = [{"capability_ref": "repository.read", "required": True}]
    calls = []
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (FakeModelProvider(), "fake")},
        capability_bindings={
            "repository.read": lambda args: calls.append(args) or "file contents"
        },
    )

    with pytest.raises(CapabilityInvocationError, match="approval authorizer"):
        worker.invoke_tool("repository.read", {"path": "README.md"})

    assert calls == []


def test_configured_worker_rejects_capability_not_declared_as_tool() -> None:
    worker = activate_model_worker(
        validated(),
        model_selections={("local-gateway", "coding"): (FakeModelProvider(), "fake")},
        capability_bindings={"repository.read": lambda _args: "contents"},
    )

    with pytest.raises(
        WorkerActivationError, match="not declared as a configured tool"
    ):
        worker.invoke_tool("repository.read", {"path": "README.md"})


def test_configured_worker_enforces_declared_input_schema() -> None:
    raw = raw_config()
    raw["tools"] = [
        {
            "capability_ref": "repository.read",
            "required": True,
            "input_schema_ref": "repository-read-input",
        }
    ]
    calls = []
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (FakeModelProvider(), "fake")},
        capability_bindings={
            "repository.read": lambda args: calls.append(args) or "contents"
        },
        approval_authorizer=lambda *_: True,
        input_schema_validators={
            "repository-read-input": lambda args: isinstance(args.get("path"), str)
        },
    )

    with pytest.raises(CapabilityInvocationError, match="do not satisfy"):
        worker.invoke_tool("repository.read", {"path": 123})

    assert calls == []
    assert worker.invoke_tool("repository.read", {"path": "README.md"}) == "contents"
    assert calls == [{"path": "README.md"}]


def test_model_response_accepts_provider_neutral_tool_calls() -> None:
    from loom_ai import ModelToolCall

    call = ModelToolCall(
        call_id="call-1",
        tool_name="repository.read",
        arguments={"path": "README.md"},
    )
    response = ModelResponse(
        text="",
        provider="recording",
        model="model-a",
        kind="tool_calls",
        tool_calls=(call,),
    )

    assert response.kind == "tool_calls"
    assert response.tool_calls == (call,)
    assert response.tool_calls[0].arguments["path"] == "README.md"


@pytest.mark.parametrize(
    "call",
    [
        {"call_id": "", "tool_name": "repository.read", "arguments": {}},
        {"call_id": "call-1", "tool_name": "", "arguments": {}},
        {
            "call_id": "call-1",
            "tool_name": "repository.read",
            "arguments": {"bad": object()},
        },
    ],
)
def test_model_tool_call_rejects_invalid_contract_values(call) -> None:
    from loom_ai import ModelToolCall

    with pytest.raises(ValueError):
        ModelToolCall(**call)


def test_model_response_rejects_duplicate_tool_call_ids() -> None:
    from loom_ai import ModelToolCall

    call = ModelToolCall("same-id", "repository.read", {})
    with pytest.raises(ValueError, match="identifiers must be unique"):
        ModelResponse(
            text="",
            provider="recording",
            model="model-a",
            kind="tool_calls",
            tool_calls=(call, call),
        )


def test_model_response_requires_explicit_final_or_tool_call_kind() -> None:
    with pytest.raises(ValueError, match="kind must be"):
        ModelResponse(
            text="answer",
            provider="recording",
            model="model-a",
            kind="implicit-tools",
        )


class StructuredRecordingProvider:
    provider_id = "recording"

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_configured_worker_executes_structured_tool_and_returns_to_model() -> None:
    from loom_ai import ModelToolCall

    raw = raw_config()
    raw["tools"] = [
        {
            "capability_ref": "repository.read",
            "name": "read-file",
            "description": "Read a repository file",
            "required": True,
            "input_schema_ref": "repository-read",
        }
    ]
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    provider = StructuredRecordingProvider(
        [
            ModelResponse(
                text="",
                provider="recording",
                model="model-a",
                kind="tool_calls",
                tool_calls=(
                    ModelToolCall("call-1", "read-file", {"path": "README.md"}),
                ),
            ),
            ModelResponse(
                text="README says hello.",
                provider="recording",
                model="model-a",
            ),
        ]
    )
    calls = []
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
        capability_bindings={
            "repository.read": lambda args: calls.append(args) or {"contents": "hello"}
        },
        approval_authorizer=lambda _config, _capability, _args: True,
        input_schema_validators={
            "repository-read": lambda args: isinstance(args.get("path"), str)
        },
        tool_schemas={"repository-read": input_schema},
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="summarize README")))

    assert result.status is WorkerStatus.SUCCESS
    assert result.output == "README says hello."
    assert calls == [{"path": "README.md"}]
    assert result.metadata["tool_calls"] == 1
    from loom_ai import ModelToolDefinition, ModelToolResult

    followup = provider.requests[1]
    assert followup.tools == (
        ModelToolDefinition(
            name="read-file",
            description="Read a repository file",
            input_schema=input_schema,
        ),
    )
    assert followup.tool_results == (
        ModelToolResult(
            call_id="call-1",
            tool_name="read-file",
            result={"contents": "hello"},
        ),
    )
    assert "Tool observations" not in followup.prompt
    assert any(item.get("type") == "tool-call-completed" for item in result.evidence)


def test_configured_worker_rejects_model_requested_unconfigured_tool() -> None:
    from loom_ai import ModelToolCall

    raw = raw_config()
    raw["tools"] = [{"capability_ref": "repository.read", "required": True}]
    provider = StructuredRecordingProvider(
        [
            ModelResponse(
                text="",
                provider="recording",
                model="model-a",
                kind="tool_calls",
                tool_calls=(ModelToolCall("call-1", "shell.exec", {"command": "id"}),),
            ),
        ]
    )
    calls = []
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
        capability_bindings={"repository.read": lambda args: calls.append(args)},
        approval_authorizer=lambda _config, _capability, _args: True,
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="inspect repository")))

    assert result.status is WorkerStatus.FAILED
    assert result.metadata["termination_cause"] == "unknown_tool_name"
    assert calls == []


def test_configured_worker_enforces_structured_tool_call_budget() -> None:
    from loom_ai import ModelToolCall

    raw = raw_config()
    raw["limits"]["max_attempts"] = 1
    raw["tools"] = [{"capability_ref": "repository.read", "required": True}]
    provider = StructuredRecordingProvider(
        [
            ModelResponse(
                text="",
                provider="recording",
                model="model-a",
                kind="tool_calls",
                tool_calls=(
                    ModelToolCall("call-1", "repository.read", {"path": "README.md"}),
                ),
            ),
        ]
    )
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
        capability_bindings={"repository.read": lambda _args: "contents"},
        approval_authorizer=lambda _config, _capability, _args: True,
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="inspect repository")))

    assert result.status is WorkerStatus.FAILED
    assert result.metadata["termination_cause"] == "turn_limit"
    assert result.metadata["tool_calls"] == 1


def test_configured_worker_executes_multiple_tool_calls_in_response_order() -> None:
    from loom_ai import ModelToolCall

    raw = raw_config()
    raw["tools"] = [{"capability_ref": "repository.read", "required": True}]
    provider = StructuredRecordingProvider(
        [
            ModelResponse(
                text="",
                provider="recording",
                model="model-a",
                kind="tool_calls",
                tool_calls=(
                    ModelToolCall("call-1", "repository.read", {"path": "a.txt"}),
                    ModelToolCall("call-2", "repository.read", {"path": "b.txt"}),
                ),
            ),
            ModelResponse(
                text="Both files were inspected.",
                provider="recording",
                model="model-a",
            ),
        ]
    )
    calls = []
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
        capability_bindings={
            "repository.read": lambda args: calls.append(args) or {"found": True}
        },
        approval_authorizer=lambda _config, _capability, _args: True,
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="inspect two files")))

    assert result.status is WorkerStatus.SUCCESS
    assert calls == [{"path": "a.txt"}, {"path": "b.txt"}]
    assert result.metadata["tool_calls"] == 2


def test_configured_worker_does_not_execute_tool_after_provider_cancels() -> None:
    from loom_ai import ModelToolCall

    raw = raw_config()
    raw["tools"] = [{"capability_ref": "repository.read", "required": True}]
    token = CancellationToken()
    provider = StructuredRecordingProvider(
        [
            ModelResponse(
                text="",
                provider="recording",
                model="model-a",
                kind="tool_calls",
                tool_calls=(
                    ModelToolCall("call-1", "repository.read", {"path": "README.md"}),
                ),
            ),
        ]
    )

    class CancellingProvider(StructuredRecordingProvider):
        def generate(self, request: ModelRequest) -> ModelResponse:
            response = super().generate(request)
            token.cancel("cancel before tool execution")
            return response

    provider = CancellingProvider(provider.responses)
    calls = []
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
        capability_bindings={"repository.read": lambda args: calls.append(args)},
        approval_authorizer=lambda _config, _capability, _args: True,
    )

    result = worker.execute(
        WorkerContext(
            intent=Intent(goal="inspect repository"), cancellation_token=token
        )
    )

    assert result.status is WorkerStatus.CANCELLED
    assert calls == []


def test_configured_worker_does_not_execute_tool_without_required_approval() -> None:
    from loom_ai import ModelToolCall

    raw = raw_config()
    raw["tools"] = [{"capability_ref": "repository.read", "required": True}]
    provider = StructuredRecordingProvider(
        [
            ModelResponse(
                text="",
                provider="recording",
                model="model-a",
                kind="tool_calls",
                tool_calls=(
                    ModelToolCall("call-1", "repository.read", {"path": "README.md"}),
                ),
            ),
        ]
    )
    calls = []
    worker = activate_model_worker(
        validated(raw),
        model_selections={("local-gateway", "coding"): (provider, "model-a")},
        capability_bindings={"repository.read": lambda args: calls.append(args)},
    )

    result = worker.execute(WorkerContext(intent=Intent(goal="inspect repository")))

    assert result.status is WorkerStatus.FAILED
    assert result.metadata["termination_cause"] == "tool_invocation_failed"
    assert calls == []


def test_model_request_rejects_duplicate_tool_definition_names() -> None:
    from loom_ai import ModelToolDefinition

    tool = ModelToolDefinition("repository.read", "Read a repository file")
    with pytest.raises(ValueError, match="tool names must be unique"):
        ModelRequest(
            prompt="inspect repository",
            tools=(tool, tool),
        )


def test_model_tool_result_rejects_non_json_values() -> None:
    from loom_ai import ModelToolResult

    with pytest.raises(ValueError, match="JSON-compatible"):
        ModelToolResult("call-1", "repository.read", object())
