"""Conformance tests for execution lifecycle, results, and evaluation separation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Thread
from urllib.request import Request, urlopen

import pytest

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.evaluation import EvaluationRecord, EvaluationStatus
from loom_ai.execution_state import ExecutionState, FileExecutionStateStore
from loom_ai.intent import Intent
from loom_ai.orchestrator import Orchestrator
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class SuccessfulWorker:
    worker_id = "successful-worker"

    def execute(self, _context: WorkerContext) -> WorkerResult:
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={"changed": True},
        )


def build_orchestrator(root) -> Orchestrator:
    def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
        return WorkerEvaluation(ArbiterDecision.COMPLETE, reason="worker completed")

    return Orchestrator(
        Arbiter([SuccessfulWorker()], evaluate, max_retries=0),
        execution_store=FileExecutionStateStore(root),
    )


def test_completed_execution_defaults_to_not_evaluated(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    submission = orchestrator.submit(
        Intent(goal="complete without an acceptance assessment"),
        execution_id="not-evaluated",
    )

    observed = orchestrator.observe("not-evaluated")

    assert submission.result.status is WorkerStatus.SUCCESS
    assert observed.status == "success"  # Backward-compatible field.
    assert observed.lifecycle == "completed"
    assert observed.result is not None
    assert observed.result["status"] == "success"
    assert observed.evaluation.status is EvaluationStatus.NOT_EVALUATED
    assert observed.evaluations == ()


@pytest.mark.parametrize(
    "status",
    [
        EvaluationStatus.MET,
        EvaluationStatus.PARTIALLY_MET,
        EvaluationStatus.NOT_MET,
        EvaluationStatus.INCONCLUSIVE,
    ],
)
def test_evaluation_is_appended_without_rewriting_result_or_lifecycle(
    tmp_path, status: EvaluationStatus
) -> None:
    orchestrator = build_orchestrator(tmp_path)
    intent = Intent(goal="evaluate criteria", acceptance=("artifact exists",))
    orchestrator.submit(intent, execution_id="evaluated")
    before = orchestrator.observe("evaluated")

    updated = orchestrator.record_evaluation(
        "evaluated",
        EvaluationRecord(
            status=status,
            criteria=("artifact exists",),
            criteria_version="1",
            evaluator="test-suite",
            evaluator_version="0.1",
            evidence=({"check": "artifact-exists", "passed": status is EvaluationStatus.MET},),
            provenance={"source": "regression-test"},
        ),
    )

    assert updated.lifecycle == before.lifecycle == "completed"
    assert updated.status == before.status == "success"
    assert updated.result == before.result
    assert updated.evaluation.status is status
    assert updated.evaluation.execution_id == "evaluated"
    assert updated.evaluation.intent_id == intent.intent_id
    assert updated.evaluation.evaluated_at is not None
    assert updated.evaluation.criteria == ("artifact exists",)

    second = orchestrator.record_evaluation(
        "evaluated", EvaluationRecord(status=EvaluationStatus.INCONCLUSIVE)
    )
    assert len(second.evaluations) == 2
    assert second.evaluations[0].status is status
    assert second.evaluation.status is EvaluationStatus.INCONCLUSIVE


def test_evaluation_rejects_wrong_execution_identity(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    orchestrator.submit(Intent(goal="identity check"), execution_id="execution-a")

    with pytest.raises(ValueError, match="execution_id"):
        orchestrator.record_evaluation(
            "execution-a",
            EvaluationRecord(
                status=EvaluationStatus.MET,
                execution_id="execution-b",
            ),
        )


def test_v1_snapshot_migrates_to_separate_lifecycle_and_evaluation(tmp_path) -> None:
    store = FileExecutionStateStore(tmp_path)
    store.save(
        ExecutionState(
            execution_id="legacy",
            intent=Intent(goal="legacy snapshot"),
            status="success",
        )
    )
    path = next(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["representation_version"] = 1
    payload.pop("lifecycle", None)
    payload.pop("evaluations", None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = store.load("legacy")

    assert restored is not None
    assert restored.representation_version == 2
    assert restored.status == "success"
    assert restored.lifecycle == "completed"
    assert restored.evaluation.status is EvaluationStatus.NOT_EVALUATED
    store.save(restored)
    assert json.loads(path.read_text(encoding="utf-8"))["representation_version"] == 2


def test_http_exposes_and_records_evaluation_without_changing_lifecycle(tmp_path) -> None:
    server = LoomServer(orchestrator=build_orchestrator(tmp_path), port=0)
    instance = server.start()
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.host}:{server.port}"
    try:
        request = Request(
            f"{base_url}/intents",
            data=json.dumps(
                {
                    "goal": "execute then evaluate",
                    "execution_id": "http-evaluation",
                    "acceptance": ["artifact exists"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            submitted = json.load(response)
        assert submitted["status"] == "success"
        assert submitted["lifecycle"] == "completed"
        assert submitted["evaluation"]["status"] == "not_evaluated"

        evaluation_request = Request(
            f"{base_url}/executions/http-evaluation/evaluations",
            data=json.dumps(
                {
                    "status": "met",
                    "criteria": ["artifact exists"],
                    "evaluator": "http-test",
                    "evidence": [{"check": "artifact-exists", "passed": True}],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(evaluation_request) as response:
            evaluated = json.load(response)
        assert evaluated["status"] == "success"
        assert evaluated["lifecycle"] == "completed"
        assert evaluated["evaluation"]["status"] == "met"
        assert len(evaluated["evaluations"]) == 1
        assert evaluated["result"]["status"] == "success"
    finally:
        server.close()
        thread.join(timeout=2)
