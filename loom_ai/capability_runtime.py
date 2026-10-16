"""Fail-closed invocation boundary for configured Worker capabilities."""

from __future__ import annotations

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
) -> Any:
    """Invoke one capability only through a trusted runtime binding.

    A configuration reference is not itself executable authority. The caller
    must supply the runtime's trusted binding registry. When the validated
    policy requires approval, the caller must provide an explicit boolean
    approval result for this invocation. This function intentionally does not
    infer approval from config metadata or from a truthy non-boolean value.
    """
    if not isinstance(capability_ref, str) or not capability_ref.strip():
        raise CapabilityInvocationError("capability reference must be non-empty")
    if capability_ref not in configuration.capabilities:
        raise CapabilityInvocationError("capability is not authorized by Worker config")
    if configuration.approval_required:
        if approval_authorizer is None:
            raise CapabilityInvocationError(
                "capability invocation requires an approval authorizer"
            )
        try:
            approved = approval_authorizer(configuration, capability_ref, dict(arguments))
        except Exception as exc:
            raise CapabilityInvocationError("capability approval check failed") from exc
        if approved is not True:
            raise CapabilityInvocationError("capability invocation was not approved")
    binding = bindings.get(capability_ref)
    if binding is None or not callable(binding):
        raise CapabilityInvocationError("authorized capability has no runtime binding")
    if not isinstance(arguments, Mapping):
        raise CapabilityInvocationError("capability arguments must be an object")

    # Do not expose arbitrary binding exceptions as capability output or
    # evidence. Preserve the cause for trusted diagnostics at the caller.
    try:
        return binding(dict(arguments))
    except Exception as exc:
        raise CapabilityInvocationError("capability invocation failed") from exc
