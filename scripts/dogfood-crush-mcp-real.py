#!/usr/bin/env python3
"""Dogfood a fresh Crush session against a real Loom repository.

The harness intentionally keeps repository tooling on the Crush side while Loom
owns the public execution boundary, durable execution state, orchestration, and
verification checkpoint. It uses a disposable clone of FlossWare/loom-ai so
that a failed experiment cannot modify the user's checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from loom_ai import Arbiter, ArbiterDecision, WorkerEvaluation, WorkerResult, WorkerStatus
from loom_ai.execution_state import FileExecutionStateStore
from loom_ai.server import LoomServer
from loom_ai.worker import WorkerContext

REPO_URL = "https://github.com/FlossWare/loom-ai.git"
TARGET_FILE = "loom_ai/server.py"
ISSUE = "#986"


def fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def request_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"HTTP {exc.code} from {url}: {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach {url}: {exc.reason}") from exc


def verify_real_repository(repo: Path) -> WorkerResult:
    file_path = repo / TARGET_FILE
    if not file_path.is_file():
        return WorkerResult(
            "real-repo-verify",
            WorkerStatus.FAILURE,
            error=f"missing {TARGET_FILE}",
        )

    text = file_path.read_text(encoding="utf-8")
    literal_count = text.count('"not found"')
    status = run(["git", "status", "--short"], cwd=repo)
    diff = run(["git", "diff", "--name-only"], cwd=repo)
    diff_check = run(["git", "diff", "--check"], cwd=repo)

    if (
        status.returncode != 0
        or diff.returncode != 0
        or diff_check.returncode != 0
    ):
        return WorkerResult(
            "real-repo-verify",
            WorkerStatus.FAILURE,
            error="git verification command failed",
        )

    changed = [line for line in diff.stdout.splitlines() if line]
    if changed != [TARGET_FILE]:
        return WorkerResult(
            "real-repo-verify",
            WorkerStatus.FAILURE,
            error=f"unexpected changed files: {changed!r}",
        )
    if literal_count > 1:
        return WorkerResult(
            "real-repo-verify",
            WorkerStatus.FAILURE,
            error=(
                f'{literal_count} occurrences of "not found" remain '
                f"in {TARGET_FILE}"
            ),
        )

    return WorkerResult(
        "real-repo-verify",
        WorkerStatus.SUCCESS,
        evidence=(
            {
                "type": "real-repository-verification",
                "message": (
                    f"verified {ISSUE}: {TARGET_FILE} has one or fewer "
                    'not-found literals'
                ),
                "changed_files": changed,
            },
        ),
    )


class VerificationWorker:
    worker_id = "real-repo-verify"

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def execute(self, context: WorkerContext) -> WorkerResult:
        checkpoint_seen = any(
            isinstance(item, dict)
            and item.get("message") == "real repository is clean before task"
            for item in context.evidence
        )
        if not checkpoint_seen:
            result = run(["git", "status", "--porcelain"], cwd=self.repo)
            if result.returncode != 0:
                return WorkerResult(
                    self.worker_id,
                    WorkerStatus.FAILURE,
                    error=result.stderr.strip(),
                )
            if result.stdout.strip():
                return WorkerResult(
                    self.worker_id,
                    WorkerStatus.FAILURE,
                    error="real dogfood clone is not clean before the task",
                )
            return WorkerResult(
                self.worker_id,
                WorkerStatus.SUCCESS,
                evidence=(
                    {
                        "type": "real-repository-checkpoint",
                        "message": "real repository is clean before task",
                    },
                ),
            )
        return verify_real_repository(self.repo)


def evaluate(
    result: WorkerResult, _context: WorkerContext
) -> WorkerEvaluation:
    if result.successful:
        return WorkerEvaluation(
            ArbiterDecision.COMPLETE,
            reason="dogfood checkpoint verified",
        )
    return WorkerEvaluation(
        ArbiterDecision.REPLAN,
        reason=result.error or "verification failed",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dogfood fresh Crush -> generic Loom MCP against real loom-ai"
    )
    parser.add_argument("--repo", default=REPO_URL, help="Git repository URL")
    parser.add_argument(
        "--model",
        default=None,
        help="Optional Crush model passed to crush run",
    )
    args = parser.parse_args()

    crush = shutil.which("crush")
    if crush is None:
        fail("crush is not installed or is not on PATH")
    git = shutil.which("git")
    if git is None:
        fail("git is not installed or is not on PATH")

    python = root / ".venv" / "bin" / "python"
    mcp_server = root / "scripts" / "loom_mcp_server.py"
    if not python.is_file():
        fail(f"missing project Python environment: {python}")
    if not mcp_server.is_file():
        fail(f"missing generic MCP server: {mcp_server}")

    with tempfile.TemporaryDirectory(prefix="loom-ai-949-") as temp:
        temp_root = Path(temp)
        repo = temp_root / "loom-ai"
        clone = run(
            [
                git,
                "clone",
                "--depth",
                "1",
                "--branch",
                "main",
                args.repo,
                str(repo),
            ]
        )
        if clone.returncode != 0:
            fail(f"could not clone {args.repo}: {clone.stderr.strip()}")

        clean = run(["git", "status", "--porcelain"], cwd=repo)
        if clean.returncode != 0 or clean.stdout.strip():
            fail("fresh loom-ai clone is not clean")

        execution_id = f"crush-real-{uuid4()}"
        intent_id = f"intent-{uuid4()}"
        store = FileExecutionStateStore(temp_root / "execution-state")
        server = LoomServer(
            Arbiter([VerificationWorker(repo)], evaluate, max_retries=0),
            host="127.0.0.1",
            port=0,
            execution_store=store,
        )
        http_server = server.start()
        thread = threading.Thread(
            target=http_server.serve_forever,
            daemon=True,
        )
        thread.start()

        crushrc = repo / ".crushrc"
        crushrc.write_text(
            "# Disposable real-repository Loom dogfood configuration.\n"
            f'mcp add loom --command "{python}" --args "{mcp_server}" '
            f'--env LOOM_URL "http://127.0.0.1:{server.port}" --timeout 30\n',
            encoding="utf-8",
        )

        prompt = f"""
