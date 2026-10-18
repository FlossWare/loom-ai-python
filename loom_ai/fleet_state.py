"""Versioned, atomic persistence port for fleet coordinator snapshots.

The coordinator owns snapshot semantics and recovery policy. This module only
provides a storage boundary, so filesystem durability is not confused with
multi-process coordination or transactional scheduling.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Protocol

FLEET_STATE_VERSION = 1


class FleetStateError(ValueError):
    """Base error for fleet snapshot storage failures."""


class FleetStateCorruptError(FleetStateError):
    """Raised when a stored fleet snapshot cannot be trusted."""


class FleetStateConflictError(FleetStateError):
    """Raised when create-only persistence finds an existing coordinator."""


class FleetStateStore(Protocol):
    """Storage port for one complete coordinator snapshot."""

    def create(self, coordinator_id: str, snapshot: Mapping[str, Any]) -> None:
        """Atomically create a snapshot without replacing existing state."""

    def save(self, coordinator_id: str, snapshot: Mapping[str, Any]) -> None:
        """Atomically replace a coordinator snapshot."""

    def load(self, coordinator_id: str) -> Mapping[str, Any] | None:
        """Load a snapshot, returning None when it does not exist."""


class FileFleetStateStore:
    """Atomic JSON-file implementation for single-coordinator persistence.

    Writes use a temporary file, fsync, and atomic link/replace. This protects
    readers from partial JSON, but intentionally does not claim cross-process
    compare-and-swap or safe concurrent coordinator leadership.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, coordinator_id: str, snapshot: Mapping[str, Any]) -> None:
        """Create the first snapshot for a coordinator identity."""
        path = self._path(coordinator_id)
        temporary_path: Path | None = None
        data = self._encode(coordinator_id, snapshot)
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.root,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise FleetStateConflictError(
                f"fleet coordinator already exists: {coordinator_id}"
            ) from exc
        except OSError as exc:
            raise FleetStateError(
                f"could not create fleet snapshot for {coordinator_id!r}"
            ) from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def save(self, coordinator_id: str, snapshot: Mapping[str, Any]) -> None:
        """Atomically replace a coordinator snapshot."""
        path = self._path(coordinator_id)
        temporary_path: Path | None = None
        data = self._encode(coordinator_id, snapshot)
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.root,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        except OSError as exc:
            raise FleetStateError(
                f"could not persist fleet snapshot for {coordinator_id!r}"
            ) from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def load(self, coordinator_id: str) -> Mapping[str, Any] | None:
        """Read and validate a stored snapshot envelope."""
        if not coordinator_id.strip():
            raise FleetStateError("coordinator_id must not be empty")
        path = self._path(coordinator_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FleetStateError(
                f"could not read fleet snapshot for {coordinator_id!r}"
            ) from exc

        try:
            envelope = json.loads(data.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise TypeError("snapshot envelope must be an object")
            if envelope.get("representation_version") != FLEET_STATE_VERSION:
                raise ValueError("unsupported fleet snapshot version")
            if envelope.get("coordinator_id") != coordinator_id:
                raise ValueError("coordinator identity does not match")
            snapshot = envelope.get("snapshot")
            if not isinstance(snapshot, dict):
                raise TypeError("snapshot payload must be an object")
            return snapshot
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise FleetStateCorruptError(
                f"durable fleet snapshot for {coordinator_id!r} is corrupt"
            ) from exc

    def _path(self, coordinator_id: str) -> Path:
        if not coordinator_id.strip():
            raise FleetStateError("coordinator_id must not be empty")
        digest = hashlib.sha256(coordinator_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    @staticmethod
    def _encode(coordinator_id: str, snapshot: Mapping[str, Any]) -> bytes:
        if not coordinator_id.strip():
            raise FleetStateError("coordinator_id must not be empty")
        if not isinstance(snapshot, Mapping):
            raise FleetStateError("snapshot must be a mapping")
        try:
            envelope = {
                "representation_version": FLEET_STATE_VERSION,
                "coordinator_id": coordinator_id,
                "snapshot": dict(snapshot),
            }
            return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        except (TypeError, ValueError) as exc:
            raise FleetStateError("fleet snapshot must be JSON serializable") from exc
