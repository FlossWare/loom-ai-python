"""Tests for the configured Worker capability invocation boundary."""

from __future__ import annotations

import pytest

from loom_ai.capability_runtime import (
    CapabilityInvocationError,
    invoke_capability,
)
from loom_ai.worker_configuration import validate_worker_configuration


def configuration(*, approval_required: bool = True, declare_tool: bool = True):
    raw = {
        "schema_version": "0.1",
        "worker_id": "bounded-worker",
        "purpose": "Run bounded tasks",
        "model_gateway": {"gateway_ref": "gateway", "selection_ref": "coding"},
        "instructions": ["Use approved capabilities only"],
        "tools": (
            [{"capability_ref": "repository.read", "required": True}]
            if declare_tool
            else []
        ),
        "capabilities": ["repository.read"],
        "policy": {"policy_ref": "safe", "approval_required": approval_required},
        "limits": {
            "max_attempts": 1,
            "max_duration_seconds": 30,
            "max_output_bytes": 1024,
        },
    }
    return validate_worker_configuration(
        raw,
        gateway_selections={"gateway": {"coding"}},
        available_capabilities={"repository.read"},
        policy_registry={
            "safe": {
                "approval_required": approval_required,
                "allowed_capabilities": ["repository.read"],
                "denied_capabilities": [],
            }
        },
    )


def test_capability_invocation_requires_explicit_approval() -> None:
    calls = []
    config = configuration()

    with pytest.raises(CapabilityInvocationError, match="not approved"):
        invoke_capability(
            config,
            "repository.read",
            {"path": "README.md"},
            bindings={"repository.read": lambda args: calls.append(args)},
            approval_authorizer=lambda *_: "yes",  # Must return literal True.
        )

    assert calls == []


def test_capability_invocation_rejects_undeclared_capability() -> None:
    with pytest.raises(CapabilityInvocationError, match="not authorized"):
        invoke_capability(
            configuration(approval_required=False),
            "shell.execute",
            {},
            bindings={"shell.execute": lambda args: "executed"},
        )


def test_capability_invocation_fails_closed_without_binding() -> None:
    with pytest.raises(CapabilityInvocationError, match="no runtime binding"):
        invoke_capability(
            configuration(approval_required=False),
            "repository.read",
            {},
            bindings={},
        )


def test_capability_invocation_passes_a_copy_of_arguments() -> None:
    config = configuration(approval_required=False)
    source = {"path": "README.md"}
    received = []

    result = invoke_capability(
        config,
        "repository.read",
        source,
        bindings={"repository.read": lambda args: received.append(args) or "contents"},
    )

    assert result == "contents"
    assert received == [{"path": "README.md"}]
    assert received[0] is not source


def test_binding_exception_is_wrapped_without_leaking_message() -> None:
    def broken(_arguments):
        raise RuntimeError("secret path / credentials / private data")

    with pytest.raises(CapabilityInvocationError, match="invocation failed") as exc:
        invoke_capability(
            configuration(approval_required=False),
            "repository.read",
            {},
            bindings={"repository.read": broken},
        )

    assert "secret path" not in str(exc.value)
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_capability_invocation_requires_trusted_authorizer() -> None:
    calls = []
    config = configuration()

    with pytest.raises(CapabilityInvocationError, match="approval authorizer"):
        invoke_capability(
            config,
            "repository.read",
            {"path": "README.md"},
            bindings={"repository.read": lambda args: calls.append(args)},
        )

    assert calls == []


def test_approved_capability_invocation_calls_binding() -> None:
    config = configuration()
    approved = []

    result = invoke_capability(
        config,
        "repository.read",
        {"path": "README.md"},
        bindings={"repository.read": lambda args: "contents:" + args["path"]},
        approval_authorizer=lambda _config, capability, _args: (
            approved.append(capability) or True
        ),
    )

    assert result == "contents:README.md"
    assert approved == ["repository.read"]


def test_declared_capability_without_tool_binding_is_not_executable() -> None:
    with pytest.raises(
        CapabilityInvocationError, match="not declared as an executable"
    ):
        invoke_capability(
            configuration(approval_required=False, declare_tool=False),
            "repository.read",
            {},
            bindings={"repository.read": lambda args: "must not run"},
        )


