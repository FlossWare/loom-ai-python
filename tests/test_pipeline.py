"""Conformance tests for sequential multi-worker arbitration pipelines."""

from loom_ai.intent import Intent
from loom_ai.pipeline import (
    ArbitrationStage,
    SequentialArbitrationPipeline,
    StageEvaluation,
)
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class RecordingWorker:
    def __init__(
        self,
        worker_id: str,
        *,
        output: object = None,
        evidence: tuple[dict, ...] = (),
    ) -> None:
        self.worker_id = worker_id
        self.output = output if output is not None else {"worker": worker_id}
        self.evidence = evidence
        self.contexts: list[WorkerContext] = []
        self.calls = 0

    def execute(self, context: WorkerContext) -> WorkerResult:
        self.calls += 1
        self.contexts.append(context)
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output=self.output,
            evidence=self.evidence,
        )


class NestedMutatingWorker(RecordingWorker):
    def execute(self, context: WorkerContext) -> WorkerResult:
        context.state["files"]["src/example.py"] = "mutated by first worker"
        return super().execute(context)


def select_first(
    results: tuple[WorkerResult, ...], _context: WorkerContext
) -> StageEvaluation:
    return StageEvaluation(results[0], reason="selected first candidate")


def test_stage_workers_receive_the_same_logical_input() -> None:
    intent = Intent(goal="review one artifact", intent_id="pipeline-input")
    context = WorkerContext(
        intent=intent,
        state={"workspace": "/workspace/repo", "revision": "abc123"},
    )
    first = RecordingWorker("first")
    second = RecordingWorker("second")
    stage = ArbitrationStage("review", (first, second), select_first)

    result = SequentialArbitrationPipeline((stage,)).execute(context)

    assert result.successful
    assert first.contexts[0].intent is intent
    assert second.contexts[0].intent is intent
    assert dict(first.contexts[0].state) == dict(second.contexts[0].state)
    assert first.contexts[0].state is not second.contexts[0].state


def test_nested_mutation_by_one_worker_does_not_change_sibling_input() -> None:
    context = WorkerContext(
        intent=Intent(goal="isolate workspace snapshots"),
        state={"files": {"src/example.py": "original"}},
    )
    first = NestedMutatingWorker("mutator")
    second = RecordingWorker("observer")
    stage = ArbitrationStage("review", (first, second), select_first)

    SequentialArbitrationPipeline((stage,)).execute(context)

    assert first.contexts[0].state["files"]["src/example.py"] == (
        "mutated by first worker"
    )
    assert second.contexts[0].state["files"]["src/example.py"] == "original"
    assert context.state["files"]["src/example.py"] == "original"


def test_arbiter_receives_all_worker_results_and_selects_a_real_result() -> None:
    first = RecordingWorker("first", output={"candidate": 1})
    second = RecordingWorker("second", output={"candidate": 2})
    observed: list[tuple[WorkerResult, ...]] = []

    def evaluate(
        results: tuple[WorkerResult, ...], _context: WorkerContext
    ) -> StageEvaluation:
        observed.append(results)
        return StageEvaluation(results[1], reason="second has stronger evidence")

    pipeline = SequentialArbitrationPipeline(
        (ArbitrationStage("adjudicate", (first, second), evaluate),)
    )
    result = pipeline.execute(WorkerContext(intent=Intent(goal="choose a result")))

    assert len(observed) == 1
    assert [item.worker_id for item in observed[0]] == ["first", "second"]
    assert result.output["selected_result"].output == {"candidate": 2}
    assert result.output["stages"][0]["worker_results"][0].output == {"candidate": 1}
    assert result.metadata["selected_worker_id"] == "second"


def test_selected_result_is_available_to_the_next_stage() -> None:
    first = RecordingWorker("candidate-a", output={"patch": "A"})
    selected = RecordingWorker("candidate-b", output={"patch": "B"})
    next_worker = RecordingWorker("independent-review")
    first_stage = ArbitrationStage(
        "solve",
        (first, selected),
        lambda results, _context: StageEvaluation(results[1]),
    )
    second_stage = ArbitrationStage(
        "review",
        (next_worker,),
        lambda results, _context: StageEvaluation(results[0]),
    )

    pipeline = SequentialArbitrationPipeline((first_stage, second_stage))
    result = pipeline.execute(
        WorkerContext(intent=Intent(goal="solve and independently review"))
    )

    handoff = next_worker.contexts[0].state["previous_result"]
    assert isinstance(handoff, WorkerResult)
    assert handoff.worker_id == "candidate-b"
    assert handoff.output == {"patch": "B"}
    assert result.metadata["stages"][1]["stage_id"] == "review"
    assert result.output["stages"][0]["worker_results"][0].worker_id == "candidate-a"


def test_workspace_and_evidence_survive_stage_boundaries() -> None:
    initial_evidence = {"source": "caller", "revision": "abc123"}
    worker_evidence = {"source": "worker", "path": "src/example.py"}
    first = RecordingWorker(
        "implementation",
        output={"changed": "src/example.py"},
        evidence=(worker_evidence,),
    )
    second = RecordingWorker("reviewer")
    context = WorkerContext(
        intent=Intent(goal="modify and review"),
        state={
            "workspace": "/workspace/repo",
            "files": {"src/example.py": "original content"},
            "repository_revision": "abc123",
        },
        evidence=(initial_evidence,),
    )
    pipeline = SequentialArbitrationPipeline(
        (
            ArbitrationStage("solve", (first,), select_first),
            ArbitrationStage("review", (second,), select_first),
        )
    )

    pipeline.execute(context)

    received = second.contexts[0]
    assert received.state["workspace"] == "/workspace/repo"
    assert received.state["files"] == {"src/example.py": "original content"}
    assert received.state["repository_revision"] == "abc123"
    assert initial_evidence in received.evidence
    assert worker_evidence in received.evidence
    assert received.state["previous_stage_results"][0].worker_id == "implementation"


def test_pipeline_runs_every_configured_stage_without_confidence_termination() -> None:
    workers = [RecordingWorker(f"stage-{index}") for index in range(3)]
    stages = [
        ArbitrationStage(
            f"stage-{index}",
            (worker,),
            lambda results, _context: StageEvaluation(
                results[0], reason="continue regardless of confidence"
            ),
        )
        for index, worker in enumerate(workers)
    ]

    result = SequentialArbitrationPipeline(stages).execute(
        WorkerContext(intent=Intent(goal="complete configured stages"))
    )

    assert [worker.calls for worker in workers] == [1, 1, 1]
    assert [stage["stage_id"] for stage in result.metadata["stages"]] == [
        "stage-0",
        "stage-1",
        "stage-2",
    ]


def test_single_stage_pipeline_remains_supported() -> None:
    worker = RecordingWorker("single-stage", output={"answer": 42})
    pipeline = SequentialArbitrationPipeline(
        (ArbitrationStage("solve", (worker,), select_first),)
    )

    result = pipeline.execute(WorkerContext(intent=Intent(goal="solve once")))

    assert result.status is WorkerStatus.SUCCESS
    assert result.output["selected_result"].output == {"answer": 42}
    assert len(result.metadata["stages"]) == 1
