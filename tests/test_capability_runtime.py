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
    with pytest.raises(CapabilityInvocationError, match="not declared as an executable"):
        invoke_capability(
            configuration(approval_required=False, declare_tool=False),
            "repository.read",
            {},
            bindings={"repository.read": lambda args: "must not run"},
        )
