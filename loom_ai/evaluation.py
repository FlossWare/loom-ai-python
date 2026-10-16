"""Portable task-evaluation records, separate from Arbiter control decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import math
from typing import Any, Mapping


class EvaluationStatus(str, Enum):
    """Portable evaluation meanings defined by the AI-domain contract."""

    MET = "met"
    PARTIALLY_MET = "partially_met"
    NOT_MET = "not_met"
    INCONCLUSIVE = "inconclusive"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class EvaluationRecord:
    """One attributable assessment of declared task criteria."""

    execution_id: str
    criteria_id: str
    criteria_version: str
    evaluator_id: str
    evaluator_version: str
    status: EvaluationStatus
    evaluated_at: str
    evidence: tuple[Mapping[str, Any], ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    policy_version: str | None = None
    score: float | None = None
    confidence: float | None = None
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "execution_id",
            "criteria_id",
            "criteria_version",
            "evaluator_id",
            "evaluator_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if isinstance(self.status, str):
            object.__setattr__(self, "status", EvaluationStatus(self.status))
        if not isinstance(self.status, EvaluationStatus):
            raise ValueError("status must be an EvaluationStatus")
        try:
            timestamp = datetime.fromisoformat(self.evaluated_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("evaluated_at must be an ISO 8601 timestamp") from exc
        if timestamp.tzinfo is None:
            raise ValueError("evaluated_at must include a timezone")
        if not all(isinstance(item, Mapping) for item in self.evidence):
            raise ValueError("evidence must contain mappings")
        if not isinstance(self.provenance, Mapping):
            raise ValueError("provenance must be a mapping")
        if self.score is not None and not math.isfinite(self.score):
            raise ValueError("score must be finite")
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be finite and between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON-compatible representation of this record."""
        payload: dict[str, Any] = {
            "execution_id": self.execution_id,
            "criteria_id": self.criteria_id,
            "criteria_version": self.criteria_version,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "status": self.status.value,
            "evaluated_at": self.evaluated_at,
            "evidence": [dict(item) for item in self.evidence],
            "provenance": dict(self.provenance),
        }
        if self.policy_version is not None:
            payload["policy_version"] = self.policy_version
        if self.score is not None:
            payload["score"] = self.score
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.uncertainty is not None:
            payload["uncertainty"] = self.uncertainty
        return payload
