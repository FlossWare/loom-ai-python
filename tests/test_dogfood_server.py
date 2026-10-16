"""Tests for the durable process-boundary dogfood profile."""

from pathlib import Path

from loom_ai import (
    FileExecutionStateStore,
    Intent,
    LoomServer,
    WorkerContext,
    WorkerStatus,
)
from scripts.dogfood_server import (
    FOLLOWUP_MARKER,
    INITIAL_MARKER,
    RepositoryTaskWorker,
    VerificationWorker,
    build_server,
)


def test_dogfood_profile_uses_durable_store_and_real_arbiter(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    target = root / "tests" / "test_server.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_existing():\n    assert True\n", encoding="utf-8")

    monkeypatch.chdir(root)
    server = build_server(host="127.0.0.1", port=0)

    assert isinstance(server, LoomServer)
    assert isinstance(server.execution_store, FileExecutionStateStore)
    assert [worker.worker_id for worker in server.arbiter.workers] == [
        "repository-task",
        "verification",
    ]


def test_repository_worker_applies_initial_and_post_restart_changes(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    target = root / "tests" / "test_server.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(root)
    worker = RepositoryTaskWorker()
    context = WorkerContext(intent=Intent(goal="apply bounded change"))

    initial = worker.execute(context)
    continued = worker.execute(context.with_state(phase="initial"))

    assert initial.status is WorkerStatus.SUCCESS
    assert initial.output == {"phase": "initial", "changed": True}
    assert continued.status is WorkerStatus.SUCCESS
    assert continued.output == {"phase": "continued", "changed": True}
    source = target.read_text(encoding="utf-8")
    assert f"def {INITIAL_MARKER}():" in source
    assert f"def {FOLLOWUP_MARKER}():" in source


def test_repository_worker_rejects_symlinked_tests_directory(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "test_server.py").write_text("def test_marker():\n    assert True\n")
    (root / "tests").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(root)

    result = RepositoryTaskWorker().execute(
        WorkerContext(intent=Intent(goal="must remain inside checkout"))
    )

    assert result.status is WorkerStatus.FAILED
    assert result.error == "dogfood target escaped the task checkout"


def test_verification_worker_uses_fixed_target(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    target = root / "tests" / "test_server.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_marker():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(root)

    result = VerificationWorker().execute(
        WorkerContext(intent=Intent(goal="verify target"))
    )

    assert result.successful
    assert result.output == {"pytest_exit_code": 0}
