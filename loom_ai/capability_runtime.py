"""Fail-closed invocation boundary for configured Worker capabilities."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from loom_ai.worker_configuration import ValidatedWorkerConfiguration


class CapabilityInvocationError(RuntimeError):
    """Raised when a configured capability cannot safely be invoked."""


CapabilityBinding = Callable[[Mapping[str, Any]], Any]


def invoke_capability(
    configuration: ValidatedWorkerConfiguration,
    capability_ref: str,
    arguments: Mapping[str, Any],
    *,
    bindings: Mapping[str, CapabilityBinding],
    approval_authorizer: Callable[
        [ValidatedWorkerConfiguration, str, Mapping[str, Any]], bool
    ]
    | None = None,
    input_schema_validators: Mapping[
        str, Callable[[Mapping[str, Any]], bool]
    ]
    | None = None,
) -> Any:
    """Invoke one capability only through a trusted runtime binding.

    A configuration reference is not itself executable authority. The caller
    must supply the runtime's trusted binding registry. When the validated
    policy requires approval, a trusted runtime-supplied authorizer must
    explicitly approve this invocation. A request cannot bypass the check by
    carrying a caller-provided approval boolean.
    """
    if not isinstance(capability_ref, str) or not capability_ref.strip():
        raise CapabilityInvocationError("capability reference must be non-empty")
    if capability_ref not in configuration.capabilities:
        raise CapabilityInvocationError("capability is not authorized by Worker config")
    if capability_ref not in configuration.tool_capabilities:
        raise CapabilityInvocationError(
            "capability is not declared as an executable Worker tool"
        )
    if not isinstance(arguments, Mapping):
        raise CapabilityInvocationError("capability arguments must be an object")
    try:
        _assert_json_value(arguments)
        safe_arguments = json.loads(
            json.dumps(arguments, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError):
        raise CapabilityInvocationError(
            "capability arguments must contain only JSON-compatible values"
        ) from None
    if not isinstance(safe_arguments, dict):
        raise CapabilityInvocationError("capability arguments must be a JSON object")
    schema_ref = configuration.tool_input_schema_refs.get(capability_ref)
    if schema_ref is not None:
        validator = (input_schema_validators or {}).get(schema_ref)
        if validator is None or not callable(validator):
            raise CapabilityInvocationError(
                "configured tool input schema has no trusted validator"
            )
        try:
            valid_arguments = validator(safe_arguments)
        except Exception as exc:
            raise CapabilityInvocationError(
                "capability input validation failed"
            ) from exc
        if valid_arguments is not True:
            raise CapabilityInvocationError(
                "capability arguments do not satisfy the configured input schema"
            )
    if configuration.approval_required:
        if approval_authorizer is None:
            raise CapabilityInvocationError(
                "capability invocation requires an approval authorizer"
            )
        try:
            approved = approval_authorizer(
                configuration, capability_ref, safe_arguments
            )
        except Exception as exc:
            raise CapabilityInvocationError("capability approval check failed") from exc
        if approved is not True:
            raise CapabilityInvocationError("capability invocation was not approved")
    binding = bindings.get(capability_ref)
    if binding is None or not callable(binding):
        raise CapabilityInvocationError("authorized capability has no runtime binding")
    # Do not expose arbitrary binding exceptions as capability output or
    # evidence. Preserve the cause for trusted diagnostics at the caller.
    try:
        result = binding(safe_arguments)
    except Exception as exc:
        raise CapabilityInvocationError("capability invocation failed") from exc

    try:
        _assert_json_value(result)
        encoded_result = json.dumps(
            result, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise CapabilityInvocationError(
            "capability result must be JSON-compatible"
        ) from None
    if len(encoded_result) > configuration.limits.max_output_bytes:
        raise CapabilityInvocationError("capability result exceeds output byte limit")
    return json.loads(encoded_result.decode("utf-8"))


def _assert_json_value(value: Any, path: str = "$") -> None:
    """Reject Python values that JSON encoding would silently coerce."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{path} must be finite")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} object keys must be strings")
            _assert_json_value(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_value(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path} is not a JSON value")
