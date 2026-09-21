#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${LOOM_DOGFOOD_REPO:-https://github.com/FlossWare/loom-ai-python.git}"
REF="${LOOM_DOGFOOD_REF:-main}"
KEEP="${LOOM_DOGFOOD_KEEP:-0}"

command -v git >/dev/null || { echo "FAIL: git is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "FAIL: python3 is required" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' ||
  { echo "FAIL: Python 3.11+ is required" >&2; exit 1; }

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/loom-failure-recovery.XXXXXX")"
trap '[[ "$KEEP" == 1 ]] || rm -rf "$WORKDIR"' EXIT
ROOT="$WORKDIR/loom-ai-python"

echo "==> Cloning real repository $REPO_URL@$REF"
git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$ROOT"
cd "$ROOT"

python3 -m venv .venv
PY=.venv/bin/python
"$PY" -m pip install --quiet -e '.[dev]'

echo "==> Creating bounded task in the real repository checkout"
cat > loom_ai/dogfood_recovery_target.py <<'PY'
def expected_value() -> int:
    return 42
PY
cat > tests/test_dogfood_recovery_target.py <<'PY'
from loom_ai.dogfood_recovery_target import expected_value


def test_expected_value() -> None:
    assert expected_value() == 42
PY
git add loom_ai/dogfood_recovery_target.py tests/test_dogfood_recovery_target.py
git -c user.name='Loom Dogfood' -c user.email='loom@example.invalid'   commit -q -m 'dogfood: add verification target'

"$PY" - "$ROOT" <<'PY'
import json
import pathlib
import subprocess
import sys
import threading
import urllib.request

from loom_ai import (
    Arbiter, ArbiterDecision, Intent, Worker, WorkerContext,
    WorkerEvaluation, WorkerResult, WorkerStatus,
)
from loom_ai.server import LoomServer


class InspectWorker(Worker):
    worker_id = "inspect"

    def execute(self, context):
        root = pathlib.Path(context.intent.provenance["repository"])
        target = root / "loom_ai/dogfood_recovery_target.py"
        ok = target.exists() and (root / ".git").is_dir()
        return WorkerResult(
            self.worker_id, WorkerStatus.SUCCESS if ok else WorkerStatus.FAILED,
            {"target": str(target), "git_repository": (root / ".git").is_dir()},
            evidence=({"worker": self.worker_id},),
            error="" if ok else "task target missing",
        )


class ImplementWorker(Worker):
    worker_id = "implement"

    def execute(self, context):
        target = pathlib.Path(context.intent.provenance["repository"]) / "loom_ai/dogfood_recovery_target.py"
        target.write_text("def expected_value() -> int:\n    return 41\n", encoding="utf-8")
        return WorkerResult(
            self.worker_id, WorkerStatus.SUCCESS,
            {"changed": True, "deliberately_invalid": True},
            evidence=({"worker": self.worker_id, "attempt": 1},),
        )


class VerifyWorker(Worker):
    worker_id = "verify"

    def __init__(self):
        self.attempt = 0

    def execute(self, context):
        self.attempt += 1
        root = pathlib.Path(context.intent.provenance["repository"])
        pycache = root / "loom_ai" / "__pycache__"
        for pyc in pycache.glob("dogfood_recovery_target*.pyc"):
            pyc.unlink(missing_ok=True)
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_dogfood_recovery_target.py"],
            cwd=root, capture_output=True, text=True, check=False,
        )
        return WorkerResult(
            self.worker_id,
            WorkerStatus.SUCCESS if run.returncode == 0 else WorkerStatus.FAILED,
            {"returncode": run.returncode, "attempt": self.attempt},
            error="" if run.returncode == 0 else "verification failed",
            evidence=({"worker": self.worker_id, "attempt": self.attempt,
                       "returncode": run.returncode},),
        )


class RecoverWorker(Worker):
    worker_id = "recover"

    def execute(self, context):
        target = pathlib.Path(context.intent.provenance["repository"]) / "loom_ai/dogfood_recovery_target.py"
        target.write_text("def expected_value() -> int:\n    return 42\n", encoding="utf-8")
        return WorkerResult(
            self.worker_id, WorkerStatus.SUCCESS,
            {"changed": True, "recovered": True},
            evidence=({"worker": self.worker_id, "recovered_after_verification_failure": True},),
        )


verify = VerifyWorker()


def evaluate(result, _context):
    if result.worker_id == "verify":
        if result.successful:
            return WorkerEvaluation(ArbiterDecision.COMPLETE, reason="verification passed")
        if verify.attempt == 1:
            return WorkerEvaluation(
                ArbiterDecision.REPLAN,
                workers=(RecoverWorker(), verify),
                reason="verification failed; schedule recovery",
            )
        return WorkerEvaluation(ArbiterDecision.REPLAN, reason="verification failed twice")
    return WorkerEvaluation(ArbiterDecision.CONTINUE)


root = pathlib.Path(sys.argv[1])
intent = Intent(
    goal="Perform a bounded repository change and recover from verification failure",
    requirements=["inspect", "implement", "verify", "recover", "verify"],
    acceptance=["verification passes after an explicit failed attempt"],
    provenance={"repository": str(root)},
)
arbiter = Arbiter(
    [InspectWorker(), ImplementWorker(), verify],
    evaluate,
    max_retries=0,
)
server = LoomServer(arbiter, port=0)
server.start()
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()

try:
    body = json.dumps({
        "title": "Failure recovery dogfood",
        "goal": intent.goal,
        "requirements": intent.requirements,
        "acceptance": intent.acceptance,
        "provenance": intent.provenance,
    }).encode()
    request = urllib.request.Request(
        f"http://{server.host}:{server.port}/intents",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)

    print(json.dumps(payload, indent=2))
    assert payload["status"] == "success", payload
    outputs = payload["output"]
    assert [x["worker_id"] for x in outputs] == ["inspect", "implement", "verify", "recover", "verify"], payload
    assert outputs[2]["status"] == "failed", payload
    assert outputs[2]["evidence"][0]["attempt"] == 1, payload
    assert outputs[4]["status"] == "success", payload
    assert outputs[4]["evidence"][0]["attempt"] == 2, payload
    assert outputs[3]["output"]["recovered"] is True, payload
    assert "return 42" in (root / "loom_ai/dogfood_recovery_target.py").read_text(encoding="utf-8")
    print("RESULT: LOOM FAILURE-RECOVERY DOGFOOD PASSED")
finally:
    server.close()
    thread.join(timeout=5)
PY
