"""PostgreSQL-backed durable fleet coordinator snapshots."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from loom_ai.fleet_state import (
    FLEET_STATE_VERSION,
    FleetStateConflictError,
    FleetStateCorruptError,
    FleetStateError,
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
            "PostgresFleetStateStore requires the 'postgres' optional dependencies"
        ) from exc
    return Jsonb(value)


class PostgresFleetStateStore:
    """Persist versioned fleet snapshots using PostgreSQL JSONB.

    The adapter preserves the existing FleetStateStore contract. Coordinator
    leadership and concurrent scheduling remain coordinator responsibilities;
    this class provides durable snapshots, not distributed leader election.
    """

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
    ) -> PostgresFleetStateStore:
        """Open a synchronous psycopg pool using the optional postgres extra."""
        _identifier(schema, "schema")
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - requires optional dependency
            raise RuntimeError(
                "PostgresFleetStateStore.connect requires loom-ai-python[postgres]"
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
        """Create the adapter-owned fleet snapshot table idempotently."""
        with self.pool.connection() as connection:
            connection.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.schema}.fleet_states (
                    coordinator_id TEXT PRIMARY KEY,
                    representation_version INTEGER NOT NULL,
                    snapshot JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_fleet_states_updated_at
                ON {self.schema}.fleet_states (updated_at)
                """
            )

    def create(self, coordinator_id: str, snapshot: Mapping[str, Any]) -> None:
        """Create a coordinator identity without replacing existing state."""
        payload = _snapshot_payload(snapshot)
        if not coordinator_id.strip():
            raise FleetStateError("coordinator_id must not be empty")
        try:
            with self.pool.connection() as connection:
                connection.execute(
                    f"""
                    INSERT INTO {self.schema}.fleet_states
                        (coordinator_id, representation_version, snapshot)
                    VALUES (%s, %s, %s)
                    """,
                    (coordinator_id, FLEET_STATE_VERSION, _jsonb(payload)),
                )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise FleetStateConflictError(
                    f"fleet coordinator already exists: {coordinator_id}"
                ) from exc
            raise FleetStateError(
                f"could not create fleet snapshot for {coordinator_id!r}"
            ) from exc

    def save(self, coordinator_id: str, snapshot: Mapping[str, Any]) -> None:
        """Atomically replace a coordinator snapshot."""
        payload = _snapshot_payload(snapshot)
        if not coordinator_id.strip():
            raise FleetStateError("coordinator_id must not be empty")
        try:
            with self.pool.connection() as connection:
                connection.execute(
                    f"""
                    INSERT INTO {self.schema}.fleet_states
                        (coordinator_id, representation_version, snapshot, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (coordinator_id) DO UPDATE SET
                        representation_version = EXCLUDED.representation_version,
                        snapshot = EXCLUDED.snapshot,
                        updated_at = now()
                    """,
                    (coordinator_id, FLEET_STATE_VERSION, _jsonb(payload)),
                )
        except Exception as exc:
            raise FleetStateError(
                f"could not persist fleet snapshot for {coordinator_id!r}"
            ) from exc

    def load(self, coordinator_id: str) -> Mapping[str, Any] | None:
        """Load and validate one fleet snapshot."""
        if not coordinator_id.strip():
            raise FleetStateError("coordinator_id must not be empty")
        try:
            with self.pool.connection() as connection:
                cursor = connection.execute(
                    f"""
                    SELECT representation_version, snapshot
                    FROM {self.schema}.fleet_states
                    WHERE coordinator_id = %s
                    """,
                    (coordinator_id,),
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise FleetStateError(
                f"could not read fleet snapshot for {coordinator_id!r}"
            ) from exc
        if row is None:
            return None
        version, snapshot = row
        if version != FLEET_STATE_VERSION:
            raise FleetStateCorruptError(
                f"durable fleet snapshot for {coordinator_id!r} has an unsupported version"
            )
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except json.JSONDecodeError as exc:
                raise FleetStateCorruptError(
                    f"durable fleet snapshot for {coordinator_id!r} is corrupt"
                ) from exc
        if not isinstance(snapshot, dict):
            raise FleetStateCorruptError(
                f"durable fleet snapshot for {coordinator_id!r} is corrupt"
            )
        return snapshot

    def close(self) -> None:
        """Close the underlying connection pool."""
        self.pool.close()


def _snapshot_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise FleetStateError("snapshot must be a mapping")
    try:
        payload = json.loads(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise FleetStateError("fleet snapshot must be JSON serializable") from exc
    if not isinstance(payload, dict):
        raise FleetStateError("snapshot must be a JSON object")
    return payload
