"""Tests for the durable fleet snapshot storage port."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from loom_ai.fleet_state import (
    FileFleetStateStore,
    FleetStateConflictError,
    FleetStateCorruptError,
    FleetStateError,
)


def test_file_store_round_trips_snapshot(tmp_path) -> None:
    store = FileFleetStateStore(tmp_path)
    snapshot = {
        "tasks": [{"task_id": "task-1", "status": "queued"}],
        "workers": [{"worker_id": "worker-1", "lifecycle": "ready"}],
        "generation": 4,
    }

    store.save("coordinator-a", snapshot)

    assert store.load("coordinator-a") == snapshot


def test_create_only_store_write_does_not_overwrite(tmp_path) -> None:
    store = FileFleetStateStore(tmp_path)
    original = {"tasks": [], "workers": []}
    store.create("coordinator-a", original)

    with pytest.raises(FleetStateConflictError):
        store.create("coordinator-a", {"tasks": [{"task_id": "different"}]})

    assert store.load("coordinator-a") == original


def test_concurrent_create_only_writes_claim_identity_once(tmp_path) -> None:
    stores = [FileFleetStateStore(tmp_path), FileFleetStateStore(tmp_path)]
    snapshots = [{"winner": "first"}, {"winner": "second"}]
    barrier = Barrier(2)

    def create(index: int) -> str:
        barrier.wait(timeout=2)
        try:
            stores[index].create("coordinator-a", snapshots[index])
        except FleetStateConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(create, range(2)))

    assert sorted(outcomes) == ["conflict", "created"]
    assert stores[0].load("coordinator-a") in snapshots


def test_concurrent_saves_never_publish_partial_snapshot(tmp_path) -> None:
    stores = [FileFleetStateStore(tmp_path), FileFleetStateStore(tmp_path)]
    snapshots = [
        {"writer": "first", "payload": "a" * 4096},
        {"writer": "second", "payload": "b" * 8192},
    ]
    barrier = Barrier(2)

    def save(index: int) -> None:
        barrier.wait(timeout=2)
        stores[index].save("coordinator-a", snapshots[index])

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(save, range(2)))

    assert stores[0].load("coordinator-a") in snapshots


def test_missing_snapshot_is_distinct_from_corruption(tmp_path) -> None:
    store = FileFleetStateStore(tmp_path)

    assert store.load("missing") is None
    assert list(tmp_path.iterdir()) == []


def test_corrupt_snapshot_is_explicit(tmp_path) -> None:
    store = FileFleetStateStore(tmp_path)
    store.save("coordinator-a", {"tasks": []})
    path = next(tmp_path.glob("*.json"))
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(FleetStateCorruptError):
        store.load("coordinator-a")


def test_unsupported_version_is_rejected(tmp_path) -> None:
    store = FileFleetStateStore(tmp_path)
    store.save("coordinator-a", {"tasks": []})
    path = next(tmp_path.glob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["representation_version"] = 999
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(FleetStateCorruptError):
        store.load("coordinator-a")


def test_coordinator_identity_mismatch_is_corrupt(tmp_path) -> None:
    store = FileFleetStateStore(tmp_path)
    store.save("coordinator-a", {"tasks": []})
    path = next(tmp_path.glob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["coordinator_id"] = "coordinator-b"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(FleetStateCorruptError):
        store.load("coordinator-a")


def test_non_json_snapshot_is_rejected_before_write(tmp_path) -> None:
    store = FileFleetStateStore(tmp_path)

    with pytest.raises(FleetStateError, match="JSON serializable"):
        store.save("coordinator-a", {"invalid": object()})

    assert store.load("coordinator-a") is None
