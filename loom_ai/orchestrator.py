"""Application-facing orchestration service for Loom execution.

HTTP, MCP, and CLI/TUI adapters delegate through this boundary. The
language-neutral Orchestrator semantics are defined by FlossWare/loom-ai; this
module is the Python realization.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

from loom_ai.arbiter import Arbiter
from loom_ai.execution_state import ExecutionState, ExecutionStateStore
from loom_ai.intent import Intent
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


@dataclass(frozen=True)
class OrchestratorSubmission:
    """Result of submitting or continuing an execution."""

    execution_id: str | None
    result: WorkerResult


class Orchestrator:
    """Own execution invocation, durable lifecycle, and observable state."""

    def __init__(
        self,
        arbiter: Arbiter,
        *,
        execution_store: ExecutionStateStore | None = None,
    ) -> None:
        self.arbiter = arbiter
        self.execution_store = execution_store

    @property
    def durable_enabled(self) -> bool:
        """Whether durable execution observation and continuation are available."""
        return self.execution_store is not None

    @property
    def durable(self) -> bool:
        """Compatibility alias for durable_enabled."""
        return self.durable_enabled

    def execute(self, intent: Intent) -> WorkerResult:
        """Execute an Intent through the configured Arbiter."""
        return self.arbiter.execute(WorkerContext(intent=intent))

    def submit(
        self,
        intent: Intent,
        *,
        execution_id: str | None = None,
    ) -> OrchestratorSubmission:
        """Submit an Intent and persist lifecycle state when configured."""
        if self.execution_store is None:
            return OrchestratorSubmission(None, self.execute(intent))

        actual_id = execution_id or str(uuid4())
        self.execution_store.save(
            ExecutionState(
                execution_id=actual_id,
                intent=intent,
                state={"phase": "initial"},
                status="running",
                metadata={"phase": "initial"},
            )
        )
        try:
            result = self.execute(intent)
        except Exception as exc:
            failure = _exception_result(exc, phase="initial")
            self.execution_store.save(
                _state_from_result(actual_id, intent, failure, phase="initial")
            )
            raise

        self.execution_store.save(
            _state_from_result(actual_id, intent, result, phase="initial")
        )
        return OrchestratorSubmission(actual_id, result)

    def observe(self, execution_id: str) -> ExecutionState:
        """Return durable execution state through the application boundary."""
        if self.execution_store is None:
            raise RuntimeError("durable execution state is not configured")
        execution = self.execution_store.load(execution_id)
        if execution is None:
            raise KeyError(f"execution not found: {execution_id}")
        return execution

    def continue_execution(self, execution_id: str) -> OrchestratorSubmission:
        """Continue an execution from its persisted context and evidence."""
        execution = self.observe(execution_id)
        context_state = dict(execution.state)
        if execution.result is not None:
            context_state["last_execution_result"] = execution.result
        context = WorkerContext(
            intent=execution.intent,
            state=context_state,
            evidence=execution.evidence,
            metadata=execution.metadata,
        )
        try:
            result = self.arbiter.execute(context)
        except Exception as exc:
            failure = _exception_result(exc, phase="continued")
            assert self.execution_store is not None
            self.execution_store.save(
                _state_from_result(
                    execution_id,
                    execution.intent,
                    failure,
                    phase="continued",
                    prior=execution,
                )
            )
            raise

        assert self.execution_store is not None
        self.execution_store.save(
            _state_from_result(
                execution_id,
                execution.intent,
                result,
                phase="continued",
                prior=execution,
            )
        )
        return OrchestratorSubmission(execution_id, result)


def _state_from_result(
    execution_id: str,
    intent: Intent,
    result: WorkerResult,
    *,
    phase: str,
    prior: ExecutionState | None = None,
) -> ExecutionState:
    """Build durable state without conflating cancellation with failure."""
    prior_evidence = prior.evidence if prior is not None else ()
    evidence = (
        prior_evidence
        + ({"type": "execution-phase", "phase": phase, "execution_id": execution_id},)
        + result.evidence
    )
    state = dict(prior.state) if prior is not None else {}
    state.update(
        {
            "last_worker": result.worker_id,
            "last_status": result.status.value,
            "phase": phase,
        }
    )
    metadata = dict(prior.metadata) if prior is not None else {}
    metadata.update(
        {
            "phase": phase,
            "worker_id": result.worker_id,
            "result_metadata": dict(result.metadata),
        }
    )
    status = {
        WorkerStatus.SUCCESS: "success",
        WorkerStatus.FAILED: "failed",
        WorkerStatus.CANCELLED: "cancelled",
    }[result.status]
    return ExecutionState(
        execution_id=execution_id,
        intent=intent,
        state=state,
        evidence=evidence,
        status=status,
        metadata=metadata,
        result=_result_to_dict(result),
    )


def _result_to_dict(result: WorkerResult) -> dict[str, Any]:
    """Convert a structured Worker Result into a durable JSON representation."""
    return {
        "worker_id": result.worker_id,
        "status": result.status.value,
        "output": _json_value(result.output),
        "evidence": _json_value(result.evidence),
        "error": result.error,
        "metadata": _json_value(result.metadata),
    }


def _json_value(value: Any) -> Any:
    """Convert contract values to JSON data without stringifying unknown objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _json_value(getattr(value, item.name)) for item in fields(value)
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("durable result mapping keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported durable result value: {type(value).__name__}")


def _exception_result(exc: Exception, *, phase: str) -> WorkerResult:
    """Create safe durable failure evidence before propagating an exception."""
    exception_type = type(exc).__name__
    return WorkerResult(
        worker_id="orchestrator",
        status=WorkerStatus.FAILED,
        error=f"execution raised {exception_type}",
        evidence=(
            {
                "type": "execution-exception",
                "phase": phase,
                "exception_type": exception_type,
            },
        ),
        metadata={"exception_type": exception_type},
    )
