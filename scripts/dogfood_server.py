#!/usr/bin/env python3
"""Run the durable Loom server profile used by process-boundary dogfooding.

This profile is separate from the default transport-smoke entrypoint. It
composes the public HTTP boundary, FileExecutionStateStore, Arbiter, a bounded
repository-changing Worker, and a verification Worker.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
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
    """Apply one bounded, observable change to the current task checkout."""

    worker_id = "repository-task"

    def execute(self, context: WorkerContext) -> WorkerResult:
        root = Path.cwd().resolve()
        target = (root / "tests" / "test_server.py").resolve()
        if target.parent != root / "tests":
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.FAILED,
                error="dogfood target escaped the task checkout",
            )

        try:
            text = target.read_text(encoding="utf-8")
            continued = context.state.get("phase") == "initial"
            if INITIAL_MARKER not in text:
                addition = (
                    f"\n\n\ndef {INITIAL_MARKER}():\n"
                    '    "Bounded Loom dogfood marker."\n'
                    "    assert True\n"
                )
                target.write_text(text.rstrip() + addition, encoding="utf-8")
                return WorkerResult(
                    worker_id=self.worker_id,
                    status=WorkerStatus.SUCCESS,
                    output={"phase": "initial", "changed": True},
                    evidence=(
                        {
                            "worker": self.worker_id,
                            "phase": "initial",
                            "changed": True,
                            "target": str(target),
                        },
                    ),
                )

            if continued and FOLLOWUP_MARKER not in text:
                addition = (
                    f"\n\n\ndef {FOLLOWUP_MARKER}():\n"
                    '    "Follow-up after Loom process restart."\n'
                    "    assert True\n"
                )
                target.write_text(text.rstrip() + addition, encoding="utf-8")
                return WorkerResult(
                    worker_id=self.worker_id,
                    status=WorkerStatus.SUCCESS,
                    output={"phase": "continued", "changed": True},
                    evidence=(
                        {
                            "worker": self.worker_id,
                            "phase": "continued",
                            "changed": True,
                            "target": str(target),
                        },
                    ),
                )
        except OSError as exc:
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.FAILED,
                error=f"repository task failed: {exc}",
            )

        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={"phase": "continued" if continued else "initial", "changed": False},
            evidence=(
                {
                    "worker": self.worker_id,
                    "phase": "continued" if continued else "initial",
                    "changed": False,
                    "target": str(target),
                },
            ),
        )


class VerificationWorker:
    """Run repository verification in an isolated child process."""

    worker_id = "verification"

    def execute(self, _context: WorkerContext) -> WorkerResult:
        target = Path.cwd().resolve() / "tests" / "test_server.py"
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(target)],
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.FAILED,
                error="repository verification timed out",
            )
        except OSError as exc:
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.FAILED,
                error=f"verification could not run: {exc}",
            )

        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS
            if completed.returncode == 0
            else WorkerStatus.FAILED,
            output={"pytest_exit_code": completed.returncode},
            evidence=(
                {
                    "worker": self.worker_id,
                    "returncode": completed.returncode,
                    "target": str(target),
                },
            ),
            error=""
            if completed.returncode == 0
            else "repository verification failed",
        )


def evaluate(
    result: WorkerResult, _context: WorkerContext
) -> WorkerEvaluation:
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
    if result.successful:
        return WorkerEvaluation(ArbiterDecision.CONTINUE)
    return WorkerEvaluation(
        ArbiterDecision.REPLAN,
        reason=result.error or "repository task failed",
    )


def build_server(*, host: str, port: int) -> LoomServer:
    """Build the durable process-boundary qualification server."""
    arbiter = Arbiter(
        [RepositoryTaskWorker(), VerificationWorker()],
        evaluate,
        max_retries=0,
    )
    return LoomServer(
        arbiter,
        host=host,
        port=port,
        execution_store=FileExecutionStateStore(Path(".loom-dogfood-state")),
    )


def main() -> None:
    """Run the profile against the current working directory as the task repo."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    build_server(host=args.host, port=args.port).serve_forever()


if __name__ == "__main__":
    main()
