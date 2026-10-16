"""Tests for the Orchestrator application service."""

from pathlib import Path

import pytest

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.execution_state import FileExecutionStateStore
from loom_ai.intent import Intent
from loom_ai.orchestrator import Orchestrator
from loom_ai.worker import Worker, WorkerContext, WorkerResult, WorkerStatus


class PhaseWorker:
    worker_id = "phase-worker"

    def execute(self, context: WorkerContext) -> WorkerResult:
        phase = "continued" if context.state.get("phase") == "initial" else "initial"
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={"phase": phase},
            evidence=({"worker": self.worker_id, "phase": phase},),
        )


class CancelledWorker:
    worker_id = "cancelled-worker"

    def execute(self, _context: WorkerContext) -> WorkerResult:
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.CANCELLED,
            error="cancelled by caller",
        )


class RaisingWorker:
    worker_id = "raising-worker"

    def execute(self, _context: WorkerContext) -> WorkerResult:
        raise ValueError("private implementation detail")


def complete(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
    return WorkerEvaluation(ArbiterDecision.COMPLETE, reason=result.error)


def make_orchestrator(state_dir: Path, worker: Worker) -> Orchestrator:
    return Orchestrator(
        Arbiter([worker], complete, max_retries=0),
        execution_store=FileExecutionStateStore(state_dir),
    )


def test_submit_persists_execution_identity_state_and_evidence(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path / "state", PhaseWorker())
    intent = Intent(goal="run a durable task", provenance={"source": "test"})

    submission = orchestrator.submit(intent, execution_id="execution-123")

    assert submission.execution_id == "execution-123"
    assert submission.result.status is WorkerStatus.SUCCESS
    state = orchestrator.observe("execution-123")
    assert state.execution_id == "execution-123"
    assert state.intent.intent_id == intent.intent_id
    assert state.intent.provenance == {"source": "test"}
    assert state.status == "success"
    assert state.state["phase"] == "initial"
    assert any(
        item.get("type") == "execution-phase" and item.get("phase") == "initial"
        for item in state.evidence
    )


def test_continue_preserves_execution_identity_and_phase_history(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path / "state", PhaseWorker())
    initial = orchestrator.submit(
        Intent(goal="run then continue"),
        execution_id="execution-456",
    )

    continued = orchestrator.continue_execution("execution-456")
    state = orchestrator.observe("execution-456")

    assert initial.execution_id == continued.execution_id == "execution-456"
    assert continued.result.output[0].output == {"phase": "continued"}
    assert state.status == "success"
    assert state.state["phase"] == "continued"
    phases = [
        item["phase"]
        for item in state.evidence
        if item.get("type") == "execution-phase"
    ]
    assert phases == ["initial", "continued"]


def test_cancelled_execution_remains_cancelled_in_durable_state(
    tmp_path: Path,
) -> None:
    orchestrator = make_orchestrator(tmp_path / "state", CancelledWorker())

    submission = orchestrator.submit(
        Intent(goal="cancel explicitly"),
        execution_id="execution-cancelled",
    )

    state = orchestrator.observe("execution-cancelled")
    assert submission.result.status is WorkerStatus.CANCELLED
    assert state.status == "cancelled"
    assert state.state["last_status"] == "cancelled"


def test_unexpected_worker_exception_is_persisted_as_failure(
    tmp_path: Path,
) -> None:
    orchestrator = make_orchestrator(tmp_path / "state", RaisingWorker())

    with pytest.raises(ValueError, match="private implementation detail"):
        orchestrator.submit(
            Intent(goal="persist an execution failure"),
            execution_id="execution-failed",
        )

    state = orchestrator.observe("execution-failed")
    assert state.status == "failed"
    assert state.metadata["worker_id"] == "orchestrator"
    assert any(
        item.get("type") == "execution-exception"
        and item.get("exception_type") == "ValueError"
        for item in state.evidence
    )
