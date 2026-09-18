from dataclasses import dataclass

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.intent import Intent
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


@dataclass
class StubWorker:
    worker_id: str
    status: WorkerStatus = WorkerStatus.SUCCESS

    def execute(self, context: WorkerContext) -> WorkerResult:
        return WorkerResult(
            worker_id=self.worker_id,
            status=self.status,
            output={"worker": self.worker_id},
            evidence=(
                {"worker": self.worker_id, "intent_id": context.intent.intent_id},
            ),
        )


def test_arbiter_is_a_worker_and_composes_workers() -> None:
    intent = Intent(goal="compose workers", intent_id="intent-1")
    context = WorkerContext(intent=intent)
    first = StubWorker("first")
    second = StubWorker("second")

    def evaluate(result, _context):
        if result.worker_id == "second":
            return WorkerEvaluation(ArbiterDecision.COMPLETE)
        return WorkerEvaluation(ArbiterDecision.CONTINUE)

    arbiter = Arbiter([first, second], evaluate, worker_id="root")
    result = arbiter.execute(context)

    assert result.successful
    assert [item.worker_id for item in result.output] == ["first", "second"]
    assert result.metadata["arbiter_id"] == "root"


def test_nested_arbiter_works_as_worker() -> None:
    intent = Intent(goal="nested execution", intent_id="intent-2")
    context = WorkerContext(intent=intent)
    leaf = StubWorker("leaf")

    child = Arbiter(
        [leaf],
        lambda _result, _context: WorkerEvaluation(ArbiterDecision.COMPLETE),
        worker_id="child",
    )
    root = Arbiter(
        [child],
        lambda _result, _context: WorkerEvaluation(ArbiterDecision.COMPLETE),
        worker_id="root",
    )

    result = root.execute(context)

    assert result.successful
    assert result.output[0].worker_id == "child"
    assert result.output[0].output[0].worker_id == "leaf"
