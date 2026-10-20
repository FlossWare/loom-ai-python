from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import pytest

from loom_ai.execution_state import (
    ExecutionState,
    ExecutionStateConflictError,
    ExecutionStateCorruptError,
    _execution_to_dict,
)
from loom_ai.intent import Intent
from loom_ai.postgres_execution_state import PostgresExecutionStateStore


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


def build_state() -> ExecutionState:
    return ExecutionState(
        execution_id="execution-postgres-1",
        intent=Intent(
            title="Persist execution",
            goal="resume after restart",
            intent_id="intent-postgres-1",
            provenance={"source": "test"},
        ),
        state={"phase": "continued", "step": 3},
        evidence=({"worker": "verify", "attempt": 2},),
        status="interrupted",
        metadata={"phase": "continued"},
        result={
            "worker_id": "verify",
            "status": "success",
            "outcome": "succeeded",
            "artifacts": [{"uri": "file:///artifact"}],
            "observations": [{"event_id": "event-1"}],
            "provenance": {"event_ids": ["event-1"]},
        },
    )


def test_postgres_store_initializes_versioned_jsonb_table_and_index():
    pool = FakePool()
    store = PostgresExecutionStateStore(pool)
    store.initialize()

    sql = "\\n".join(statement for statement, _ in pool.connection_instance.statements)
    assert "CREATE SCHEMA IF NOT EXISTS loom_runtime" in sql
    assert "loom_runtime.execution_states" in sql
    assert "representation_version INTEGER NOT NULL" in sql
    assert "payload JSONB NOT NULL" in sql
    assert "PRIMARY KEY" in sql
    assert "idx_execution_states_updated_at" in sql


def test_postgres_store_create_and_save_preserve_full_execution_envelope(monkeypatch):
    monkeypatch.setattr("loom_ai.postgres_execution_state._jsonb", lambda value: value)
    pool = FakePool()
    store = PostgresExecutionStateStore(pool)
    execution = build_state()

    store.create(execution)
    create_sql, create_params = pool.connection_instance.statements[-1]
    assert "INSERT INTO loom_runtime.execution_states" in create_sql
    assert "ON CONFLICT" not in create_sql
    assert create_params[0] == execution.execution_id
    assert create_params[2] == _execution_to_dict(execution)

    store.save(replace(execution, status="success", lifecycle="completed"))
    save_sql, save_params = pool.connection_instance.statements[-1]
    assert "ON CONFLICT (execution_id) DO UPDATE" in save_sql
    assert save_params[2]["result"]["provenance"]["event_ids"] == ["event-1"]


def test_postgres_store_load_round_trips_execution_state(monkeypatch):
    monkeypatch.setattr("loom_ai.postgres_execution_state._jsonb", lambda value: value)
    pool = FakePool()
    expected = build_state()
    pool.connection_instance.next_row = (_execution_to_dict(expected),)

    restored = PostgresExecutionStateStore(pool).load(expected.execution_id)

    assert restored == expected
    assert restored is not None
    assert restored.intent.provenance == {"source": "test"}
    assert restored.result is not None
    assert restored.result["artifacts"] == [{"uri": "file:///artifact"}]
    assert restored.result["observations"] == [{"event_id": "event-1"}]


def test_postgres_store_distinguishes_missing_and_corrupt_state(monkeypatch):
    pool = FakePool()
    store = PostgresExecutionStateStore(pool)
    assert store.load("missing") is None

    pool.connection_instance.next_row = ("{not-json",)
    with pytest.raises(ExecutionStateCorruptError):
        store.load("execution-postgres-1")

    pool.connection_instance.next_row = ({"execution_id": "different"},)
    with pytest.raises(ExecutionStateCorruptError):
        store.load("execution-postgres-1")


def test_postgres_store_create_rejects_duplicate_identity(monkeypatch):
    monkeypatch.setattr("loom_ai.postgres_execution_state._jsonb", lambda value: value)
    pool = FakePool()
    pool.connection_instance.error = UniqueViolation("duplicate")
    store = PostgresExecutionStateStore(pool)

    with pytest.raises(ExecutionStateConflictError, match="execution already exists"):
        store.create(build_state())


def test_postgres_store_validates_schema_and_closes_pool():
    with pytest.raises(ValueError, match="schema"):
        PostgresExecutionStateStore(FakePool(), schema="loom; DROP SCHEMA public")

    pool = FakePool()
    store = PostgresExecutionStateStore(pool)
    store.close()
    assert pool.closed


def test_live_postgres_execution_recovery_when_configured():
    import os
    from uuid import uuid4

    dsn = os.getenv("FLOSSWARE_LOOM_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set FLOSSWARE_LOOM_POSTGRES_DSN to run live PostgreSQL recovery")

    execution = replace(build_state(), execution_id=f"loom-recovery-{uuid4().hex}")
    store = PostgresExecutionStateStore.connect(dsn)
    try:
        store.initialize()
        store.create(execution)
        store.save(replace(execution, status="success", lifecycle="completed"))
        restored = store.load(execution.execution_id)
        assert restored == replace(execution, status="success", lifecycle="completed")
    finally:
        if store.load(execution.execution_id) is not None:
            with store.pool.connection() as connection:
                connection.execute(
                    "DELETE FROM loom_runtime.execution_states WHERE execution_id = %s",
                    (execution.execution_id,),
                )
        store.close()
