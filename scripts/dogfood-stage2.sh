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

PORT="${LOOM_SERVER_PORT:-8766}"
LOG="$(mktemp)"
cleanup() {
    if [[ -n "${SERVER_PID:-}" ]]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -f "$LOG"
}
trap cleanup EXIT

"$PY" -m loom_ai.server --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 &
SERVER_PID=$!

for _ in {1..50}; do
    if "$PY" - <<PY
import sys, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:${PORT}/health", timeout=1).close()
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
    then
        break
    fi
    sleep 0.1
done

"$PY" - <<PY
import json
import urllib.request

base = "http://127.0.0.1:${PORT}"
with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
    assert response.status == 200

request = urllib.request.Request(
    f"{base}/intents",
    data=json.dumps({
        "title": "Stage 2 server dogfood",
        "goal": "Execute an Intent through the Loom server.",
        "acceptance": ["The server returns a successful execution result."],
    }).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=5) as response:
    payload = json.load(response)

assert payload["status"] == "success", payload
assert payload["output"][0]["worker_id"] == "server", payload
assert payload["output"][0]["status"] == "success", payload
print("health: success")
print("intent: success")
print("execution: success")
PY

printf '%s\n' "RESULT: LOOM STAGE 2 SERVER DOGFOOD PASSED"
