import json
import os
from pathlib import Path

from loom_ai import Intent, WorkerResult, WorkerStatus


def _conformance_fixture() -> Path:
    root = os.environ.get("LOOM_AI_CONFORMANCE_ROOT")
    if root:
        return Path(root) / "fixtures" / "worker-result-success.json"
    return (
        Path(__file__).resolve().parents[2]
        / "loom-ai"
        / "conformance"
        / "fixtures"
        / "worker-result-success.json"
    )


FIXTURE = _conformance_fixture()


def test_worker_result_success_fixture_is_consumed_by_python_realization() -> None:
    assert FIXTURE.is_file(), (
        "Authoritative Loom AI conformance fixture is required. "
        "Clone FlossWare/loom-ai beside this repository or set "
        "LOOM_AI_CONFORMANCE_ROOT."
    )
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
