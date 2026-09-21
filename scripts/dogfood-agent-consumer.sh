#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/FlossWare/loom-ai-python.git"
REF="${LOOM_DOGFOOD_REF:-main}"
KEEP="${LOOM_DOGFOOD_KEEP:-0}"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/loom-agent.XXXXXX")"
ROOT="$WORKDIR/loom-ai-python"
trap '[[ "$KEEP" == 1 ]] || rm -rf "$WORKDIR"' EXIT

command -v git >/dev/null 2>&1 || fail "git is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' || fail "Python 3.11+ is required"

log "Cloning $REPO_URL@$REF"
git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$ROOT"
cd "$ROOT"

python3 -m venv .venv
PY=.venv/bin/python
"$PY" -m pip install --quiet -e '.[dev]'

export LOOM_AGENT_ROOT="$ROOT"
export LOOM_AGENT_PYTHON="$PY"

log "Launching real agent-consumer qualification"

"$PY" - <<'PY'
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

from loom_ai import (
    Arbiter,
    ArbiterDecision,
    Intent,
    LoomServer,
    WorkerContext,
    WorkerEvaluation,
    WorkerResult,
    WorkerStatus,
)

ROOT = Path(os.environ["LOOM_AGENT_ROOT"])
PYTHON = os.environ["LOOM_AGENT_PYTHON"]
TARGET = ROOT / "tests" / "test_worker_arbiter.py"
MARKER = "test_agent_consumer_dogfood"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)


class InspectWorker:
    worker_id = "inspect"

    def execute(self, context: WorkerContext) -> WorkerResult:
        text = TARGET.read_text()
        missing = MARKER not in text
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output={"target": str(TARGET), "marker_missing": missing},
            evidence=({"worker": self.worker_id, "marker_missing": missing},),
        )


class ImplementationWorker:
    worker_id = "implementation"

    def execute(self, context: WorkerContext) -> WorkerResult:
        inspect_evidence = next(
            (
                evidence
                for evidence in reversed(context.evidence)
                if evidence.get("worker") == "inspect"
            ),
            {},
        )
        if not inspect_evidence.get("marker_missing", False):
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.SUCCESS,
                output="target change already present",
                evidence=({"worker": self.worker_id, "changed": False},),
            )

        addition = f'''\n\n\ndef {MARKER}():\n    """Bounded real-agent dogfood regression test."""\n    assert True\n'''
        TARGET.write_text(TARGET.read_text().rstrip() + addition)
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output=f"added bounded regression test to {TARGET}",
            evidence=(
                {
                    "worker": self.worker_id,
                    "changed": True,
                    "artifact": str(TARGET),
                    "marker": MARKER,
                },
            ),
        )


class VerificationWorker:
    worker_id = "verification"

    def execute(self, context: WorkerContext) -> WorkerResult:
        result = run(PYTHON, "-m", "pytest", "-q", "tests/test_worker_arbiter.py")
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS if result.returncode == 0 else WorkerStatus.FAILED,
            output=result.stdout,
            evidence=(
                {
                    "worker": self.worker_id,
                    "returncode": result.returncode,
                    "artifact": str(TARGET),
                },
            ),
            error=result.stderr[-4000:],
        )


def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
    if not result.successful:
        return WorkerEvaluation(ArbiterDecision.REPLAN, reason=result.error or "worker failed")
    if result.worker_id == "verification":
        return WorkerEvaluation(ArbiterDecision.COMPLETE, reason="agent task verified")
    return WorkerEvaluation(ArbiterDecision.CONTINUE)


workers = [InspectWorker(), ImplementationWorker(), VerificationWorker()]
intent = Intent(
    title="Agent-consumer repository task",
    goal="Inspect the repository, make one bounded test-only change, and verify it.",
    requirements=(
        "Inspect before modifying the repository.",
        "Make only the bounded test-only change.",
        "Run the relevant repository tests after the change.",
    ),
    constraints=(
        "Do not modify Loom production code.",
        "Do not use an external model provider.",
    ),
    acceptance=(
        "The bounded regression test exists.",
        "The relevant repository tests pass.",
        "The final result contains worker and verification evidence.",
    ),
    provenance={"source": "agent-consumer-dogfood", "repository": str(ROOT)},
)

server = LoomServer(
    Arbiter(workers, evaluate, max_retries=0),
    port=0,
)
httpd = server.start()
thread = Thread(target=httpd.serve_forever, daemon=True)
thread.start()

try:
    # This is the application/agent consumer. It is a separate process and
    # knows only the public HTTP boundary, not Loom worker implementations.
    agent_code = r'''
import json
import os
import sys
from urllib.request import Request, urlopen

url = os.environ["LOOM_AGENT_URL"]
intent = json.loads(os.environ["LOOM_AGENT_INTENT"])
request = Request(
    url,
    data=json.dumps(intent).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request) as response:
    payload = json.load(response)

print(json.dumps(payload))
if payload.get("status") != "success":
    raise SystemExit("agent-consumer received unsuccessful Loom result")

if not payload.get("evidence"):
    raise SystemExit("agent-consumer received no top-level evidence")

workers = [item["worker_id"] for item in payload.get("output", [])]
expected = ["inspect", "implementation", "verification"]
if workers != expected:
    raise SystemExit(f"unexpected worker sequence: {workers!r}")

verification = next(
    (item for item in payload["evidence"] if item.get("worker") == "verification"),
    None,
)
if verification is None:
    raise SystemExit("agent-consumer received no verification evidence")
if verification.get("returncode") != 0:
    raise SystemExit(
        f"agent-consumer received failed verification evidence: {verification!r}"
    )
'''
    env = os.environ.copy()
    env["LOOM_AGENT_URL"] = f"http://{server.host}:{server.port}/intents"
    env["LOOM_AGENT_INTENT"] = json.dumps(
        {
            "title": intent.title,
            "goal": intent.goal,
            "requirements": list(intent.requirements),
            "constraints": list(intent.constraints),
            "acceptance": list(intent.acceptance),
            "provenance": intent.provenance,
        }
    )
    agent = subprocess.run(
        [PYTHON, "-c", agent_code],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    print(agent.stdout)
    if agent.returncode != 0:
        print(agent.stderr, file=sys.stderr)
        raise SystemExit(agent.returncode)

    final_text = TARGET.read_text()
    if MARKER not in final_text:
        raise SystemExit("agent-consumer did not produce the expected repository change")

    print("RESULT: LOOM AGENT-CONSUMER DOGFOOD PASSED")
    print("RESULT: real process -> HTTP -> LoomServer -> Arbiter -> Workers -> pytest")
finally:
    server.close()
    thread.join(timeout=2)
PY
