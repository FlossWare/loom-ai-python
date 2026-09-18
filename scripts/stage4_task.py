#!/usr/bin/env python3
"""Run a deterministic Stage 4 repository task through Loom's HTTP boundary."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import threading
import urllib.request

from loom_ai import (
    Arbiter,
    ArbiterDecision,
    Intent,
    Worker,
    WorkerContext,
    WorkerEvaluation,
    WorkerResult,
    WorkerStatus,
)
from loom_ai.server import LoomServer


class InspectWorker(Worker):
    worker_id = "inspect"

    def execute(self, context: WorkerContext) -> WorkerResult:
        path = pathlib.Path(context.intent.provenance["task_path"])
        text = path.read_text(encoding="utf-8")
        return WorkerResult(
            self.worker_id,
            WorkerStatus.SUCCESS,
            {
                "path": str(path),
                "git_repository": (path.parent / ".git").is_dir(),
                "contains_return_41": "return 41" in text,
            },
            evidence=({"message": "inspected repository task file"},),
        )


class PlanWorker(Worker):
    worker_id = "plan"

    def execute(self, context: WorkerContext) -> WorkerResult:
        if not any(
            item.get("message") == "inspected repository task file"
            for item in context.evidence
        ):
            return WorkerResult(
                self.worker_id,
                WorkerStatus.FAILED,
                error="inspect evidence is missing",
            )
        return WorkerResult(
            self.worker_id,
            WorkerStatus.SUCCESS,
            {"replacement": "return 42"},
            evidence=({"message": "planned replacement of return 41 with return 42"},),
        )


class ImplementationWorker(Worker):
    worker_id = "implement"

    def execute(self, context: WorkerContext) -> WorkerResult:
        path = pathlib.Path(context.intent.provenance["task_path"])
        if not any(
            item.get("message") == "planned replacement of return 41 with return 42"
            for item in context.evidence
        ):
            return WorkerResult(
                self.worker_id, WorkerStatus.FAILED, error="plan evidence is missing"
            )
        text = path.read_text(encoding="utf-8")
        updated = text.replace("return 41", "return 42")
        if updated == text:
            return WorkerResult(
                self.worker_id,
                WorkerStatus.FAILED,
                error="planned replacement was not present",
            )
        path.write_text(updated, encoding="utf-8")
        return WorkerResult(
            self.worker_id,
            WorkerStatus.SUCCESS,
            {"changed": True},
            evidence=({"message": "implementation applied"},),
        )


class VerificationWorker(Worker):
    worker_id = "verify"

    def execute(self, context: WorkerContext) -> WorkerResult:
        path = pathlib.Path(context.intent.provenance["task_path"])
        text = path.read_text(encoding="utf-8")
        accepted = "return 42" in text and "return 41" not in text
        return WorkerResult(
            self.worker_id,
            WorkerStatus.SUCCESS if accepted else WorkerStatus.FAILED,
            {"acceptance": accepted},
            error="" if accepted else "acceptance condition failed",
            evidence=(
                ({"message": "verified repository task result"},) if accepted else ()
            ),
        )


def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
    if not result.successful:
        return WorkerEvaluation(
            ArbiterDecision.RETRY,
            reason=result.error or f"{result.worker_id} failed",
        )
    if result.worker_id == "verify":
        return WorkerEvaluation(
            ArbiterDecision.COMPLETE,
            reason="repository task verified",
        )
    return WorkerEvaluation(ArbiterDecision.CONTINUE)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: stage4_task.py TASK_PATH")

    task_path = pathlib.Path(sys.argv[1]).resolve()
    intent = Intent(
        goal="Update the repository task fixture and verify the result",
        requirements=["inspect", "plan", "implement", "verify"],
        constraints=["modify only the task file"],
        acceptance=["the task file contains return 42 and not return 41"],
        provenance={"task_path": str(task_path)},
    )
    arbiter = Arbiter(
        workers=[
            InspectWorker(),
            PlanWorker(),
            ImplementationWorker(),
            VerificationWorker(),
        ],
        evaluator=evaluate,
        max_retries=0,
    )
    server = LoomServer(arbiter, port=0)
    server.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps(
            {
                "goal": intent.goal,
                "requirements": intent.requirements,
                "constraints": intent.constraints,
                "acceptance": intent.acceptance,
                "provenance": intent.provenance,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://{server.host}:{server.port}/intents",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
            print(json.dumps(payload))

        if payload["status"] == "success":
            subprocess.run(
                ["git", "-C", str(task_path.parent), "diff", "--check"],
                check=True,
            )
    finally:
        server.close()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
