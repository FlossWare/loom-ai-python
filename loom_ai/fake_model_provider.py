"""Deterministic reference ModelProvider for tests and dogfooding."""

from __future__ import annotations

from loom_ai.model import ModelRequest, ModelResponse


class FakeModelProvider:
    """Deterministically echoes model invocation without external services."""

    provider_id = "fake"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text=f"fake response: {request.prompt}",
            provider=self.provider_id,
            model=request.model,
            metadata={"implementation": "deterministic-fake"},
            provenance={
                "provider": self.provider_id,
                "model": request.model,
            },
        )
