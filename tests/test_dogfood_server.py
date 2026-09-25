import sys
from pathlib import Path

from loom_ai.execution_state import FileExecutionStateStore
from loom_ai.server import LoomServer
from scripts.dogfood_server import VerificationWorker, build_server


def test_dogfood_profile_uses_durable_store_and_real_arbiter(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "target.py"
    target.write_text("def existing():\n    return True\n", encoding="utf-8")

    monkeypatch.chdir(root)
    server = build_server(
        host="127.0.0.1",
        port=0,
    )

    assert isinstance(server, LoomServer)
    assert isinstance(server.execution_store, FileExecutionStateStore)
    assert server.arbiter.worker_id == "arbiter"
    assert [worker.worker_id for worker in server.arbiter.workers] == [
        "repository-task",
        "verification",
    ]


def test_verification_worker_uses_fixed_target(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    target = root / "tests" / "test_server.py"
    target.parent.mkdir()
    target.write_text("def test_marker():\n    assert True\n", encoding="utf-8")

    sys.modules.pop("test_server", None)
    result = VerificationWorker().execute(None)

    assert result.successful
