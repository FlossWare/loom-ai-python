"""Sequential multi-worker arbitration pipeline realization.

The normative semantics live in FlossWare/loom-ai. This module implements
configured stages without defining a new language-neutral contract.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Iterable

from loom_ai.worker import Worker, WorkerContext, WorkerResult, WorkerStatus


@dataclass(frozen=True)
class StageEvaluation:
    """The result selected by an Arbiter after reviewing a stage's results."""

    selected_result: WorkerResult
    reason: str = ""


StageEvaluator = Callable[
    [tuple[WorkerResult, ...], WorkerContext],
    StageEvaluation,
]


@dataclass(frozen=True)
class ArbitrationStage:
    """Independent Workers and the Arbiter policy for one pipeline stage."""

    stage_id: str
    workers: Iterable[Worker]
    evaluator: StageEvaluator

    def __post_init__(self) -> None:
        if not self.stage_id.strip():
            raise ValueError("stage_id must not be empty")
        workers = tuple(self.workers)
        if not workers:
            raise ValueError("an arbitration stage must have at least one Worker")
        worker_ids = [worker.worker_id for worker in workers]
        if len(worker_ids) != len(set(worker_ids)):
            raise ValueError("Worker identifiers must be unique within a stage")
        object.__setattr__(self, "workers", workers)


@dataclass(frozen=True)
class SequentialArbitrationPipeline:
    """A Worker that executes configured arbitration stages in sequence."""

    stages: Iterable[ArbitrationStage]
    worker_id: str = "pipeline"

    def __post_init__(self) -> None:
        stages = tuple(self.stages)
        if not stages:
            raise ValueError("a pipeline must contain at least one stage")
        stage_ids = [stage.stage_id for stage in stages]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("stage identifiers must be unique within a pipeline")
        if not self.worker_id.strip():
            raise ValueError("worker_id must not be empty")
        object.__setattr__(self, "stages", stages)

    def execute(self, context: WorkerContext) -> WorkerResult:
        """Execute every configured stage unless a selected result is cancelled."""
        stage_context = context
        history: list[dict] = []
        evidence: list[dict] = list(context.evidence)
        selected: WorkerResult | None = None

        for index, stage in enumerate(self.stages):
            # Give every Worker an isolated context snapshot. Deep-copying the
            # mappings prevents nested mutable state from leaking between peers.
            worker_results = tuple(
                worker.execute(self._snapshot(stage_context))
                for worker in stage.workers
            )
            evaluation = stage.evaluator(worker_results, self._snapshot(stage_context))
            selected = evaluation.selected_result
            if not any(selected is result for result in worker_results):
                raise ValueError(
                    "stage evaluator must select one of the stage's Worker Results"
                )

            for result in worker_results:
                evidence.extend(dict(item) for item in result.evidence)

            stage_summary = {
                "type": "arbitration-stage",
                "stage_id": stage.stage_id,
                "worker_ids": [result.worker_id for result in worker_results],
                "selected_worker_id": selected.worker_id,
                "selected_status": selected.status.value,
                "reason": evaluation.reason,
            }
            evidence.append(stage_summary)
            history.append(
                {
                    "stage_id": stage.stage_id,
                    "worker_results": worker_results,
                    "selected_result": selected,
                    "reason": evaluation.reason,
                }
            )

            if selected.status is WorkerStatus.CANCELLED:
                break

            if index + 1 < len(self.stages):
                next_stage = self.stages[index + 1]
                stage_context = stage_context.with_state(
                    previous_result=selected,
                    previous_stage_results=worker_results,
                    pipeline_history=tuple(history),
                    pipeline_stage_index=index + 1,
                    pipeline_stage_id=next_stage.stage_id,
                ).with_evidence(
                    *(
                        [
                            dict(item)
                            for result in worker_results
                            for item in result.evidence
                        ]
                        + [stage_summary]
                    )
                )

        assert selected is not None
        summaries = [
            {
                "stage_id": record["stage_id"],
                "worker_ids": [result.worker_id for result in record["worker_results"]],
                "selected_worker_id": record["selected_result"].worker_id,
                "selected_status": record["selected_result"].status.value,
                "reason": record["reason"],
            }
            for record in history
        ]
        output = {
            "selected_result": selected,
            "stages": [
                {
                    "stage_id": record["stage_id"],
                    "worker_results": record["worker_results"],
                    "selected_result": record["selected_result"],
                    "reason": record["reason"],
                }
                for record in history
            ],
        }
        return WorkerResult(
            worker_id=self.worker_id,
            status=selected.status,
            output=output,
            evidence=tuple(evidence),
            error=selected.error
            if selected.status in (WorkerStatus.FAILED, WorkerStatus.CANCELLED)
            else "",
            metadata={
                "pipeline_id": self.worker_id,
                "selected_worker_id": selected.worker_id,
                "stages": summaries,
            },
        )

    @staticmethod
    def _snapshot(context: WorkerContext) -> WorkerContext:
        """Copy context state and metadata for an isolated Worker invocation."""
        return WorkerContext(
            intent=context.intent,
            state=deepcopy(dict(context.state)),
            evidence=tuple(deepcopy(dict(item)) for item in context.evidence),
            metadata=deepcopy(dict(context.metadata)),
        )
