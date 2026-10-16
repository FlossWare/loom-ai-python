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
    assert result.evidence[-1]["actual_bytes"] == 80


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

    clock = iter([0.0, 0.0, 2.0])
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

    clock = iter([0.0, 0.0, 3.0, 3.0, 4.0])
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
