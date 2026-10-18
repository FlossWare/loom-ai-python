"""Python implementation of the FlossWare Loom AI-domain contracts.

The canonical AI-domain semantics live in FlossWare/loom-ai. This package
provides their Python realization. Generic Loom protocol semantics remain
owned by FlossWare/loom.
"""

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.capability_runtime import CapabilityInvocationError, invoke_capability
from loom_ai.configured_worker import (
    ConfiguredModelWorker,
    WorkerActivationError,
    activate_model_worker,
)
from loom_ai.evaluation import EvaluationRecord, EvaluationStatus
from loom_ai.execution_state import (
    ExecutionState,
    ExecutionStateCorruptError,
    ExecutionStateError,
    ExecutionStateStore,
    FileExecutionStateStore,
)
from loom_ai.fake_model_provider import FakeModelProvider
from loom_ai.fleet import (
    FleetConflictError,
    FleetCoordinator,
    FleetError,
    FleetTask,
    FleetTaskStatus,
    StaleLeaseError,
    TaskLease,
    WorkerLifecycle,
    WorkerNotRegisteredError,
    WorkerRegistration,
)
from loom_ai.fleet_http import FleetHTTPError, FleetHTTPServer, FleetWorkerAgent
from loom_ai.intent import Intent, IntentParseError
from loom_ai.model import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelToolDefinition,
    ModelToolResult,
)
from loom_ai.model_worker import ModelWorker
from loom_ai.orchestrator import (
    CancellationResult,
    Orchestrator,
    OrchestratorSubmission,
)
from loom_ai.pipeline import (
    ArbitrationStage,
    SequentialArbitrationPipeline,
    StageEvaluation,
)
from loom_ai.server import LoomServer
from loom_ai.worker import (
    CancellationToken,
    Worker,
    WorkerContext,
    WorkerResult,
    WorkerStatus,
)
from loom_ai.worker_configuration import (
    ValidatedWorkerConfiguration,
    WorkerConfigurationError,
    WorkerLimits,
    validate_worker_configuration,
)

__all__ = [
    "Arbiter",
    "ArbiterDecision",
    "CancellationResult",
    "CapabilityInvocationError",
    "CancellationToken",
    "ConfiguredModelWorker",
    "ArbitrationStage",
    "EvaluationRecord",
    "EvaluationStatus",
    "ExecutionState",
    "ExecutionStateCorruptError",
    "ExecutionStateError",
    "ExecutionStateStore",
    "FakeModelProvider",
    "FileExecutionStateStore",
    "Intent",
    "IntentParseError",
    "LoomServer",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelToolCall",
    "ModelToolDefinition",
    "ModelToolResult",
    "FleetCoordinator",
    "FleetHTTPError",
    "FleetHTTPServer",
    "FleetWorkerAgent",
    "FleetConflictError",
    "FleetError",
    "FleetTask",
    "FleetTaskStatus",
    "StaleLeaseError",
    "TaskLease",
    "WorkerLifecycle",
    "WorkerNotRegisteredError",
    "WorkerRegistration",
    "ModelWorker",
    "Orchestrator",
    "OrchestratorSubmission",
    "SequentialArbitrationPipeline",
    "StageEvaluation",
    "ValidatedWorkerConfiguration",
    "WorkerActivationError",
    "WorkerConfigurationError",
    "WorkerLimits",
    "Worker",
    "WorkerContext",
    "WorkerEvaluation",
    "WorkerResult",
    "WorkerStatus",
    "activate_model_worker",
    "invoke_capability",
    "validate_worker_configuration",
]

__version__ = "0.1"
