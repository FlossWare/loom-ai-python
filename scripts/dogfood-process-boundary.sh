#!/usr/bin/env bash
set -euo pipefail

RUNTIME_URL="https://github.com/FlossWare/loom-ai-python.git"
TASK_URL="https://github.com/FlossWare/loom-ai.git"
REF="${LOOM_DOGFOOD_REF:-main}"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/loom-dogfood.XXXXXX")"
RUNTIME="$WORKDIR/runtime"
TASK="$WORKDIR/task"
STATE="$WORKDIR/state"
PORT="${LOOM_DOGFOOD_PORT:-18766}"
BASE="http://127.0.0.1:$PORT"
LOG="$WORKDIR/server.log"
PY="$RUNTIME/.venv/bin/python"
SERVER_PID=""

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }
cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" || true
    wait "$SERVER_PID" || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

command -v git >/dev/null 2>&1 || fail "git is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

log "Cloning Loom runtime and real task repository"
git clone --quiet --depth 1 --branch "$REF" "$RUNTIME_URL" "$RUNTIME"
git clone --quiet --depth 1 --branch "$REF" "$TASK_URL" "$TASK"

cd "$RUNTIME"
python3 -m venv .venv
"$PY" -m pip install --quiet -e '.[dev]'

TARGET="$TASK/tests/test_server.py"

start_server() {
  log "Starting durable dogfood server profile"
  "$PY" scripts/dogfood-server.py \
    --root "$TASK" \
    --target "$TARGET" \
    --state-dir "$STATE" \
    --python "$PY" \
    --host 127.0.0.1 \
    --port "$PORT" >"$LOG" 2>&1 &
  SERVER_PID=$
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
    wait "$SERVER_PID" || true
  fi
  SERVER_PID=""
}

start_server

log "Submit real repository task through public HTTP boundary"
INITIAL="$WORKDIR/initial.json"
"$PY" - "$BASE" "$INITIAL" <<'PY'
import json
import sys
from urllib.request import Request, urlopen

base, output = sys.argv[1:]
intent = {
    "title": "Durable Loom dogfood",
    "goal": "Make the bounded repository change required by the dogfood server profile.",
    "requirements": ["Inspect before modifying.", "Run repository verification."],
    "constraints": ["Use the public HTTP boundary.", "Do not use Python Loom internals from the consumer."],
    "acceptance": ["Initial marker exists.", "Follow-up marker exists.", "Verification passes."],
    "provenance": {"source": "dogfood-1015"},
}
request = Request(
    f"{base}/intents",
    data=json.dumps(intent).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request) as response:
    payload = json.load(response)
assert payload["status"] == "success", payload
assert payload["execution_id"]
json.dump(payload, open(output, "w", encoding="utf-8"))
print(json.dumps(payload))
PY

EXECUTION_ID="$("$PY" - "$INITIAL" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["execution_id"])
PY
)"

grep -q "test_loom_dogfood_initial" "$TARGET" || fail "initial repository change missing"

log "Terminate Loom process and preserve only durable state"
stop_server

log "Restart Loom from a fresh process"
start_server

log "Observe the same execution identity after restart"
"$PY" - "$BASE" "$EXECUTION_ID" <<'PY'
import json
import sys
from urllib.request import urlopen

with urlopen(f"{sys.argv[1]}/executions/{sys.argv[2]}") as response:
    payload = json.load(response)
assert payload["execution_id"] == sys.argv[2]
assert payload["status"] == "success"
assert payload["provenance"]["source"] == "dogfood-1015"
phases = [x["phase"] for x in payload["evidence"] if x.get("type") == "execution-phase"]
assert phases == ["initial"], phases
print(json.dumps(payload))
PY

log "Continue by execution_id without replaying a transcript"
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
with urlopen(request) as response:
    payload = json.load(response)
assert payload["execution_id"] == sys.argv[2]
assert payload["status"] == "success", payload
print(json.dumps(payload))
PY

grep -q "test_loom_dogfood_followup" "$TARGET" || fail "follow-up repository change missing"

log "Final durable observation and verification"
"$PY" - "$BASE" "$EXECUTION_ID" "$TARGET" <<'PY'
import json
import sys
from urllib.request import urlopen

with urlopen(f"{sys.argv[1]}/executions/{sys.argv[2]}") as response:
    payload = json.load(response)
assert payload["execution_id"] == sys.argv[2]
assert payload["status"] == "success"
assert payload["provenance"]["source"] == "dogfood-1015"
phases = [x["phase"] for x in payload["evidence"] if x.get("type") == "execution-phase"]
assert phases == ["initial", "continued"], phases
text = open(sys.argv[3], encoding="utf-8").read()
assert "test_loom_dogfood_initial" in text
assert "test_loom_dogfood_followup" in text
print("RESULT: #1015 DURABLE DOGFOOD PASSED")
print("RESULT: submit -> persist -> verify -> terminate -> restart -> observe -> continue -> verify")
PY

stop_server
