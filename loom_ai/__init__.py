"""Python implementation of the FlossWare Loom AI-domain contracts.

The canonical AI-domain semantics live in FlossWare/loom-ai. This package
provides their Python realization. Generic Loom protocol semantics remain
owned by FlossWare/loom.
"""

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.fake_model_provider import FakeModelProvider
from loom_ai.intent import Intent, IntentParseError
from loom_ai.model import ModelProvider, ModelRequest, ModelResponse
from loom_ai.model_worker import ModelWorker
from loom_ai.server import LoomServer
from loom_ai.worker import Worker, WorkerContext, WorkerResult, WorkerStatus

__all__ = [
    "Arbiter",
    "ArbiterDecision",
    "FakeModelProvider",
    "Intent",
    "IntentParseError",
    "LoomServer",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelWorker",
    "Worker",
    "WorkerContext",
    "WorkerEvaluation",
    "WorkerResult",
    "WorkerStatus",
]

__version__ = "0.1"
