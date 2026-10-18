"""Conformance tests for declarative Worker configuration preflight."""

from __future__ import annotations

from copy import deepcopy

import pytest

from loom_ai.worker_configuration import (
    WorkerConfigurationError,
    validate_worker_configuration,
)


def config() -> dict:
    return {
        "schema_version": "0.1",
        "worker_id": "coding-worker",
        "purpose": "Make bounded repository changes",
        "model_gateway": {
            "gateway_ref": "default-gateway",
            "selection_ref": "coding-balanced",
        },
        "instructions": ["Inspect before editing"],
        "tools": [
            {"capability_ref": "repository.read", "required": True},
            {"capability_ref": "verification.run", "required": True},
            {"capability_ref": "optional.search", "required": False},
        ],
        "capabilities": ["repository.read"],
        "policy": {
            "policy_ref": "bounded-coding",
            "approval_required": True,
            "allowed_capabilities": [
                "repository.read",
                "verification.run",
                "optional.search",
            ],
            "denied_capabilities": ["credential.read"],
        },
        "limits": {
            "max_attempts": 2,
            "max_duration_seconds": 900,
            "max_output_bytes": 200000,
        },
        "metadata": {"team": "engineering", "nested": {"items": ["keep"]}},
    }


GATEWAYS = {"default-gateway": {"coding-balanced", "research-balanced"}}
CAPABILITIES = {"repository.read", "verification.run"}
POLICIES = {
    "bounded-coding": {
        "approval_required": True,
        "allowed_capabilities": [
            "repository.read",
            "verification.run",
            "optional.search",
        ],
        "denied_capabilities": ["credential.read"],
    }
}


def validate(value: dict, *, policies=POLICIES):
    return validate_worker_configuration(
        value,
        gateway_selections=GATEWAYS,
        available_capabilities=CAPABILITIES,
        policy_registry=policies,
    )


def test_valid_config_becomes_immutable_effective_contract() -> None:
    result = validate(config())

    assert result.worker_id == "coding-worker"
    assert result.gateway_ref == "default-gateway"
    assert result.selection_ref == "coding-balanced"
    assert result.required_capabilities == {"repository.read", "verification.run"}
    assert result.limits.max_attempts == 2
    assert result.approval_required is True


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schema_version="9.0"), "unsupported"),
        (lambda value: value.update(unrecognized=True), "unknown fields"),
        (
            lambda value: value["model_gateway"].update(
                endpoint="https://example.test"
            ),
            "unknown fields",
        ),
        (
            lambda value: value["limits"].update(max_output_bytes=True),
            "positive integer",
        ),
        (lambda value: value["instructions"].clear(), "at least 1"),
        (
            lambda value: value["policy"].update(
                denied_capabilities=["repository.read"]
            ),
            "denies",
        ),
        (
            lambda value: value["policy"].update(
                allowed_capabilities=["repository.read"]
            ),
            "does not allow",
        ),
    ],
)
def test_invalid_config_fails_closed(mutation, message: str) -> None:
    value = config()
    mutation(value)

    with pytest.raises(WorkerConfigurationError, match=message):
        validate(value)


def test_unknown_gateway_and_selection_are_rejected() -> None:
    value = config()
    value["model_gateway"]["gateway_ref"] = "unregistered"

    with pytest.raises(WorkerConfigurationError, match="unknown model gateway"):
        validate(value)

    value = config()
    value["model_gateway"]["selection_ref"] = "vendor-account-secret"

    with pytest.raises(WorkerConfigurationError, match="unknown model selection"):
        validate(value)


def test_required_unavailable_capability_prevents_activation() -> None:
    value = config()
    value["tools"] = [{"capability_ref": "shell.execute", "required": True}]
    value["policy"]["allowed_capabilities"].append("shell.execute")

    permissive_for_test = {
        "bounded-coding": {
            "approval_required": True,
            "allowed_capabilities": [
                "repository.read",
                "verification.run",
                "optional.search",
                "shell.execute",
            ],
            "denied_capabilities": ["credential.read"],
        }
    }
    with pytest.raises(
        WorkerConfigurationError, match="required capabilities are unavailable"
    ):
        validate(value, policies=permissive_for_test)


def test_optional_unavailable_tool_is_not_required_for_activation() -> None:
    result = validate(config())

    assert "optional.search" in result.capabilities
    assert "optional.search" not in result.required_capabilities


def test_duplicate_capabilities_are_rejected() -> None:
    value = config()
    value["capabilities"] = ["repository.read", "repository.read"]

    with pytest.raises(WorkerConfigurationError, match="duplicates"):
        validate(value)


def test_input_mapping_is_not_mutated() -> None:
    value = config()
    original = deepcopy(value)

    validate(value)

    assert value == original


def test_unknown_policy_reference_is_rejected() -> None:
    value = config()
    value["policy"]["policy_ref"] = "unregistered"

    with pytest.raises(
        WorkerConfigurationError, match="unknown or unauthorized policy"
    ):
        validate(value)


def test_configuration_cannot_weaken_registered_approval_requirement() -> None:
    value = config()
    value["policy"]["approval_required"] = False

    result = validate(value)

    assert result.approval_required is True


def test_configuration_cannot_widen_registered_capabilities() -> None:
    value = config()
    value["policy"]["allowed_capabilities"].append("shell.execute")

    with pytest.raises(WorkerConfigurationError, match="does not allow"):
        validate(value)


def test_validated_metadata_is_deeply_immutable() -> None:
    result = validate(config())

    assert result.metadata["nested"]["items"] == ("keep",)
    with pytest.raises(TypeError):
        result.metadata["nested"]["new"] = "value"
    with pytest.raises(TypeError):
        result.metadata["nested"]["items"][0] = "changed"


def test_non_finite_metadata_numbers_are_rejected() -> None:
    value = config()
    value["metadata"] = {"cost": float("nan")}

    with pytest.raises(WorkerConfigurationError, match="finite JSON number"):
        validate(value)



def test_tool_input_schema_references_are_preserved_and_immutable() -> None:
    value = config()
    value["tools"][0]["input_schema_ref"] = "repository-read-input"

    result = validate(value)

    assert result.tool_input_schema_refs["repository.read"] == "repository-read-input"
    with pytest.raises(TypeError):
        result.tool_input_schema_refs["repository.read"] = "other-schema"


def test_duplicate_tool_capability_is_rejected() -> None:
    value = config()
    value["tools"].append(
        {"capability_ref": "repository.read", "required": False}
    )

    with pytest.raises(WorkerConfigurationError, match="duplicates a configured tool"):
        validate(value)
