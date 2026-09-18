"""Worker implementation that consumes the provider-neutral model boundary."""

from __future__ import annotations

from loom_ai.model import ModelProvider, ModelRequest
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class ModelWorker:
    """Adapt an Intent to a provider-neutral model invocation."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        model: str = "default",
        worker_id: str = "model",
    ) -> None:
        self.provider = provider
        self.model = model
        self._worker_id = worker_id

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def execute(self, context: WorkerContext) -> WorkerResult:
        intent = context.intent
        prompt = intent.goal
        if intent.requirements:
            prompt += "\nRequirements:\n" + "\n".join(
                f"- {item}" for item in intent.requirements
            )
        if intent.constraints:
            prompt += "\nConstraints:\n" + "\n".join(
                f"- {item}" for item in intent.constraints
            )

        try:
            response = self.provider.generate(
                ModelRequest(
                    prompt=prompt,
                    model=self.model,
                    metadata={"intent_id": intent.intent_id},
                )
            )
            if response.provider != self.provider.provider_id:
                raise ValueError("model provider provenance mismatch")
        except Exception:
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.FAILED,
                error="model provider invocation failed",
                metadata={
                    "provider": self.provider.provider_id,
                    "model": self.model,
                },
            )

        evidence = (
            {
                "type": "model-response",
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "metadata": dict(response.metadata),
                "provenance": dict(response.provenance),
            },
        )
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output=response.text,
            evidence=evidence,
            metadata={
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "provenance": dict(response.provenance),
            },
        )
