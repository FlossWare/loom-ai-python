#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/FlossWare/loom-ai.git"
REF="${LOOM_DOGFOOD_REF:-main}"
KEEP="${LOOM_DOGFOOD_KEEP:-0}"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    command -v git >/dev/null 2>&1 || fail "git is required"
    WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/loom-dogfood.XXXXXX")"
    trap '[[ "$KEEP" == 1 ]] || rm -rf "$WORKDIR"' EXIT
    ROOT="$WORKDIR/loom-ai"
    log "Cloning $REPO_URL@$REF"
    git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$ROOT" || fail "unable to clone Loom"
    exec env LOOM_DOGFOOD_REF="$REF" LOOM_DOGFOOD_KEEP="$KEEP" "$ROOT/scripts/dogfood.sh" "$@"
fi

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' || fail "Python 3.11+ is required"

log "Loom qualification"
printf 'Commit: %s\n' "$(git rev-parse HEAD)"
printf 'Python: %s\n' "$(python3 --version 2>&1)"

[[ -x .venv/bin/python ]] || python3 -m venv .venv
PY=.venv/bin/python

log "Installing qualification dependencies"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -e '.[dev]'

log "Static quality"
"$PY" -m ruff format --check .
"$PY" -m ruff check .

log "Core execution tests"
"$PY" -m pytest -q

log "Core package build"
"$PY" -m build --wheel --sdist

log "Core smoke"
"$PY" - <<'PY'
from loom_ai import Arbiter, ArbiterDecision, Intent, WorkerContext, WorkerEvaluation, WorkerResult, WorkerStatus

class SmokeWorker:
    worker_id = "smoke"

    def execute(self, context):
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output=context.intent.goal,
            evidence=({"worker": self.worker_id},),
        )

intent = Intent(goal="dogfood the canonical Loom core")
result = Arbiter(
    [SmokeWorker()],
    lambda _result, _context: WorkerEvaluation(ArbiterDecision.COMPLETE),
).execute(WorkerContext(intent=intent))
assert result.successful
assert result.output[0].output == intent.goal
PY

log "Stage 5 model boundary dogfood"
"$PY" - <<'PY'
import json
from threading import Thread
from urllib.request import Request, urlopen

from loom_ai import (
    Arbiter,
    ArbiterDecision,
    FakeModelProvider,
    Intent,
    LoomServer,
    ModelWorker,
    WorkerEvaluation,
)

worker = ModelWorker(FakeModelProvider(), model="dogfood-model", worker_id="model-dogfood")
arbiter = Arbiter(
    [worker],
    lambda result, _context: WorkerEvaluation(
        ArbiterDecision.COMPLETE if result.successful else ArbiterDecision.REPLAN
    ),
    max_retries=0,
)
server = LoomServer(arbiter, port=0)
instance = server.start()
thread = Thread(target=instance.serve_forever, daemon=True)
thread.start()
try:
    url = f"http://{server.host}:{server.port}/intents"
    request = Request(
        url,
        data=json.dumps(
            {
                "title": "Stage 5",
                "goal": "exercise the model provider boundary",
                "requirements": ["keep the provider hidden from the Worker contract"],
                "provenance": {"source": "stage5-dogfood"},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request) as response:
        payload = json.load(response)

    assert payload["status"] == "success"
    model_result = payload["output"][0]
    assert model_result["worker_id"] == "model-dogfood"
    assert model_result["output"].startswith("fake response: exercise the model provider boundary")
    assert model_result["metadata"]["provider"] == "fake"
    assert model_result["metadata"]["model"] == "dogfood-model"
    assert model_result["evidence"][0]["finish_reason"] == "stop"
    assert "api_key" not in json.dumps(payload)
    assert "secret" not in json.dumps(payload).lower()
finally:
    server.close()
    thread.join(timeout=2)
PY

log "Stage 5 model boundary qualification complete"
echo
printf 'RESULT: LOOM CORE DOGFOOD PASSED\n'
printf 'RESULT: LOOM STAGE 5 DOGFOOD PASSED\n'
