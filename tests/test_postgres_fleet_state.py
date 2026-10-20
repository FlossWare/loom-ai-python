from __future__ import annotations

from contextlib import contextmanager

import pytest

from loom_ai.fleet_state import (
    FleetStateConflictError,
    FleetStateCorruptError,
    FleetStateError,
)
from loom_ai.postgres_fleet_state import PostgresFleetStateStore


class FakeCursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self):
        self.statements = []
        self.next_row = None
        self.error = None

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if self.error is not None:
            raise self.error
        if statement.lstrip().startswith("SELECT"):
            return FakeCursor(self.next_row)
        return FakeCursor()


class FakePool:
    def __init__(self):
        self.connection_instance = FakeConnection()
        self.closed = False

    @contextmanager
    def connection(self):
        yield self.connection_instance

    def close(self):
        self.closed = True


class UniqueViolation(Exception):
    sqlstate = "23505"


def test_postgres_fleet_store_initializes_versioned_snapshot_table():
    pool = FakePool()
    PostgresFleetStateStore(pool).initialize()
    sql = "\\n".join(statement for statement, _ in pool.connection_instance.statements)
    assert "CREATE SCHEMA IF NOT EXISTS loom_runtime" in sql
    assert "loom_runtime.fleet_states" in sql
    assert "representation_version INTEGER NOT NULL" in sql
    assert "snapshot JSONB NOT NULL" in sql
    assert "coordinator_id TEXT PRIMARY KEY" in sql


def test_postgres_fleet_store_create_and_save(monkeypatch):
    monkeypatch.setattr("loom_ai.postgres_fleet_state._jsonb", lambda value: value)
    pool = FakePool()
    store = PostgresFleetStateStore(pool)
    snapshot = {"tasks": [{"task_id": "task-1", "status": "queued"}]}

    store.create("coordinator-1", snapshot)
    create_sql, create_params = pool.connection_instance.statements[-1]
    assert "INSERT INTO loom_runtime.fleet_states" in create_sql
    assert "ON CONFLICT" not in create_sql
    assert create_params == ("coordinator-1", 1, snapshot)

    store.save("coordinator-1", {"tasks": []})
    save_sql, save_params = pool.connection_instance.statements[-1]
    assert "ON CONFLICT (coordinator_id) DO UPDATE" in save_sql
    assert save_params == ("coordinator-1", 1, {"tasks": []})


def test_postgres_fleet_store_loads_and_validates_snapshots():
    pool = FakePool()
    store = PostgresFleetStateStore(pool)
    assert store.load("missing") is None

    pool.connection_instance.next_row = (1, {"tasks": [{"task_id": "task-1"}]})
    assert store.load("coordinator-1") == {"tasks": [{"task_id": "task-1"}]}

    pool.connection_instance.next_row = (99, {})
    with pytest.raises(FleetStateCorruptError, match="unsupported version"):
        store.load("coordinator-1")

    pool.connection_instance.next_row = (1, "{not-json")
    with pytest.raises(FleetStateCorruptError, match="corrupt"):
        store.load("coordinator-1")


def test_postgres_fleet_store_duplicate_identity_and_invalid_snapshots(monkeypatch):
    monkeypatch.setattr("loom_ai.postgres_fleet_state._jsonb", lambda value: value)
    pool = FakePool()
    pool.connection_instance.error = UniqueViolation("duplicate")
    store = PostgresFleetStateStore(pool)

    with pytest.raises(FleetStateConflictError, match="already exists"):
        store.create("coordinator-1", {"tasks": []})
    with pytest.raises(FleetStateError, match="JSON serializable"):
        store.save("coordinator-1", {"invalid": object()})
    with pytest.raises(FleetStateError, match="must not be empty"):
        store.load("")


def test_postgres_fleet_store_validates_schema_and_closes_pool():
    with pytest.raises(ValueError, match="schema"):
        PostgresFleetStateStore(FakePool(), schema="loom; DROP SCHEMA public")

    pool = FakePool()
    PostgresFleetStateStore(pool).close()
    assert pool.closed


def test_live_postgres_fleet_snapshot_recovery_when_configured():
    import os
    from uuid import uuid4

    dsn = os.getenv("FLOSSWARE_LOOM_FLEET_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set FLOSSWARE_LOOM_FLEET_POSTGRES_DSN for live fleet recovery")

    coordinator_id = f"loom-fleet-recovery-{uuid4().hex}"
    expected = {
        "workers": [],
        "tasks": [{"task_id": "task-live", "status": "queued"}],
    }
    writer = PostgresFleetStateStore.connect(dsn)
    reader = None
    try:
        writer.initialize()
        writer.create(coordinator_id, expected)
        replacement = {"workers": [], "tasks": []}
        writer.save(coordinator_id, replacement)

        # A separate pool models a process restart and verifies durable recovery.
        reader = PostgresFleetStateStore.connect(dsn)
        assert reader.load(coordinator_id) == replacement
    finally:
        with writer.pool.connection() as connection:
            connection.execute(
                "DELETE FROM loom_runtime.fleet_states WHERE coordinator_id = %s",
                (coordinator_id,),
            )
        if reader is not None:
            reader.close()
        writer.close()