def test_capability_invocation_deep_copies_json_arguments() -> None:
    config = configuration(approval_required=False)
    source = {"nested": {"items": [1, 2]}}
    received = []

    invoke_capability(
        config,
        "repository.read",
        source,
        bindings={
            "repository.read": lambda args: received.append(args) or {"ok": True}
        },
    )

    assert received == [{"nested": {"items": [1, 2]}}]
    assert received[0] is not source
    assert received[0]["nested"] is not source["nested"]


def test_capability_invocation_rejects_non_json_arguments() -> None:
    with pytest.raises(CapabilityInvocationError, match="JSON-compatible"):
        invoke_capability(
            configuration(approval_required=False),
            "repository.read",
            {"not_json": object()},
            bindings={"repository.read": lambda args: "must not run"},
        )


def test_capability_invocation_rejects_non_json_result() -> None:
    with pytest.raises(CapabilityInvocationError, match="JSON-compatible"):
        invoke_capability(
            configuration(approval_required=False),
            "repository.read",
            {},
            bindings={"repository.read": lambda args: object()},
        )


def test_capability_invocation_rejects_oversized_result() -> None:
    config = configuration(approval_required=False)
    with pytest.raises(CapabilityInvocationError, match="output byte limit"):
        invoke_capability(
            config,
            "repository.read",
            {},
            bindings={"repository.read": lambda args: {"payload": "x" * 2048}},
        )


def test_capability_invocation_rejects_non_finite_result_numbers() -> None:
    with pytest.raises(CapabilityInvocationError, match="JSON-compatible"):
        invoke_capability(
            configuration(approval_required=False),
            "repository.read",
            {},
            bindings={"repository.read": lambda args: {"number": float("nan")}},
        )


def test_capability_invocation_rejects_non_string_json_object_keys() -> None:
    with pytest.raises(CapabilityInvocationError, match="JSON-compatible"):
        invoke_capability(
            configuration(approval_required=False),
            "repository.read",
            {1: "not a JSON object key"},
            bindings={"repository.read": lambda args: "must not run"},
        )


def test_capability_invocation_rejects_python_tuple_as_json_array() -> None:
    with pytest.raises(CapabilityInvocationError, match="JSON-compatible"):
        invoke_capability(
            configuration(approval_required=False),
            "repository.read",
            {"values": (1, 2)},
            bindings={"repository.read": lambda args: "must not run"},
        )


def test_capability_input_schema_requires_trusted_validator() -> None:
    config = configuration(approval_required=False)
    # Replace with a validated configuration that declares the schema.
    raw = {
        "schema_version": "0.1",
        "worker_id": "bounded-worker",
        "purpose": "Run bounded tasks",
        "model_gateway": {"gateway_ref": "gateway", "selection_ref": "coding"},
        "instructions": ["Use approved capabilities only"],
        "tools": [
            {
                "capability_ref": "repository.read",
                "required": True,
                "input_schema_ref": "repository-read-input",
            }
        ],
        "capabilities": ["repository.read"],
        "policy": {"policy_ref": "safe", "approval_required": False},
        "limits": {
            "max_attempts": 1,
            "max_duration_seconds": 30,
            "max_output_bytes": 1024,
        },
    }
    config = validate_worker_configuration(
        raw,
        gateway_selections={"gateway": {"coding"}},
        available_capabilities={"repository.read"},
        policy_registry={
            "safe": {
                "approval_required": False,
                "allowed_capabilities": ["repository.read"],
                "denied_capabilities": [],
            }
        },
    )
    calls = []
    with pytest.raises(CapabilityInvocationError, match="no trusted validator"):
        invoke_capability(
            config,
            "repository.read",
            {"path": "README.md"},
            bindings={"repository.read": lambda args: calls.append(args)},
        )
    assert calls == []


