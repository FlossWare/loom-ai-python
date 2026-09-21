"""Durable execution-state boundary for the Python Loom implementation.

The language-neutral semantics are defined by FlossWare/loom-ai. This module
provides a small Python storage port and a filesystem realization suitable for
process-boundary qualification without choosing a database.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from loom_ai.intent import Intent

EXECUTION_STATE_VERSION = 1
_TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled", "interrupted"})


class ExecutionStateError(ValueError):
    """Base error for invalid durable execution state."""


class ExecutionStateCorruptError(ExecutionStateError):
    """Raised when durable state exists but cannot be trusted or decoded."""


@dataclass(frozen=True)
class ExecutionState:
    """Durable snapshot sufficient to reconstruct an execution context."""

    execution_id: str
    intent: Intent
    state: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Mapping[str, Any], ...] = ()
    status: str = "interrupted"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    representation_version: int = EXECUTION_STATE_VERSION

    def __post_init__(self) -> None:
        if not self.execution_id.strip():
            raise ExecutionStateError("execution_id must not be empty")
        if self.status not in {"running", *_TERMINAL_STATUSES}:
            raise ExecutionStateError(f"unsupported execution status: {self.status!r}")
        if self.representation_version != EXECUTION_STATE_VERSION:
            raise ExecutionStateError(
                f"unsupported execution-state version: "
                f"{self.representation_version}"
            )

    @property
    def terminal(self) -> bool:
        """Whether the snapshot represents a terminal execution outcome."""

        return self.status in _TERMINAL_STATUSES


class ExecutionStateStore(Protocol):
    """Minimal durable store port used by Loom execution infrastructure."""

    def save(self, execution: ExecutionState) -> None:
        """Durably replace the current snapshot for an execution identity."""

    def load(self, execution_id: str) -> ExecutionState | None:
        """Load durable state, returning None when no state exists."""


class FileExecutionStateStore:
    """Atomic JSON-file realization of the durable execution-state port."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, execution: ExecutionState) -> None:
        """Persist one execution snapshot atomically."""

        payload = _execution_to_dict(execution)
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        path = self._path(execution.execution_id)
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        except OSError as exc:
            raise ExecutionStateError(
                f"could not persist execution {execution.execution_id!r}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, execution_id: str) -> ExecutionState | None:
        """Load one execution snapshot from durable storage."""

        if not execution_id.strip():
            raise ExecutionStateError("execution_id must not be empty")
        path = self._path(execution_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ExecutionStateError(
                f"could not read execution {execution_id!r}"
            ) from exc

        try:
            payload = json.loads(data.decode("utf-8"))
            return _execution_from_dict(payload, expected_id=execution_id)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            raise ExecutionStateCorruptError(
                f"durable state for execution {execution_id!r} is corrupt"
            ) from exc

    def _path(self, execution_id: str) -> Path:
        digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"


def _execution_to_dict(execution: ExecutionState) -> dict[str, Any]:
    return {
        "representation_version": execution.representation_version,
        "execution_id": execution.execution_id,
        "intent": {
            "title": execution.intent.title,
            "goal": execution.intent.goal,
            "requirements": list(execution.intent.requirements),
            "constraints": list(execution.intent.constraints),
            "acceptance": list(execution.intent.acceptance),
            "intent_id": execution.intent.intent_id,
            "provenance": dict(execution.intent.provenance),
        },
        "state": dict(execution.state),
        "evidence": [dict(item) for item in execution.evidence],
        "status": execution.status,
        "metadata": dict(execution.metadata),
    }


def _execution_from_dict(
    payload: Any, *, expected_id: str
) -> ExecutionState:
    if not isinstance(payload, dict):
        raise TypeError("execution state must be a JSON object")
    if payload.get("representation_version") != EXECUTION_STATE_VERSION:
        raise ValueError("unsupported execution-state representation version")
    if payload.get("execution_id") != expected_id:
        raise ValueError("execution identity does not match requested execution")

    intent_payload = payload["intent"]
    if not isinstance(intent_payload, dict):
        raise TypeError("intent must be a JSON object")

    evidence = payload.get("evidence", [])
    if not isinstance(evidence, list) or not all(
        isinstance(item, dict) for item in evidence
    ):
        raise TypeError("evidence must be a list of JSON objects")

    return ExecutionState(
        execution_id=expected_id,
        intent=Intent(
            title=intent_payload["title"],
            goal=intent_payload["goal"],
            requirements=tuple(intent_payload["requirements"]),
            constraints=tuple(intent_payload["constraints"]),
            acceptance=tuple(intent_payload["acceptance"]),
            intent_id=intent_payload["intent_id"],
            provenance=dict(intent_payload["provenance"]),
        ),
        state=dict(payload.get("state", {})),
        evidence=tuple(evidence),
        status=payload["status"],
        metadata=dict(payload.get("metadata", {})),
        representation_version=payload["representation_version"],
    )
