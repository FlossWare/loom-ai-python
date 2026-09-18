#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/FlossWare/loom-ai.git"
REF="${LOOM_DOGFOOD_REF:-main}"
KEEP="${LOOM_DOGFOOD_KEEP:-0}"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }

ROOT=""
if git rev-parse --show-toplevel >/dev/null 2>&1; then
    ROOT="$(git rev-parse --show-toplevel)"
else
    command -v git >/dev/null 2>&1 || fail "git is required"
    ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loom-stage1.XXXXXX")/loom-ai"
    trap '[[ "$KEEP" == 1 ]] || rm -rf "$(dirname "$ROOT")"' EXIT
    log "Cloning $REPO_URL@$REF"
    git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$ROOT" || fail "unable to clone Loom"
fi

cd "$ROOT"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' || fail "Python 3.11+ is required"

log "Loom Stage 1 real-task dogfood"
printf 'Repository: %s\n' "$ROOT"
printf 'Commit: %s\n' "$(git rev-parse HEAD)"
printf 'Python: %s\n' "$(python3 --version 2>&1)"

[[ -x .venv/bin/python ]] || python3 -m venv .venv
PY=.venv/bin/python
"$PY" -m pip install --quiet -e '.[dev]'

export LOOM_STAGE1_ROOT="$ROOT"

"$PY" - <<'PY'
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from loom_ai import (
    Arbiter,
    ArbiterDecision,
    Intent,
    WorkerContext,
    WorkerEvaluation,
    WorkerResult,
    WorkerStatus,
)

ROOT = Path(os.environ["LOOM_STAGE1_ROOT"])
TEST_FILE = ROOT / "tests" / "test_worker_arbiter.py"
MARKER = "test_worker_result_successful_property"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class InspectWorker:
    worker_id = "inspect"

    def execute(self, context: WorkerContext) -> WorkerResult:
        text = TEST_FILE.read_text() if TEST_FILE.is_file() else ""
        target_missing = MARKER not in text
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS if TEST_FILE.is_file() else WorkerStatus.FAILED,
            output={
                "test_file": str(TEST_FILE),
                "exists": TEST_FILE.is_file(),
                "target_test_missing": target_missing,
            },
            evidence=(
                {
                    "worker": self.worker_id,
                    "test_file": str(TEST_FILE),
                    "target_test_missing": target_missing,
                },
            ),
            error="target test file is missing" if not TEST_FILE.is_file() else "",
        )


class TestWorker:
    worker_id = "baseline-tests"

    def execute(self, context: WorkerContext) -> WorkerResult:
        result = run("python", "-m", "pytest", "-q")
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS if result.returncode == 0 else WorkerStatus.FAILED,
            output=result.stdout,
            evidence=({"worker": self.worker_id, "returncode": result.returncode},),
            error=result.stderr[-4000:],
        )


class ImplementationWorker:
    worker_id = "implementation"

    def execute(self, context: WorkerContext) -> WorkerResult:
        inspection = context.evidence[-1] if context.evidence else {}
        if not inspection.get("target_test_missing", True):
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.SUCCESS,
                output="target regression test already present",
                evidence=({"worker": self.worker_id, "changed": False},),
            )

        text = TEST_FILE.read_text()
        addition = '''\n\n\ndef test_worker_result_successful_property():\n    """Verify successful reflects WorkerStatus.SUCCESS only."""\n    success = WorkerResult(worker_id="success", status=WorkerStatus.SUCCESS)\n    failure = WorkerResult(worker_id="failure", status=WorkerStatus.FAILED)\n\n    assert success.successful\n    assert not failure.successful\n'''
        TEST_FILE.write_text(text.rstrip() + addition)
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS,
            output=f"added regression test to {TEST_FILE}",
            evidence=(
                {
                    "worker": self.worker_id,
                    "changed": True,
                    "marker": MARKER,
                    "reason": "inspection found missing coverage for WorkerResult.successful",
                },
            ),
        )


class VerificationWorker:
    worker_id = "verification"

    def execute(self, context: WorkerContext) -> WorkerResult:
        if MARKER not in TEST_FILE.read_text():
            return WorkerResult(
                worker_id=self.worker_id,
                status=WorkerStatus.FAILED,
                error="regression test marker is missing",
            )
        result = run("python", "-m", "pytest", "-q")
        return WorkerResult(
            worker_id=self.worker_id,
            status=WorkerStatus.SUCCESS if result.returncode == 0 else WorkerStatus.FAILED,
            output=result.stdout,
            evidence=({"worker": self.worker_id, "returncode": result.returncode},),
            error=result.stderr[-4000:],
        )


intent = Intent(
    title="Stage 1 Loom self-dogfood",
    goal="Inspect Loom, identify a small missing regression test, add it, and verify the repository.",
    requirements=(
        "Inspect the target repository before changing it.",
        "Identify whether coverage for WorkerResult.successful is missing.",
        "Run the existing test suite before making the change.",
        "Make the smallest useful test-only change.",
        "Run the test suite after the change.",
    ),
    constraints=(
        "Do not use an LLM or external model provider.",
        "Do not modify production Loom code.",
    ),
    acceptance=(
        "The target test file exists.",
        "The baseline test suite passes.",
        "The implementation follows the inspection result.",
        "A regression test covers WorkerResult.successful for success and failure.",
        "The post-change test suite passes.",
    ),
)

workers = [InspectWorker(), TestWorker(), ImplementationWorker(), VerificationWorker()]


def evaluate(result: WorkerResult, _context: WorkerContext) -> WorkerEvaluation:
    if not result.successful:
        return WorkerEvaluation(ArbiterDecision.REPLAN, reason=result.error or "worker failed")
    if result.worker_id == "verification":
        return WorkerEvaluation(ArbiterDecision.COMPLETE, reason="acceptance criteria satisfied")
    return WorkerEvaluation(ArbiterDecision.CONTINUE)


print(f"Intent: {intent.goal}")
result = Arbiter(workers, evaluate, max_retries=0).execute(
    WorkerContext(intent=intent, state={"repository": str(ROOT)})
)

for output in result.output:
    print(f"{output.worker_id:>18}: {output.status.value}")

if not result.successful:
    raise SystemExit(f"Stage 1 failed: {result.error}")

print("\nRESULT: LOOM STAGE 1 DOGFOOD PASSED")
PY

printf '\nNOTE: Stage 1 intentionally leaves the test-only change in the checkout.\n'
printf 'Review it with: git diff -- tests/test_worker_arbiter.py\n'