def test_capability_input_schema_rejects_invalid_arguments_before_execution() -> None:
    raw = {
        "schema_version": "0.1",
        "worker_id": "bounded-worker",
        "purpose": "Run bounded tasks",
        "model_gateway": {"gateway_ref": "gateway", "selection_ref": "coding"},
        "instructions": ["Use approved capabilities only"],
        "tools": [
            {
                "capability_ref": "repository.read",
                "required": True,
                "input_schema_ref": "repository-read-input",
            }
        ],
        "capabilities": ["repository.read"],
        "policy": {"policy_ref": "safe", "approval_required": False},
        "limits": {
            "max_attempts": 1,
            "max_duration_seconds": 30,
            "max_output_bytes": 1024,
        },
    }
    config = validate_worker_configuration(
        raw,
        gateway_selections={"gateway": {"coding"}},
        available_capabilities={"repository.read"},
        policy_registry={
            "safe": {
                "approval_required": False,
                "allowed_capabilities": ["repository.read"],
                "denied_capabilities": [],
            }
        },
    )
    calls = []
    with pytest.raises(CapabilityInvocationError, match="do not satisfy"):
        invoke_capability(
            config,
            "repository.read",
            {"path": 123},
            bindings={"repository.read": lambda args: calls.append(args)},
            input_schema_validators={
                "repository-read-input": lambda args: isinstance(args.get("path"), str)
            },
        )
    assert calls == []


def test_capability_input_schema_accepts_valid_arguments() -> None:
    raw = {
        "schema_version": "0.1",
        "worker_id": "bounded-worker",
        "purpose": "Run bounded tasks",
        "model_gateway": {"gateway_ref": "gateway", "selection_ref": "coding"},
        "instructions": ["Use approved capabilities only"],
        "tools": [
            {
                "capability_ref": "repository.read",
                "required": True,
                "input_schema_ref": "repository-read-input",
            }
        ],
        "capabilities": ["repository.read"],
        "policy": {"policy_ref": "safe", "approval_required": False},
        "limits": {
            "max_attempts": 1,
            "max_duration_seconds": 30,
            "max_output_bytes": 1024,
        },
    }
    config = validate_worker_configuration(
        raw,
        gateway_selections={"gateway": {"coding"}},
        available_capabilities={"repository.read"},
        policy_registry={
            "safe": {
                "approval_required": False,
                "allowed_capabilities": ["repository.read"],
                "denied_capabilities": [],
            }
        },
    )
    result = invoke_capability(
        config,
        "repository.read",
        {"path": "README.md"},
        bindings={"repository.read": lambda args: {"path": args["path"]}},
        input_schema_validators={
            "repository-read-input": lambda args: isinstance(args.get("path"), str)
        },
    )
    assert result == {"path": "README.md"}


def test_validators_and_approvers_cannot_mutate_executed_arguments() -> None:
    raw = {
        "schema_version": "0.1",
        "worker_id": "bounded-worker",
        "purpose": "Run bounded tasks",
        "model_gateway": {"gateway_ref": "gateway", "selection_ref": "coding"},
        "instructions": ["Use approved capabilities only"],
        "tools": [
            {
                "capability_ref": "repository.read",
                "required": True,
                "input_schema_ref": "repository-read-input",
            }
        ],
        "capabilities": ["repository.read"],
        "policy": {"policy_ref": "safe", "approval_required": True},
        "limits": {
            "max_attempts": 1,
            "max_duration_seconds": 30,
            "max_output_bytes": 1024,
        },
    }
    config = validate_worker_configuration(
        raw,
        gateway_selections={"gateway": {"coding"}},
        available_capabilities={"repository.read"},
        policy_registry={
            "safe": {
                "approval_required": True,
                "allowed_capabilities": ["repository.read"],
                "denied_capabilities": [],
            }
        },
    )
    executed = []

    def mutate_and_validate(arguments):
        arguments["path"] = "/etc/shadow"
        return True

    def mutate_and_approve(_config, _capability, arguments):
        arguments["path"] = "/private/unauthorized"
        return True

    invoke_capability(
        config,
        "repository.read",
        {"path": "README.md"},
        bindings={
            "repository.read": lambda arguments: (
                executed.append(arguments) or "contents"
            )
        },
        approval_authorizer=mutate_and_approve,
        input_schema_validators={"repository-read-input": mutate_and_validate},
    )

    assert executed == [{"path": "README.md"}]
