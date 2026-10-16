#!/usr/bin/env bash
set -euo pipefail

RUNTIME_URL="https://github.com/FlossWare/loom-ai-python.git"
TASK_URL="https://github.com/FlossWare/loom-ai.git"
RUNTIME_REF="${LOOM_DOGFOOD_REF:-main}"
TASK_REF="${LOOM_DOGFOOD_TASK_REF:-main}"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/loom-dogfood.XXXXXX")"
RUNTIME="$WORKDIR/runtime"
TASK="$WORKDIR/task"
PORT="${LOOM_DOGFOOD_PORT:-18766}"
BASE="http://127.0.0.1:$PORT"
LOG="$WORKDIR/server.log"
PY="$RUNTIME/.venv/bin/python"
SERVER_PID=""

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }
cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

command -v git >/dev/null 2>&1 || fail "git is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' || fail "Python 3.11+ is required"

log "Cloning Loom runtime ref $RUNTIME_REF and task repository ref $TASK_REF"
git clone --quiet --depth 1 --branch "$RUNTIME_REF" "$RUNTIME_URL" "$RUNTIME"
git clone --quiet --depth 1 --branch "$TASK_REF" "$TASK_URL" "$TASK"

cd "$RUNTIME"
python3 -m venv .venv
"$PY" -m pip install --quiet -e '.[dev]'

TARGET="$TASK/tests/test_server.py"
log "Bootstrap a real task target in the fresh checkout"
mkdir -p "$(dirname "$TARGET")"
cat >"$TARGET" <<'PY'
def test_loom_dogfood_baseline():
    assert True
PY

start_server() {
  log "Starting durable dogfood server profile"
  (
    cd "$TASK"
    export PYTHONPATH="$RUNTIME"
    exec "$PY" "$RUNTIME/scripts/dogfood_server.py" --host 127.0.0.1 --port "$PORT"
  ) >"$LOG" 2>&1 &
  SERVER_PID=$!

  for _ in $(seq 1 100); do
    if "$PY" -c "from urllib.request import urlopen; urlopen('$BASE/health', timeout=1)" >/dev/null 2>&1; then
      return
    fi
    sleep 0.1
  done
  cat "$LOG" >&2
  fail "dogfood server did not become healthy"
}

stop_server() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID"
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=""
}

start_server

log "Submit a repository task through the public HTTP boundary"
INITIAL="$WORKDIR/initial.json"
"$PY" - "$BASE" "$INITIAL" <<'PY'
import json
import sys
from urllib.request import Request, urlopen

base, output = sys.argv[1:]
intent = {
    "title": "Durable Loom dogfood",
    "goal": "Apply a bounded repository change and verify it.",
    "requirements": ["Make the change in the task checkout.", "Run repository verification."],
    "constraints": ["Use the public HTTP boundary.", "Do not access Loom internals from the consumer."],
    "acceptance": ["Initial marker exists.", "Follow-up marker exists.", "Verification passes."],
    "provenance": {"source": "durable-dogfood"},
}
request = Request(
    f"{base}/intents",
    data=json.dumps(intent).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request, timeout=30) as response:
    payload = json.load(response)
assert payload["status"] == "success", payload
assert payload["execution_id"], payload
workers = payload["output"]
assert [worker["worker_id"] for worker in workers] == ["repository-task", "verification"], payload
assert all(worker["status"] == "success" for worker in workers), payload
evidence_types = [item.get("type") for item in payload["evidence"]]
assert "worker-result" in evidence_types, payload
json.dump(payload, open(output, "w", encoding="utf-8"))
print(json.dumps(payload))
PY

EXECUTION_ID="$("$PY" - "$INITIAL" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["execution_id"])
PY
)"

grep -q "test_loom_dogfood_initial" "$TARGET" || fail "initial repository change missing"

log "Terminate the server process"
stop_server

log "Restart Loom from a fresh process"
start_server

log "Observe the same execution identity after restart"
"$PY" - "$BASE" "$EXECUTION_ID" <<'PY'
import json
import sys
from urllib.request import urlopen

with urlopen(f"{sys.argv[1]}/executions/{sys.argv[2]}", timeout=5) as response:
    payload = json.load(response)
assert payload["execution_id"] == sys.argv[2], payload
assert payload["status"] == "success", payload
assert payload["provenance"]["source"] == "durable-dogfood", payload
phases = [item["phase"] for item in payload["evidence"] if item.get("type") == "execution-phase"]
assert phases == ["initial"], phases
print(json.dumps(payload))
PY

log "Continue the same execution without replaying a transcript"
"$PY" - "$BASE" "$EXECUTION_ID" <<'PY'
import json
import sys
from urllib.request import Request, urlopen

request = Request(
    f"{sys.argv[1]}/executions/{sys.argv[2]}/continue",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request, timeout=30) as response:
    payload = json.load(response)
assert payload["execution_id"] == sys.argv[2], payload
assert payload["status"] == "success", payload
workers = payload["output"]
assert [worker["worker_id"] for worker in workers] == ["repository-task", "verification"], payload
print(json.dumps(payload))
PY

grep -q "test_loom_dogfood_followup" "$TARGET" || fail "post-restart follow-up change missing"

log "Verify final durable state and repository contents"
"$PY" - "$BASE" "$EXECUTION_ID" "$TARGET" <<'PY'
import json
import sys
from urllib.request import urlopen

with urlopen(f"{sys.argv[1]}/executions/{sys.argv[2]}", timeout=5) as response:
    payload = json.load(response)
assert payload["execution_id"] == sys.argv[2], payload
assert payload["status"] == "success", payload
assert payload["provenance"]["source"] == "durable-dogfood", payload
phases = [item["phase"] for item in payload["evidence"] if item.get("type") == "execution-phase"]
assert phases == ["initial", "continued"], phases
with open(sys.argv[3], encoding="utf-8") as handle:
    source = handle.read()
assert "test_loom_dogfood_initial" in source
assert "test_loom_dogfood_followup" in source
print("RESULT: durable dogfood passed")
print("RESULT: submit -> persist -> verify -> terminate -> restart -> observe -> continue -> verify")
PY

stop_server
