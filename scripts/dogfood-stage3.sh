#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$ROOT" ]] || { echo "FAIL: run inside the Loom checkout" >&2; exit 1; }
cd "$ROOT"

command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 is required" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' || { echo "FAIL: Python 3.11+ is required" >&2; exit 1; }

[[ -x .venv/bin/python ]] || python3 -m venv .venv
PY=.venv/bin/python
"$PY" -m pip install --quiet -e '.[dev]'

"$PY" - <<'PY'
import json
import tempfile
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

from loom_ai.arbiter import Arbiter, ArbiterDecision, WorkerEvaluation
from loom_ai.server import LoomServer, _LoomHTTPServer
from loom_ai.worker import WorkerContext, WorkerResult, WorkerStatus


class InspectWorker:
    worker_id = "inspect"

    def execute(self, context: WorkerContext) -> WorkerResult:
        path = Path(context.intent.provenance["task_path"])
        exists = path.exists()
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS if exists else WorkerStatus.FAILED,
            output={"path": str(path), "exists": exists},
            evidence=({"source": self.worker_id, "path": str(path)},),
            error="target does not exist" if not exists else "",
        )


class ImplementationWorker:
    worker_id = "implementation"

    def execute(self, context: WorkerContext) -> WorkerResult:
        path = Path(context.intent.provenance["task_path"])
        path.write_text(path.read_text() + "\nStage 3 dogfood marker.\n")
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={"path": str(path), "changed": True},
            evidence=({"source": self.worker_id, "path": str(path)},),
        )


class VerificationWorker:
    worker_id = "verification"

    def execute(self, context: WorkerContext) -> WorkerResult:
        path = Path(context.intent.provenance["task_path"])
        content = path.read_text()
        verified = "Stage 3 dogfood marker." in content
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS if verified else WorkerStatus.FAILED,
            output={"path": str(path), "verified": verified},
            evidence=({"source": self.worker_id, "verified": verified},),
            error="acceptance marker missing" if not verified else "",
        )


def build_arbiter() -> Arbiter:
    inspect = InspectWorker()
    implementation = ImplementationWorker()
    verification = VerificationWorker()

    def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
        if result.status is not WorkerStatus.SUCCESS:
            return WorkerEvaluation(
                ArbiterDecision.REPLAN, reason=result.error or "worker failed"
            )
        if result.worker_id == inspect.worker_id:
            return WorkerEvaluation(
                ArbiterDecision.REPLAN, workers=(implementation,)
            )
        if result.worker_id == implementation.worker_id:
            return WorkerEvaluation(
                ArbiterDecision.REPLAN, workers=(verification,)
            )
        return WorkerEvaluation(
            ArbiterDecision.COMPLETE, reason="Stage 3 acceptance verified"
        )

    return Arbiter([inspect], evaluate, max_retries=0)


with tempfile.TemporaryDirectory(prefix="loom-stage3-") as tmp:
    target = Path(tmp) / "README.txt"
    target.write_text("Stage 3 server task fixture.\n")

    server = LoomServer(build_arbiter(), host="127.0.0.1", port=0)
    instance = _LoomHTTPServer((server.host, server.port), server._handler_factory())
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{server.host}:{instance.server_address[1]}"
        with urlopen(f"{base}/health", timeout=5) as response:
            assert response.status == 200

        request = Request(
            f"{base}/intents",
            data=json.dumps(
                {
                    "title": "Stage 3 real task",
                    "goal": "Inspect, modify, and verify the task fixture through Loom.",
                    "acceptance": ["The Stage 3 dogfood marker is present."],
                    "intent_id": "stage3-server-task",
                    "provenance": {"task_path": str(target)},
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)

        assert payload["status"] == "success", payload
        workers = payload["output"]
        assert [item["worker_id"] for item in workers] == [
            "inspect",
            "implementation",
            "verification",
        ], payload
        assert all(item["status"] == "success" for item in workers), payload
        assert target.read_text().endswith("Stage 3 dogfood marker.\n")
        print("health: success")
        print("intent: success")
        print("inspect: success")
        print("implementation: success")
        print("verification: success")
        print("execution: success")
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)

print("RESULT: LOOM STAGE 3 SERVER TASK DOGFOOD PASSED")
PY
