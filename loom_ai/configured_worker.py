"""Activate a model Worker from validated declarative configuration."""

from __future__ import annotations

from dataclasses import replace
from time import monotonic
from typing import Mapping

from loom_ai.model import ModelProvider
from loom_ai.model_worker import ModelWorker
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus
from loom_ai.worker_configuration import ValidatedWorkerConfiguration


class WorkerActivationError(ValueError):
    """Raised when a validated config cannot resolve to a runtime provider."""


class ConfiguredModelWorker:
    """Model Worker activated from a validated config and trusted selection map.

    Selection keys are (gateway_ref, selection_ref) pairs. The map is supplied
    by trusted runtime wiring and contains no credentials in the config itself.
    Failed model calls may be retried up to max_attempts. Duration is checked
    before and after each synchronous provider call; it cannot interrupt a call
    that blocks inside a provider implementation.
    """

    def __init__(
        self,
        configuration: ValidatedWorkerConfiguration,
        provider: ModelProvider,
        model: str,
    ) -> None:
        if not model or not model.strip():
            raise WorkerActivationError("resolved model name must not be empty")
        self.configuration = configuration
        self._worker = ModelWorker(
            provider,
            model=model,
            worker_id=configuration.worker_id,
            timeout_seconds=float(configuration.limits.max_duration_seconds),
        )

    @property
    def worker_id(self) -> str:
        return self.configuration.worker_id

    def execute(self, context: WorkerContext) -> WorkerResult:
        started = monotonic()
        instruction_block = (
            f"Worker purpose: {self.configuration.purpose}\n"
            "Worker instructions:\n"
            + "\n".join(
                f"- {instruction}" for instruction in self.configuration.instructions
            )
            + "\n\n"
        )
        configured_intent = replace(
            context.intent,
            goal=instruction_block + context.intent.goal,
        )
        configured_context = replace(context, intent=configured_intent)
        evidence: list[Mapping[str, object]] = []
        last_result: WorkerResult | None = None

        for attempt in range(1, self.configuration.limits.max_attempts + 1):
            cause = configured_context.termination_cause
            elapsed = monotonic() - started
            if cause is not None:
                return _failure(
                    self.worker_id,
                    "execution cancelled"
                    if cause == "cancelled"
                    else "deadline exceeded",
                    tuple(evidence)
                    + ({"type": "configured-worker-termination", "cause": cause},),
                    {"termination_cause": cause, "attempts": attempt - 1},
                    status=WorkerStatus.CANCELLED
                    if cause == "cancelled"
                    else WorkerStatus.FAILED,
                )
            if elapsed >= self.configuration.limits.max_duration_seconds:
                return _failure(
                    self.worker_id,
                    "configured duration limit exceeded",
                    tuple(evidence) + ({"type": "configured-duration-limit"},),
                    {"termination_cause": "duration_limit", "attempts": attempt - 1},
                )

            result = self._worker.execute(configured_context)
            last_result = result
            evidence.extend(result.evidence)
            evidence.append(
                {
                    "type": "configured-worker-attempt",
                    "attempt": attempt,
                    "status": result.status.value,
                }
            )

            elapsed = monotonic() - started
            if elapsed > self.configuration.limits.max_duration_seconds:
                return _failure(
                    self.worker_id,
                    "configured duration limit exceeded",
                    tuple(evidence) + ({"type": "configured-duration-limit"},),
                    {
                        "termination_cause": "duration_limit",
                        "attempts": attempt,
                        "elapsed_seconds": elapsed,
                    },
                )

            if result.status is WorkerStatus.CANCELLED:
                return replace(result, evidence=tuple(evidence))
            if result.status is WorkerStatus.SUCCESS:
                output = result.output
                if not isinstance(output, str):
                    return _failure(
                        self.worker_id,
                        "configured output must be text",
                        tuple(evidence) + ({"type": "configured-output-rejected"},),
                        {"termination_cause": "output_type", "attempts": attempt},
                    )
                output_bytes = len(output.encode("utf-8"))
                if output_bytes > self.configuration.limits.max_output_bytes:
                    return _failure(
                        self.worker_id,
                        "configured output limit exceeded",
                        tuple(evidence)
                        + (
                            {
                                "type": "configured-output-limit",
                                "limit_bytes": self.configuration.limits.max_output_bytes,
                                "actual_bytes": output_bytes,
                            },
                        ),
                        {
                            "termination_cause": "output_limit",
                            "attempts": attempt,
                            "output_bytes": output_bytes,
                        },
                    )
                return replace(
                    result,
                    evidence=tuple(evidence),
                    metadata={
                        **result.metadata,
                        "configured_worker_id": self.worker_id,
                        "attempts": attempt,
                        "elapsed_seconds": elapsed,
                        "output_bytes": output_bytes,
                    },
                )

            if attempt < self.configuration.limits.max_attempts:
                continue

        assert last_result is not None
        return replace(
            last_result,
            evidence=tuple(evidence),
            metadata={
                **last_result.metadata,
                "configured_worker_id": self.worker_id,
                "attempts": self.configuration.limits.max_attempts,
                "elapsed_seconds": monotonic() - started,
            },
        )


def activate_model_worker(
    configuration: ValidatedWorkerConfiguration,
    *,
    model_selections: Mapping[tuple[str, str], tuple[ModelProvider, str]],
) -> ConfiguredModelWorker:
    """Resolve the config's logical selection using a trusted runtime registry."""
    key = (configuration.gateway_ref, configuration.selection_ref)
    try:
        provider, model = model_selections[key]
    except KeyError as exc:
        raise WorkerActivationError(
            "configured model gateway/selection has no runtime binding"
        ) from exc
    if not isinstance(provider.provider_id, str) or not provider.provider_id.strip():
        raise WorkerActivationError("resolved provider has no stable provider_id")
    return ConfiguredModelWorker(configuration, provider, model)


def _failure(
    worker_id: str,
    error: str,
    evidence: tuple[Mapping[str, object], ...],
    metadata: dict[str, object],
    *,
    status: WorkerStatus = WorkerStatus.FAILED,
) -> WorkerResult:
    return WorkerResult(
        worker_id=worker_id,
        status=status,
        error=error,
        evidence=evidence,
        metadata=metadata,
    )
