import json
from pathlib import Path

from loom_ai import FakeModelProvider, Intent, ModelRequest


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

    provider = FakeModelProvider()
    response = provider.generate(ModelRequest(prompt="fixture", model="test-model"))

    expected = fixture["required_semantics"]["worker_result"]
    assert expected["status"] == "success"
    assert response.text
    assert expected["output"]["value"] == "ok"
    assert expected["evidence"][0]["fact"] == "worker completed"