You are performing the real Loom dogfood for issue {ISSUE} in this repository.
This is a real repository, not a fixture. Use your normal repository tools for
inspection, editing, shell commands, and git. Use the generic Loom MCP for
orchestration and verification. Do not call Loom Python internals.

Task: resolve {ISSUE}: define a constant instead of duplicating the literal
"not found" in {TARGET_FILE}. Keep the change narrowly scoped. Do not modify
unrelated files. Do not change the public behavior.

Execution protocol:
1. Call loom_submit_intent exactly once before editing, using execution_id
   "{execution_id}" and intent_id "{intent_id}". Include provenance with
   client=fresh-crush, issue={ISSUE}, and task_path={TARGET_FILE}.
2. Inspect the repository and the relevant code.
3. Make the real code change with your repository edit tool.
4. Run the appropriate tests/validation for the change.
5. Call loom_continue_execution exactly once with execution_id
   "{execution_id}". Do not claim success if that Loom verification fails.
6. Report the final execution_id and the repository verification result.

The Loom checkpoint is authoritative for the workflow result. If verification
fails, investigate and correct the task before reporting success.
""".strip()

        env = os.environ.copy()
        env["CRUSH_GLOBAL_DATA"] = str(temp_root / "crush-data")
        command = [crush, "run", "--quiet"]
        if args.model:
            command.extend(["--model", args.model])
        command.append(prompt)

        print("==> Fresh Crush -> generic Loom MCP -> real loom-ai repository")
        version_result = run([crush, "--version"], env=env)
        print(
            f"Crush: {version_result.stdout.strip() or version_result.stderr.strip()}"
        )
        print(f"Loom URL: http://127.0.0.1:{server.port}")
        print(f"Repository: {repo}")
        print(f"Execution ID: {execution_id}")
        print(f"Task: {ISSUE} ({TARGET_FILE})")

        try:
            completed = run(command, cwd=repo, env=env)
            print("\n==> Crush stdout")
            print(completed.stdout.rstrip())
            if completed.stderr.strip():
                print("\n==> Crush stderr")
                print(completed.stderr.rstrip(), file=sys.stderr)
            if completed.returncode != 0:
                fail(f"Crush exited with status {completed.returncode}")

            execution = request_json(
                f"http://127.0.0.1:{server.port}/executions/{execution_id}"
            )
            print("\n==> Durable Loom execution")
            print(json.dumps(execution, indent=2, sort_keys=True))
            if execution.get("status") != "success":
                fail(
                    f"Loom execution status is {execution.get('status')!r}"
                )
            if execution.get("intent_id") != intent_id:
                fail("Loom intent_id does not match")

            provenance = execution.get("provenance", {})
            expected_provenance = {
                "client": "fresh-crush",
                "issue": ISSUE,
                "task_path": TARGET_FILE,
            }
            for key, expected in expected_provenance.items():
                if provenance.get(key) != expected:
                    fail(f"missing or incorrect provenance {key!r}")

            final_text = (repo / TARGET_FILE).read_text(encoding="utf-8")
            if final_text.count('"not found"') > 1:
                fail(
                    f'{TARGET_FILE} still duplicates the "not found" literal'
                )

            diff = run(["git", "diff", "--name-only"], cwd=repo)
            if diff.stdout.splitlines() != [TARGET_FILE]:
                fail(
                    f"unexpected repository changes: {diff.stdout.splitlines()!r}"
                )

            diff_check = run(["git", "diff", "--check"], cwd=repo)
            if diff_check.returncode != 0:
                fail("git diff --check failed")

            print(
                "\nRESULT: FRESH CRUSH -> GENERIC MCP -> LOOM -> "
                "REAL REPO PASSED"
            )
            print(f"Changed: {TARGET_FILE}")
            print(f"Issue: {ISSUE}")
            print(f"Execution: {execution_id}")
            print(
                "Independent checks: changed-file scope, literal "
                "deduplication, git diff --check"
            )
            return 0
        finally:
            server.close()
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
