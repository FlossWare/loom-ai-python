"""PostgreSQL-backed durable execution snapshots for Loom.

This adapter implements the existing synchronous ExecutionStateStore port.
It persists the complete versioned execution envelope in JSONB and keeps the
execution identity indexed independently of payload shape.
"""

from __future__ import annotations

import json
import re
from typing import Any

from loom_ai.execution_state import (
    ExecutionState,
    ExecutionStateConflictError,
    ExecutionStateCorruptError,
    ExecutionStateError,
    _execution_from_dict,
    _execution_to_dict,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value) or len(value) > 45:
        raise ValueError(f"{label} must be a simple SQL identifier (max 45 chars)")
    return value


def _jsonb(value: Any) -> Any:
    try:
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - requires optional dependency
        raise RuntimeError(
            "PostgresExecutionStateStore requires the 'postgres' optional dependencies"
        ) from exc
    return Jsonb(value)


class PostgresExecutionStateStore:
    """Durable execution state backed by PostgreSQL JSONB."""

    def __init__(self, pool: Any, *, schema: str = "loom_runtime") -> None:
        self.pool = pool
        self.schema = _identifier(schema, "schema")

    @classmethod
    def connect(
        cls,
        dsn: str,
        *,
        schema: str = "loom_runtime",
        min_size: int = 1,
        max_size: int = 10,
    ) -> PostgresExecutionStateStore:
        """Open a synchronous psycopg pool using the optional postgres extra."""
        _identifier(schema, "schema")
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - requires optional dependency
            raise RuntimeError(
                "PostgresExecutionStateStore.connect requires loom-ai-python[postgres]"
            ) from exc
        pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
        )
        pool.open()
        return cls(pool, schema=schema)

    def initialize(self) -> None:
        """Create the adapter-owned schema and execution table idempotently."""
        with self.pool.connection() as connection:
            connection.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.schema}.execution_states (
                    execution_id TEXT PRIMARY KEY,
                    representation_version INTEGER NOT NULL,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_execution_states_updated_at
                ON {self.schema}.execution_states (updated_at)
                """
            )

    def create(self, execution: ExecutionState) -> None:
        """Atomically claim an execution identity without replacing prior state."""
        payload = _execution_to_dict(execution)
        try:
            with self.pool.connection() as connection:
                connection.execute(
                    f"""
                    INSERT INTO {self.schema}.execution_states
                        (execution_id, representation_version, payload)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        execution.execution_id,
                        payload["representation_version"],
                        _jsonb(payload),
                    ),
                )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ExecutionStateConflictError(
                    f"execution already exists: {execution.execution_id}"
                ) from exc
            raise ExecutionStateError(
                f"could not create execution {execution.execution_id!r}"
            ) from exc

    def save(self, execution: ExecutionState) -> None:
        """Atomically replace one execution snapshot."""
        payload = _execution_to_dict(execution)
        try:
            with self.pool.connection() as connection:
                connection.execute(
                    f"""
                    INSERT INTO {self.schema}.execution_states
                        (execution_id, representation_version, payload, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (execution_id) DO UPDATE SET
                        representation_version = EXCLUDED.representation_version,
                        payload = EXCLUDED.payload,
                        updated_at = now()
                    """,
                    (
                        execution.execution_id,
                        payload["representation_version"],
                        _jsonb(payload),
                    ),
                )
        except Exception as exc:
            raise ExecutionStateError(
                f"could not persist execution {execution.execution_id!r}"
            ) from exc

    def load(self, execution_id: str) -> ExecutionState | None:
        """Load and validate one execution snapshot from PostgreSQL."""
        if not execution_id.strip():
            raise ExecutionStateError("execution_id must not be empty")
        try:
            with self.pool.connection() as connection:
                cursor = connection.execute(
                    f"""
                    SELECT representation_version, payload
                    FROM {self.schema}.execution_states
                    WHERE execution_id = %s
                    """,
                    (execution_id,),
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise ExecutionStateError(
                f"could not read execution {execution_id!r}"
            ) from exc
        if row is None:
            return None
        stored_version, payload = row
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ExecutionStateCorruptError(
                    f"durable state for execution {execution_id!r} is corrupt"
                ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("representation_version") != stored_version
        ):
            raise ExecutionStateCorruptError(
                f"durable state for execution {execution_id!r} is corrupt"
            )
        try:
            return _execution_from_dict(payload, expected_id=execution_id)
        except (TypeError, KeyError, ValueError) as exc:
            raise ExecutionStateCorruptError(
                f"durable state for execution {execution_id!r} is corrupt"
            ) from exc

    def close(self) -> None:
        """Close the underlying connection pool."""
        self.pool.close()
