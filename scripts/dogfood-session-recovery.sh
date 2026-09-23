#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/FlossWare/loom-ai-python.git"
REF="${LOOM_DOGFOOD_REF:-main}"
KEEP="${LOOM_DOGFOOD_KEEP:-0}"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/loom-session.XXXXXX")"
ROOT="$WORKDIR/loom-ai-python"
STATE_DIR="$WORKDIR/state"
SERVER_SCRIPT="$WORKDIR/server.py"
SERVER_LOG="$WORKDIR/server.log"
PORT=18765
BASE_URL="http://127.0.0.1:$PORT"
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

TARGET="$ROOT/tests/test_server.py"
INITIAL_MARKER="test_session_recovery_initial"
FOLLOWUP_MARKER="test_session_recovery_followup"

cat > "$SERVER_SCRIPT" <<'PY'
from __future__ import annotations

import os
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

ROOT = Path(os.environ["LOOM_SESSION_ROOT"])
TARGET = ROOT / "tests" / "test_server.py"
INITIAL_MARKER = os.environ["LOOM_INITIAL_MARKER"]
FOLLOWUP_MARKER = os.environ["LOOM_FOLLOWUP_MARKER"]


class SessionWorker:
    worker_id = "session-task"

    def execute(self, context: WorkerContext) -> WorkerResult:
        text = TARGET.read_text()
        phase = any(
            item.get("type") == "execution-phase" and item.get("phase") == "initial"
            for item in context.evidence
        )
        if INITIAL_MARKER not in text:
            addition = (
                f"\n\ndef {INITIAL_MARKER}():\n"
                "    \\\"Bounded process-boundary qualification marker.\\\"\n"
                "    assert True\n"
            )
            TARGET.write_text(text.rstrip() + addition)
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.SUCCESS,
                output="initial repository change applied",
                evidence=(
                    {"worker": self.worker_id, "phase": "initial", "changed": True},
                ),
            )
        if phase and FOLLOWUP_MARKER not in text:
            addition = (
                f"\n\ndef {FOLLOWUP_MARKER}():\n"
                "    \\\"Follow-up after Loom process restart.\\\"\n"
                "    assert True\n"
            )
            TARGET.write_text(text.rstrip() + addition)
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
                {"worker": self.worker_id, "phase": "continued" if phase else "initial", "changed": False},
            ),
        )


