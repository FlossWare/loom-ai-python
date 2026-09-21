"""Tests for the durable execution-state boundary."""

from __future__ import annotations

import json

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

    store.save(build_state())

    restored = store.load("execution-1")
    assert restored == build_state()
    assert restored is not None
    assert restored.intent.intent_id == "intent-1"
    assert restored.evidence == ({"worker": "inspect", "result": "complete"},)


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
