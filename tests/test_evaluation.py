"""Tests for portable task-evaluation records."""

from __future__ import annotations

import pytest

from loom_ai.evaluation import EvaluationRecord, EvaluationStatus


def build_evaluation(**overrides) -> EvaluationRecord:
    values = {
        "execution_id": "execution-1",
        "criteria_id": "acceptance",
        "criteria_version": "1",
        "evaluator_id": "test-suite",
        "evaluator_version": "1",
        "status": EvaluationStatus.MET,
        "evaluated_at": "2026-10-16T12:00:00+00:00",
        "evidence": ({"test": "passed"},),
        "provenance": {"source": "unit-test"},
    }
    values.update(overrides)
    return EvaluationRecord(**values)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("met", EvaluationStatus.MET),
        ("partially_met", EvaluationStatus.PARTIALLY_MET),
        ("not_met", EvaluationStatus.NOT_MET),
        ("inconclusive", EvaluationStatus.INCONCLUSIVE),
        ("not_evaluated", EvaluationStatus.NOT_EVALUATED),
    ],
)
def test_portable_evaluation_statuses_round_trip(status, expected) -> None:
    record = build_evaluation(status=status)

    assert record.status is expected
    assert record.to_dict()["status"] == status


def test_evaluation_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone"):
        build_evaluation(evaluated_at="2026-10-16T12:00:00")


def test_evaluation_confidence_must_be_between_zero_and_one() -> None:
    with pytest.raises(ValueError, match="confidence"):
        build_evaluation(confidence=1.5)


def test_evaluation_record_preserves_evidence_and_provenance() -> None:
    record = build_evaluation()

    assert record.to_dict()["evidence"] == [{"test": "passed"}]
    assert record.to_dict()["provenance"] == {"source": "unit-test"}