class VerificationWorker:
    worker_id = "verification"

    def execute(self, context: WorkerContext) -> WorkerResult:
        import subprocess

        result = subprocess.run(
            [os.environ["LOOM_SESSION_PYTHON"], "-m", "pytest", "-q", "tests/test_server.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS if result.returncode == 0 else WorkerStatus.FAILED,
            output=result.stdout,
            error=result.stderr[-4000:],
            evidence=(
                {"worker": self.worker_id, "returncode": result.returncode},
            ),
        )


def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
    if result.worker_id == "verification":
        if result.successful:
            return WorkerEvaluation(ArbiterDecision.COMPLETE, reason="repository verified")
        return WorkerEvaluation(ArbiterDecision.REPLAN, reason=result.error or "verification failed")
    return WorkerEvaluation(ArbiterDecision.CONTINUE)


server = LoomServer(
    Arbiter([SessionWorker(), VerificationWorker()], evaluate, max_retries=0),
    host="127.0.0.1",
    port=int(os.environ["LOOM_SESSION_PORT"]),
    execution_store=FileExecutionStateStore(os.environ["LOOM_SESSION_STATE"]),
)
server.serve_forever()
PY

export LOOM_SESSION_ROOT="$ROOT"
export LOOM_SESSION_STATE="$STATE_DIR"
export LOOM_SESSION_PORT="$PORT"
export LOOM_SESSION_PYTHON="$PY"
export LOOM_INITIAL_MARKER="$INITIAL_MARKER"
export LOOM_FOLLOWUP_MARKER="$FOLLOWUP_MARKER"

start_server() {
  log "Starting Loom process"
  "$PY" "$SERVER_SCRIPT" >"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 100); do
    if "$PY" -c 'from urllib.request import urlopen; urlopen("http://127.0.0.1:18765/health", timeout=1)' >/dev/null 2>&1; then
      return
    fi
    sleep 0.1
  done
  cat "$SERVER_LOG" >&2
  fail "Loom process did not become healthy"
}

stop_server() {
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID"
    wait "$SERVER_PID" || true
  fi
}

start_server

log "External consumer submits the initial real-repository task"
INITIAL_JSON="$WORKDIR/initial.json"
"$PY" - "$BASE_URL" "$ROOT" "$INITIAL_JSON" <<'PY'
import json
import sys
from urllib.request import Request, urlopen

base_url, root, output = sys.argv[1:]
intent = {
    "title": "Process-boundary session recovery",
    "goal": "Make a bounded test-only repository change, then continue after Loom restarts.",
    "requirements": ["Inspect before modifying.", "Verify each phase."],
    "constraints": ["No model provider.", "No Loom production changes."],
    "acceptance": ["Initial marker exists.", "Follow-up marker exists.", "Tests pass."],
    "provenance": {"source": "dogfood-1010", "repository": root},
}
request = Request(
    f"{base_url}/intents",
    data=json.dumps(intent).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request) as response:
    payload = json.load(response)
if payload["status"] != "success":
    raise SystemExit(f"initial execution failed: {payload}")
with open(output, "w", encoding="utf-8") as stream:
    json.dump(payload, stream)
print(json.dumps(payload))
PY

EXECUTION_ID="$("$PY" - "$INITIAL_JSON" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["execution_id"])
PY
)"

grep -q "$INITIAL_MARKER" "$TARGET" || fail "initial repository change missing"

log "Terminating Loom process"
stop_server

log "Starting a fresh Loom process with the same durable state"
start_server

log "External consumer retrieves execution state after restart"
"$PY" - "$BASE_URL" "$EXECUTION_ID" <<'PY'
import json
import sys
from urllib.request import urlopen

with urlopen(f"{sys.argv[1]}/executions/{sys.argv[2]}") as response:
    payload = json.load(response)
assert payload["execution_id"] == sys.argv[2]
assert payload["status"] == "success"
phases = [x["phase"] for x in payload["evidence"] if x.get("type") == "execution-phase"]
assert phases == ["initial"], phases
print(json.dumps(payload))
PY

log "External consumer continues without replaying the original transcript"
"$PY" - "$BASE_URL" "$EXECUTION_ID" <<'PY'
import json
import sys
from urllib.request import Request, urlopen

request = Request(
    f"{sys.argv[1]}/executions/{sys.argv[2]}/continue",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request) as response:
    payload = json.load(response)
if payload["status"] != "success":
    raise SystemExit(f"continuation failed: {payload}")
print(json.dumps(payload))
PY

grep -q "$FOLLOWUP_MARKER" "$TARGET" || fail "post-restart follow-up change missing"

log "External consumer verifies durable provenance and final state"
"$PY" - "$BASE_URL" "$EXECUTION_ID" "$TARGET" <<'PY'
import json
import sys
from urllib.request import urlopen

with urlopen(f"{sys.argv[1]}/executions/{sys.argv[2]}") as response:
    payload = json.load(response)
assert payload["execution_id"] == sys.argv[2]
assert payload["status"] == "success"
phases = [x["phase"] for x in payload["evidence"] if x.get("type") == "execution-phase"]
assert phases == ["initial", "continued"], phases
assert payload["provenance"]["source"] == "dogfood-1010"
text = open(sys.argv[3], encoding="utf-8").read()
assert "test_session_recovery_initial" in text
assert "test_session_recovery_followup" in text
print("RESULT: LOOM SESSION RECOVERY DOGFOOD PASSED")
print("RESULT: external consumer -> HTTP -> persist -> process termination -> restart -> retrieve -> continue -> verify")
PY

stop_server
