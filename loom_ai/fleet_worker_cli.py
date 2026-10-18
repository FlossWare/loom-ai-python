"""Command-line runner for an independently deployed Loom fleet Worker."""

from __future__ import annotations

import argparse
import importlib
import os
from typing import Any

from loom_ai.fleet_http import FleetWorkerAgent


def load_worker(reference: str) -> Any:
    """Load a Worker instance or zero-argument Worker class from module:attribute."""
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("worker must be specified as module:attribute")
    module = importlib.import_module(module_name)
    candidate = getattr(module, attribute_name)
    worker = candidate() if isinstance(candidate, type) else candidate
    worker_id = getattr(worker, "worker_id", None)
    execute = getattr(worker, "execute", None)
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise TypeError("loaded Worker must expose a non-empty worker_id")
    if not callable(execute):
        raise TypeError("loaded Worker must expose execute(context)")
    return worker


def main() -> None:
    """Run a Worker until interrupted, draining it before exit."""
    parser = argparse.ArgumentParser(
        description="Run an independently deployed Loom fleet Worker"
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("LOOM_FLEET_URL", "http://127.0.0.1:8001"),
        help="fleet HTTP base URL (default: LOOM_FLEET_URL or loopback:8001)",
    )
    parser.add_argument(
        "--worker",
        required=True,
        help="Worker instance or class reference in module:attribute form",
    )
    parser.add_argument("--implementation-id", required=True)
    parser.add_argument(
        "--capability",
        action="append",
        required=True,
        help="capability to advertise; repeat for multiple capabilities",
    )
    parser.add_argument("--lease-seconds", type=int, default=15)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument(
        "--token-env",
        default="LOOM_FLEET_TOKEN",
        help="environment variable containing the bearer token",
    )
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"{args.token_env} must contain the fleet bearer token")
    try:
        worker = load_worker(args.worker)
        agent = FleetWorkerAgent(
            args.url,
            token,
            worker,
            implementation_id=args.implementation_id,
            capabilities=args.capability,
            lease_seconds=args.lease_seconds,
            poll_interval_seconds=args.poll_interval,
        )
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
