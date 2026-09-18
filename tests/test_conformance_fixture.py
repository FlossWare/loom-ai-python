import json
from pathlib import Path

from loom_ai import Intent, WorkerResult, WorkerStatus


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "conformance"
    / "fixtures"
    / "worker-result-success.json"
)


def test_worker_result_success_fixture_is_consumed_by_python_realization() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    intent = Intent(
        goal=fixture["intent"]["goal"],
        requirements=fixture["intent"]["requirements"],
        constraints=fixture["intent"]["constraints"],
        acceptance=fixture["intent"]["acceptance"],
    )
    assert intent.goal == "produce a successful result"

    expected = fixture["required_semantics"]["worker_result"]
    result = WorkerResult(
        worker_id="fixture",
        status=WorkerStatus.SUCCESS,
        output=expected["output"],
        evidence=tuple(expected["evidence"]),
    )

    assert result.successful
    assert result.output == {"value": "ok"}
    assert result.evidence == ({"fact": "worker completed"},)
