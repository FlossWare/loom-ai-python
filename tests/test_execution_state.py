"""Tests for the durable execution-state boundary."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from loom_ai.execution_state import (
    ExecutionState,
    ExecutionStateConflictError,
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


def test_create_only_store_write_rejects_existing_identity_without_overwrite(
    tmp_path,
) -> None:
    store = FileExecutionStateStore(tmp_path)
    original = build_state()
    store.create(original)

    replacement = replace(
        original,
        intent=Intent(goal="replacement must not win", intent_id="intent-replacement"),
    )
    with pytest.raises(
        ExecutionStateConflictError,
        match="execution already exists: execution-1",
    ):
        store.create(replacement)

    assert store.load("execution-1") == original


def test_create_only_store_write_is_safe_across_store_instances(tmp_path) -> None:
    first_store = FileExecutionStateStore(tmp_path)
    second_store = FileExecutionStateStore(tmp_path)
    original = build_state()
    first_store.create(original)

    with pytest.raises(ExecutionStateConflictError):
        second_store.create(replace(original, state={"different": True}))

    assert second_store.load("execution-1") == original


def test_concurrent_create_only_writes_claim_identity_once(tmp_path) -> None:
    stores = [FileExecutionStateStore(tmp_path), FileExecutionStateStore(tmp_path)]
    states = [
        build_state(),
        replace(build_state(), state={"winner": "second"}),
    ]
    barrier = Barrier(2)

    def create(index: int) -> str:
        barrier.wait(timeout=2)
        try:
            stores[index].create(states[index])
        except ExecutionStateConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(create, range(2)))

    assert sorted(outcomes) == ["conflict", "created"]
    assert stores[0].load("execution-1") in states


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
