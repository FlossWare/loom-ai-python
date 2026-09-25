#!/usr/bin/env python3
"""Run the minimal durable Loom server profile used by process-boundary dogfood.

This is a dogfood profile, not the default transport-smoke entrypoint. It
composes the public LoomServer HTTP boundary with FileExecutionStateStore,
Arbiter, a real repository-changing Worker, and a verification Worker.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from loom_ai import (
    Arbiter,
    ArbiterDecision,
    FileExecutionStateStore,
    LoomServer,
    WorkerContext,
    WorkerEvaluation,
    WorkerResult,
    WorkerStatus,
)

INITIAL_MARKER = "test_loom_dogfood_initial"
FOLLOWUP_MARKER = "test_loom_dogfood_followup"


class RepositoryTaskWorker:
    """Apply one bounded, observable change to a real checkout."""

    worker_id = "repository-task"

    def __init__(self, target: Path) -> None:
        self.target = target.resolve()

    def execute(self, context: WorkerContext) -> WorkerResult:
        text = self.target.read_text(encoding="utf-8")
        continued = context.state.get("phase") == "initial"
        if INITIAL_MARKER not in text:
            addition = (
                f"\n\n\ndef {INITIAL_MARKER}():\n"
                '    "Bounded Loom dogfood marker."\n'
                "    assert True\n"
            )
            self.target.write_text(
                text.rstrip() + addition, encoding="utf-8"
            )  # nosonar
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.SUCCESS,
                output="initial repository change applied",
                evidence=(
                    {"worker": self.worker_id, "phase": "initial", "changed": True},
                ),
            )
        if continued and FOLLOWUP_MARKER not in text:
            addition = (
                f"\n\n\ndef {FOLLOWUP_MARKER}():\n"
                '    "Follow-up after Loom process restart."\n'
                "    assert True\n"
            )
            self.target.write_text(
                text.rstrip() + addition, encoding="utf-8"
            )  # nosonar
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.SUCCESS,
                output="post-restart follow-up applied",
                evidence=(
                    {"worker": self.worker_id, "phase": "continued", "changed": True},
                ),
            )
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output="no repository change required",
            evidence=(
                {
                    "worker": self.worker_id,
                    "phase": "continued" if continued else "initial",
                    "changed": False,
                },
            ),
        )


class VerificationWorker:
    """Run repository verification as an ordinary Loom Worker."""

    worker_id = "verification"

    def __init__(self, target: Path) -> None:
        self.target = target.resolve()

    def execute(self, _context: WorkerContext) -> WorkerResult:
        import pytest

        target = str(self.target)
        return_code = pytest.main(["-q", target])
        return WorkerResult(
            worker_id=self.worker_id,
            status=(WorkerStatus.SUCCESS if return_code == 0 else WorkerStatus.FAILED),
            output=f"pytest exit code: {return_code}",
            evidence=(
                {
                    "worker": self.worker_id,
                    "returncode": return_code,
                },
            ),
        )


def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
    if result.worker_id == "verification":
        if result.successful:
            return WorkerEvaluation(
                ArbiterDecision.COMPLETE,
                reason="repository verified",
            )
        return WorkerEvaluation(
            ArbiterDecision.REPLAN,
            reason=result.error or "verification failed",
        )
    return WorkerEvaluation(ArbiterDecision.CONTINUE)


def build_server(
    *,
    target: Path,
    state_dir: Path,
    host: str,
    port: int,
) -> LoomServer:
    arbiter = Arbiter(
        [RepositoryTaskWorker(target), VerificationWorker(target)],
        evaluate,
        max_retries=0,
    )
    return LoomServer(
        arbiter,
        host=host,
        port=port,
        execution_store=FileExecutionStateStore(state_dir),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = build_server(
        target=args.target,
        state_dir=args.state_dir,
        host=args.host,
        port=args.port,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
