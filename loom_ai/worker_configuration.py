"""Runtime validation and activation preflight for declarative Worker config.

This module validates the 0.1 language-neutral configuration shape and checks
references against caller-supplied registries. It deliberately does not resolve
credentials, contact model providers, or invoke capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping


class WorkerConfigurationError(ValueError):
    """Raised when Worker configuration is malformed or cannot be activated."""


@dataclass(frozen=True)
class WorkerLimits:
    """Enforceable resource bounds declared by a Worker configuration."""

    max_attempts: int
    max_duration_seconds: int
    max_output_bytes: int


@dataclass(frozen=True)
class ValidatedWorkerConfiguration:
    """Validated, non-secret configuration ready for a runtime to activate."""

    worker_id: str
    purpose: str
    gateway_ref: str
    selection_ref: str
    instructions: tuple[str, ...]
    capabilities: frozenset[str]
    required_capabilities: frozenset[str]
    policy_ref: str
    approval_required: bool
    limits: WorkerLimits
    metadata: Mapping[str, Any]


def validate_worker_configuration(
    value: Mapping[str, Any],
    *,
    gateway_selections: Mapping[str, set[str] | frozenset[str]],
    available_capabilities: set[str] | frozenset[str],
    policy_registry: Mapping[str, Mapping[str, Any]],
) -> ValidatedWorkerConfiguration:
    """Validate config structure and preflight gateway/capability authorization.

    The supplied registries are the runtime's trusted view of configured model
    gateways, model-selection profiles, authorized capabilities, and policies.
    The policy registry passed to this call must contain only policies this
    Worker is authorized to select. Unknown references fail closed.
    """
    if not isinstance(value, Mapping):
        raise WorkerConfigurationError("configuration must be an object")

    _keys(
        value,
        {
            "schema_version",
            "worker_id",
            "purpose",
            "model_gateway",
            "instructions",
            "tools",
            "capabilities",
            "policy",
            "limits",
            "metadata",
        },
        "configuration",
    )
    required = {
        "schema_version",
        "worker_id",
        "purpose",
        "model_gateway",
        "instructions",
        "capabilities",
        "policy",
        "limits",
    }
    missing = required - value.keys()
    if missing:
        raise WorkerConfigurationError(
            f"configuration missing required fields: {', '.join(sorted(missing))}"
        )
    if value["schema_version"] != "0.1":
        raise WorkerConfigurationError(
            "unsupported Worker configuration schema_version"
        )

    worker_id = _nonempty_string(value["worker_id"], "worker_id")
    purpose = _nonempty_string(value["purpose"], "purpose")

    gateway = value["model_gateway"]
    if not isinstance(gateway, Mapping):
        raise WorkerConfigurationError("model_gateway must be an object")
    _keys(gateway, {"gateway_ref", "selection_ref"}, "model_gateway")
    for key in ("gateway_ref", "selection_ref"):
        if key not in gateway:
            raise WorkerConfigurationError(
                f"model_gateway missing required field: {key}"
            )
    gateway_ref = _nonempty_string(gateway["gateway_ref"], "model_gateway.gateway_ref")
    selection_ref = _nonempty_string(
        gateway["selection_ref"], "model_gateway.selection_ref"
    )
    if gateway_ref not in gateway_selections:
        raise WorkerConfigurationError(
            f"unknown model gateway reference: {gateway_ref}"
        )
    if selection_ref not in gateway_selections[gateway_ref]:
        raise WorkerConfigurationError(
            f"unknown model selection reference for gateway {gateway_ref}: {selection_ref}"
        )

    instructions = _string_list(value["instructions"], "instructions", minimum=1)
    declared = _string_list(value["capabilities"], "capabilities", unique=True)
    tools = value.get("tools", [])
    if not isinstance(tools, list):
        raise WorkerConfigurationError("tools must be an array")
    tool_capabilities: set[str] = set()
    required_tools: set[str] = set()
    for index, tool in enumerate(tools):
        path = f"tools[{index}]"
        if not isinstance(tool, Mapping):
            raise WorkerConfigurationError(f"{path} must be an object")
        _keys(tool, {"capability_ref", "required", "input_schema_ref"}, path)
        if "capability_ref" not in tool or "required" not in tool:
            raise WorkerConfigurationError(
                f"{path} requires capability_ref and required"
            )
        capability = _nonempty_string(tool["capability_ref"], f"{path}.capability_ref")
        if not isinstance(tool["required"], bool):
            raise WorkerConfigurationError(f"{path}.required must be a boolean")
        if "input_schema_ref" in tool:
            _nonempty_string(tool["input_schema_ref"], f"{path}.input_schema_ref")
        tool_capabilities.add(capability)
        if tool["required"]:
            required_tools.add(capability)

    policy = value["policy"]
    if not isinstance(policy, Mapping):
        raise WorkerConfigurationError("policy must be an object")
    _keys(
        policy,
        {
            "policy_ref",
            "approval_required",
            "allowed_capabilities",
            "denied_capabilities",
        },
        "policy",
    )
    if "policy_ref" not in policy or "approval_required" not in policy:
        raise WorkerConfigurationError(
            "policy requires policy_ref and approval_required"
        )
    policy_ref = _nonempty_string(policy["policy_ref"], "policy.policy_ref")
    configured_approval = policy["approval_required"]
    if not isinstance(configured_approval, bool):
        raise WorkerConfigurationError("policy.approval_required must be a boolean")
    if policy_ref not in policy_registry:
        raise WorkerConfigurationError(
            f"unknown or unauthorized policy reference: {policy_ref}"
        )
    trusted_policy = policy_registry[policy_ref]
    if not isinstance(trusted_policy, Mapping):
        raise WorkerConfigurationError(
            f"registered policy {policy_ref} must be an object"
        )
    _keys(
        trusted_policy,
        {"approval_required", "allowed_capabilities", "denied_capabilities"},
        f"policy_registry[{policy_ref}]",
    )
    if "approval_required" not in trusted_policy:
        raise WorkerConfigurationError(
            f"registered policy {policy_ref} requires approval_required"
        )
    trusted_approval = trusted_policy["approval_required"]
    if not isinstance(trusted_approval, bool):
        raise WorkerConfigurationError(
            f"registered policy {policy_ref} approval_required must be a boolean"
        )
    configured_allowed = (
        set(_string_list(policy["allowed_capabilities"], "policy.allowed_capabilities"))
        if "allowed_capabilities" in policy
        else None
    )
    trusted_allowed = (
        set(
            _string_list(
                trusted_policy["allowed_capabilities"],
                f"policy_registry[{policy_ref}].allowed_capabilities",
            )
        )
        if "allowed_capabilities" in trusted_policy
        else None
    )
    if configured_allowed is not None and trusted_allowed is not None:
        widened = configured_allowed - trusted_allowed
        if widened:
            raise WorkerConfigurationError(
                "policy does not allow configured capabilities: "
                + ", ".join(sorted(widened))
            )
        allowed = configured_allowed & trusted_allowed
    else:
        allowed = configured_allowed if trusted_allowed is None else trusted_allowed
    denied = set(
        _string_list(
            policy.get("denied_capabilities", []), "policy.denied_capabilities"
        )
    )
    denied.update(
        _string_list(
            trusted_policy.get("denied_capabilities", []),
            f"policy_registry[{policy_ref}].denied_capabilities",
        )
    )
    approval_required = configured_approval or trusted_approval

    declared_set = set(declared)
    all_referenced = declared_set | tool_capabilities
    blocked = all_referenced & denied
    if blocked:
        raise WorkerConfigurationError(
            f"policy denies configured capabilities: {', '.join(sorted(blocked))}"
        )
    if allowed is not None:
        disallowed = all_referenced - allowed
        if disallowed:
            raise WorkerConfigurationError(
                f"policy does not allow configured capabilities: "
                f"{', '.join(sorted(disallowed))}"
            )

    available = set(available_capabilities)
    unavailable_required = (declared_set | required_tools) - available
    if unavailable_required:
        raise WorkerConfigurationError(
            "required capabilities are unavailable: "
            + ", ".join(sorted(unavailable_required))
        )

    limits = value["limits"]
    if not isinstance(limits, Mapping):
        raise WorkerConfigurationError("limits must be an object")
    _keys(
        limits,
        {"max_attempts", "max_duration_seconds", "max_output_bytes"},
        "limits",
    )
    limit_values: dict[str, int] = {}
    for key in ("max_attempts", "max_duration_seconds", "max_output_bytes"):
        if key not in limits:
            raise WorkerConfigurationError(f"limits missing required field: {key}")
        number = limits[key]
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise WorkerConfigurationError(f"limits.{key} must be a positive integer")
        limit_values[key] = number

    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise WorkerConfigurationError("metadata must be an object")

    return ValidatedWorkerConfiguration(
        worker_id=worker_id,
        purpose=purpose,
        gateway_ref=gateway_ref,
        selection_ref=selection_ref,
        instructions=tuple(instructions),
        capabilities=frozenset(all_referenced),
        required_capabilities=frozenset(declared_set | required_tools),
        policy_ref=policy_ref,
        approval_required=approval_required,
        limits=WorkerLimits(**limit_values),
        metadata=_freeze_metadata(metadata),
    )


def _keys(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise WorkerConfigurationError(
            f"{path} contains unknown fields: {', '.join(sorted(unknown))}"
        )


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkerConfigurationError(f"{path} must be a non-empty string")
    return value


def _string_list(
    value: Any, path: str, *, minimum: int = 0, unique: bool = False
) -> list[str]:
    if not isinstance(value, list):
        raise WorkerConfigurationError(f"{path} must be an array")
    if len(value) < minimum:
        raise WorkerConfigurationError(
            f"{path} must contain at least {minimum} item(s)"
        )
    values = [
        _nonempty_string(item, f"{path}[{index}]") for index, item in enumerate(value)
    ]
    if unique and len(set(values)) != len(values):
        raise WorkerConfigurationError(f"{path} must not contain duplicates")
    return values


def _freeze_metadata(value: Any, path: str = "metadata") -> Mapping[str, Any]:
    """Deep-freeze JSON-compatible metadata so validated config cannot drift."""
    if not isinstance(value, Mapping):
        raise WorkerConfigurationError(f"{path} must be an object")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise WorkerConfigurationError(f"{path} keys must be strings")
        frozen[key] = _freeze_metadata_value(item, f"{path}.{key}")
    return MappingProxyType(frozen)


def _freeze_metadata_value(value: Any, path: str) -> Any:
    """Freeze one JSON-compatible metadata value recursively."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise WorkerConfigurationError(f"{path} must be a finite JSON number")
        return value
    if isinstance(value, Mapping):
        return _freeze_metadata(value, path)
    if isinstance(value, list):
        return tuple(
            _freeze_metadata_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise WorkerConfigurationError(f"{path} must contain only JSON-compatible values")
