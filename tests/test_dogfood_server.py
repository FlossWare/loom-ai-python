from pathlib import Path

from loom_ai.execution_state import FileExecutionStateStore
from loom_ai.server import LoomServer

from scripts.dogfood_server import build_server


def test_dogfood_profile_uses_durable_store_and_real_arbiter(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "target.py"
    target.write_text("def existing():\n    return True\n", encoding="utf-8")

    server = build_server(
        root=root,
        target=target,
        state_dir=tmp_path / "state",
        python="python3",
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
