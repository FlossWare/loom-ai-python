"""Unit tests for the real-repository Crush dogfood verifier."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from loom_ai.intent import Intent
from loom_ai.worker import WorkerContext, WorkerStatus

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dogfood-crush-mcp-real.py"
SPEC = importlib.util.spec_from_file_location("dogfood_crush_mcp_real", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
DOGFOOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOGFOOD)


def create_repository(tmp_path: Path) -> Path:
    repo = tmp_path / "loom-ai"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Loom Dogfood Test"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "loom-dogfood-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    target = repo / DOGFOOD.TARGET_FILE
    target.parent.mkdir(parents=True)
    target.write_text("# Dogfood target\n", encoding="utf-8")
    subprocess.run(["git", "add", DOGFOOD.TARGET_FILE], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "initial fixture"],
        cwd=repo,
        check=True,
    )
    return repo


def test_verifier_accepts_only_the_expected_documentation_change(
    tmp_path: Path,
) -> None:
    repo = create_repository(tmp_path)
    target = repo / DOGFOOD.TARGET_FILE
    target.write_text(
        target.read_text(encoding="utf-8") + f"\n{DOGFOOD.MARKER}\n\n"
        "The public Loom boundary is agent-neutral.\n",
        encoding="utf-8",
    )

    result = DOGFOOD.verify_repository(repo)

    assert result.status is WorkerStatus.SUCCESS
    assert result.evidence[0]["changed_files"] == [DOGFOOD.TARGET_FILE]


def test_verifier_rejects_unrelated_repository_changes(tmp_path: Path) -> None:
    repo = create_repository(tmp_path)
    target = repo / DOGFOOD.TARGET_FILE
    target.write_text(
        target.read_text(encoding="utf-8") + f"\n{DOGFOOD.MARKER}\n",
        encoding="utf-8",
    )
    (repo / "unrelated.txt").write_text("not part of this task\n", encoding="utf-8")

    result = DOGFOOD.verify_repository(repo)

    assert result.status is WorkerStatus.FAILED
    assert "unexpected changed files" in result.error


def test_verifier_rejects_missing_required_marker(tmp_path: Path) -> None:
    repo = create_repository(tmp_path)
    target = repo / DOGFOOD.TARGET_FILE
    target.write_text("# unrelated edit\n", encoding="utf-8")

    result = DOGFOOD.verify_repository(repo)

    assert result.status is WorkerStatus.FAILED
    assert DOGFOOD.MARKER in result.error


def test_initial_checkpoint_allows_only_disposable_crush_config(
    tmp_path: Path,
) -> None:
    repo = create_repository(tmp_path)
    (repo / ".crushrc").write_text("disposable config\n", encoding="utf-8")
    worker = DOGFOOD.VerificationWorker(repo)

    result = worker.execute(
        WorkerContext(intent=Intent(goal="verify clean starting repository"))
    )

    assert result.status is WorkerStatus.SUCCESS
    assert result.evidence[0]["message"] == "real repository is clean before task"


def test_negative_qualification_forces_verification_failure(
    tmp_path: Path,
) -> None:
    repo = create_repository(tmp_path)
    context = WorkerContext(
        intent=Intent(goal="verify deliberate negative path"),
        evidence=({"message": "real repository is clean before task"},),
    )
    worker = DOGFOOD.VerificationWorker(repo, force_failure=True)

    result = worker.execute(context)

    assert result.status is WorkerStatus.FAILED
    assert "deliberate negative-path" in result.error


def test_run_enforces_the_requested_subprocess_timeout() -> None:
    try:
        DOGFOOD.run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.01,
        )
    except subprocess.TimeoutExpired:
        return
    raise AssertionError("dogfood command exceeded timeout but was not stopped")


def test_build_crushrc_configures_provider_model_and_generic_mcp() -> None:
    config = DOGFOOD.build_crushrc(
        python=Path("/tmp/python with spaces"),
        mcp_server=Path("/tmp/loom mcp server.py"),
        crush_data=Path("/tmp/crush data"),
        loom_url="http://127.0.0.1:18080",
        provider_base_url="http://127.0.0.1:8765/v1",
    )

    assert (
        "provider add flossware --type openai-compat --base-url "
        '"http://127.0.0.1:8765/v1"'
    ) in config
    assert (
        'model add flossware/flossware --name "FlossWare" '
        "--context-window 128000 --default-max-tokens 20000"
    ) in config
    assert (
        'mcp add loom --command "/tmp/python with spaces" '
        '--args "/tmp/loom mcp server.py"'
    ) in config
    assert '--env LOOM_URL "http://127.0.0.1:18080" --timeout 30' in config
    assert config.endswith("\n")
