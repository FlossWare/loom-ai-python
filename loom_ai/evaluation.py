"""Portable evaluation records kept separate from execution and Worker outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class EvaluationStatus(str, Enum):
    """Assessment of declared task criteria, independent of execution lifecycle."""

    MET = "met"
    PARTIALLY_MET = "partially_met"
    NOT_MET = "not_met"
    INCONCLUSIVE = "inconclusive"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class EvaluationRecord:
    """One attributable assessment; a new assessment never overwrites prior history."""

    status: EvaluationStatus = EvaluationStatus.NOT_EVALUATED
    execution_id: str | None = None
    intent_id: str | None = None
    result_reference: str | None = None
    criteria: tuple[str, ...] = ()
    criteria_version: str | None = None
    evaluator: str | None = None
    evaluator_version: str | None = None
    evidence: tuple[Mapping[str, Any], ...] = ()
    evaluated_at: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, EvaluationStatus):
            try:
                object.__setattr__(self, "status", EvaluationStatus(self.status))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unsupported evaluation status: {self.status!r}") from exc
        if not all(isinstance(item, str) for item in self.criteria):
            raise TypeError("evaluation criteria must be strings")
        if not all(isinstance(item, Mapping) for item in self.evidence):
            raise TypeError("evaluation evidence must contain mappings")

    def finalized(self, *, execution_id: str, intent_id: str) -> "EvaluationRecord":
        """Attach stable execution identity and timestamp without changing judgments."""
        return replace(
            self,
            execution_id=self.execution_id or execution_id,
            intent_id=self.intent_id or intent_id,
            evaluated_at=(
                self.evaluated_at
                or (
                    datetime.now(timezone.utc).isoformat()
                    if self.status is not EvaluationStatus.NOT_EVALUATED
                    else None
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible portable representation."""
        return {
            "status": self.status.value,
            "execution_id": self.execution_id,
            "intent_id": self.intent_id,
            "result_reference": self.result_reference,
            "criteria": list(self.criteria),
            "criteria_version": self.criteria_version,
            "evaluator": self.evaluator,
            "evaluator_version": self.evaluator_version,
            "evidence": [dict(item) for item in self.evidence],
            "evaluated_at": self.evaluated_at,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationRecord":
        """Restore a portable evaluation record from persisted JSON data."""
        return cls(
            status=EvaluationStatus(value.get("status", "not_evaluated")),
            execution_id=value.get("execution_id"),
            intent_id=value.get("intent_id"),
            result_reference=value.get("result_reference"),
            criteria=tuple(value.get("criteria", ())),
            criteria_version=value.get("criteria_version"),
            evaluator=value.get("evaluator"),
            evaluator_version=value.get("evaluator_version"),
            evidence=tuple(value.get("evidence", ())),
            evaluated_at=value.get("evaluated_at"),
            provenance=dict(value.get("provenance", {})),
        )
