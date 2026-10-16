"""Tests for the durable execution-state boundary."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from loom_ai.execution_state import (
    ExecutionState,
    ExecutionStateCorruptError,
    ExecutionStateError,
    FileExecutionStateStore,
)
from loom_ai.intent import Intent


def build_state(*, status: str = "interrupted") -> ExecutionState:
    return ExecutionState(
        execution_id="execution-1",
        intent=Intent(
            title="Durable execution",
            goal="resume work",
            intent_id="intent-1",
            provenance={"source": "dogfood"},
        ),
        state={"repository": "/tmp/repository", "step": "inspect"},
        evidence=({"worker": "inspect", "result": "complete"},),
        status=status,
        metadata={"attempt": 1},
    )


def test_file_store_round_trips_execution_state(tmp_path) -> None:
    store = FileExecutionStateStore(tmp_path)

    expected = replace(
        build_state(),
        result={
            "worker_id": "pipeline",
            "status": "success",
            "output": {
                "selected_result": {
                    "worker_id": "reviewer",
                    "status": "success",
                    "output": {"decision": "accept"},
                }
            },
            "evidence": [{"source": "reviewer"}],
            "error": "",
            "metadata": {"selected_worker_id": "reviewer"},
        },
    )
    store.save(expected)

    restored = store.load("execution-1")
    assert restored == expected
    assert restored is not None
    assert restored.intent.intent_id == "intent-1"
    assert restored.evidence == ({"worker": "inspect", "result": "complete"},)
    assert restored.result is not None
    assert restored.result["output"]["selected_result"]["output"] == {
        "decision": "accept"
    }


def test_missing_execution_state_is_distinct_from_corruption(tmp_path) -> None:
    store = FileExecutionStateStore(tmp_path)

    assert store.load("missing") is None

    path = next(tmp_path.glob("*.json"), None)
    assert path is None


def test_corrupt_execution_state_is_explicit(tmp_path) -> None:
    store = FileExecutionStateStore(tmp_path)
    store.save(build_state())

    path = next(tmp_path.glob("*.json"))
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ExecutionStateCorruptError):
        store.load("execution-1")


def test_execution_identity_mismatch_is_corrupt_state(tmp_path) -> None:
    store = FileExecutionStateStore(tmp_path)
    store.save(build_state())

    path = next(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["execution_id"] = "other-execution"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ExecutionStateCorruptError):
        store.load("execution-1")


def test_unsupported_status_is_rejected() -> None:
    with pytest.raises(ExecutionStateError):
        build_state(status="unknown")


def test_interruption_is_incomplete_and_not_success() -> None:
    state = build_state(status="interrupted")

    assert not state.terminal
    assert state.status != "success"


@pytest.mark.parametrize("status", ["success", "failed", "cancelled"])
def test_terminal_statuses_are_explicit(status: str) -> None:
    assert build_state(status=status).terminal


def test_lifecycle_and_evaluation_history_round_trip(tmp_path) -> None:
    store = FileExecutionStateStore(tmp_path)
    evaluation = {
        "execution_id": "execution-1",
        "criteria_id": "acceptance",
        "criteria_version": "1",
        "evaluator_id": "test-suite",
        "evaluator_version": "1",
        "status": "met",
        "evaluated_at": "2026-10-16T12:00:00+00:00",
        "evidence": [{"test": "passed"}],
        "provenance": {"source": "unit-test"},
    }
    expected = replace(
        build_state(status="success"),
        lifecycle_status="completed",
        evaluations=(evaluation,),
    )

    store.save(expected)
    restored = store.load("execution-1")

    assert restored == expected
    assert restored is not None
    assert restored.outcome_status == "success"
    assert restored.lifecycle_status == "completed"
    assert restored.latest_evaluation_status == "met"
    assert restored.evaluations == (evaluation,)


def test_legacy_snapshot_infers_lifecycle_without_evaluation(tmp_path) -> None:
    store = FileExecutionStateStore(tmp_path)
    store.save(build_state(status="success"))
    path = next(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("lifecycle_status")
    payload.pop("evaluations")
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = store.load("execution-1")

    assert restored is not None
    assert restored.status == "success"
    assert restored.lifecycle_status == "completed"
    assert restored.latest_evaluation_status == "not_evaluated"
    assert restored.evaluations == ()


def test_terminal_lifecycle_can_preserve_failed_task_outcome() -> None:
    state = replace(
        build_state(status="failed"),
        lifecycle_status="completed",
    )

    assert state.terminal
    assert state.lifecycle_status == "completed"
    assert state.outcome_status == "failed"
