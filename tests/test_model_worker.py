"""Tests for the model-backed Worker."""

from __future__ import annotations

from dataclasses import dataclass

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.fake_model_provider import FakeModelProvider
from loom_ai.intent import Intent
from loom_ai.model import ModelRequest, ModelResponse
from loom_ai.model_worker import ModelWorker
from loom_ai.worker import WorkerContext, WorkerStatus


@dataclass
class RecordingProvider:
    provider_id: str = "recording"
    response_provider: str = "recording"
    response_model: str = "test-model"
    received: ModelRequest | None = None

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.received = request
        return ModelResponse(
            text="recorded result",
            provider=self.response_provider,
            model=self.response_model,
            finish_reason="stop",
            metadata={"implementation": "recording"},
            provenance={
                "provider": self.response_provider,
                "model": self.response_model,
            },
        )


class FailingProvider:
    provider_id = "failing"

    def generate(self, request: ModelRequest) -> ModelResponse:
        raise RuntimeError("https://provider.example/request?api_key=secret-value")


class MismatchedProvider:
    provider_id = "declared"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="result",
            provider="actual",
            model="test-model",
        )


def test_model_worker_uses_provider_without_vendor_knowledge() -> None:
    worker = ModelWorker(FakeModelProvider(), model="test-model")
    context = WorkerContext(
        intent=Intent(
            goal="summarize the result",
            requirements=("be concise",),
            constraints=("do not expose credentials",),
        )
    )

    result = worker.execute(context)

    assert result.status is WorkerStatus.SUCCESS
    assert result.output.startswith("fake response: summarize the result")
    assert result.metadata["provider"] == "fake"
    assert result.metadata["model"] == "test-model"
    assert result.evidence[0]["type"] == "model-response"
    assert result.evidence[0]["finish_reason"] == "stop"
    assert result.evidence[0]["metadata"]["implementation"] == "deterministic-fake"


def test_model_worker_sends_requirements_and_constraints_to_provider() -> None:
    provider = RecordingProvider()
    worker = ModelWorker(provider, model="test-model")
    intent = Intent(
        goal="summarize the result",
        requirements=("be concise", "preserve facts"),
        constraints=("no credentials",),
    )

    worker.execute(WorkerContext(intent=intent))

    assert provider.received is not None
    assert "summarize the result" in provider.received.prompt
    assert "- be concise" in provider.received.prompt
    assert "- preserve facts" in provider.received.prompt
    assert "- no credentials" in provider.received.prompt
    assert provider.received.metadata["intent_id"] == intent.intent_id


def test_model_worker_returns_safe_failure_without_provider_error_text() -> None:
    result = ModelWorker(FailingProvider(), model="test-model").execute(
        WorkerContext(intent=Intent(goal="exercise failure handling"))
    )

    assert result.status is WorkerStatus.FAILED
    assert result.error == "model provider invocation failed"
    assert "api_key" not in str(result.error).lower()
    assert result.metadata["provider"] == "failing"
    assert result.metadata["model"] == "test-model"


def test_model_worker_rejects_provider_provenance_mismatch() -> None:
    result = ModelWorker(MismatchedProvider(), model="test-model").execute(
        WorkerContext(intent=Intent(goal="exercise provenance consistency"))
    )

    assert result.status is WorkerStatus.FAILED
    assert result.error == "model provider invocation failed"


def test_model_worker_preserves_response_metadata_and_provenance_in_evidence() -> None:
    provider = RecordingProvider()
    result = ModelWorker(provider, model="test-model").execute(
        WorkerContext(intent=Intent(goal="preserve evidence"))
    )

    evidence = result.evidence[0]
    assert evidence["metadata"] == {"implementation": "recording"}
    assert evidence["provenance"] == {
        "provider": "recording",
        "model": "test-model",
    }
    assert result.metadata["finish_reason"] == "stop"


def test_model_worker_composes_through_arbiter() -> None:
    worker = ModelWorker(FakeModelProvider(), model="test-model")
    intent = Intent(goal="exercise Stage 5")

    result = Arbiter(
        [worker],
        lambda worker_result, _context: WorkerEvaluation(
            ArbiterDecision.COMPLETE
            if worker_result.successful
            else ArbiterDecision.REPLAN
        ),
        max_retries=0,
    ).execute(WorkerContext(intent=intent))

    assert result.successful
    assert result.output[0].output == "fake response: exercise Stage 5"
