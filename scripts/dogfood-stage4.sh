#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

PY=${PYTHON:-python3}
TASK_ROOT=$(mktemp -d)
trap 'rm -rf "$TASK_ROOT"' EXIT

"$PY" -m pip install -e '.[dev,server]' >/dev/null
"$PY" -m ruff format --check .
"$PY" -m ruff check .
"$PY" -m pytest -q

"$PY" - "$TASK_ROOT" <<'PY'
import json
import pathlib
import subprocess
import sys

root = pathlib.Path.cwd()
task_root = pathlib.Path(sys.argv[1])
repo = task_root / "fixture-repo"
repo.mkdir()
subprocess.run(["git", "init", "-q", str(repo)], check=True)
fixture = repo / "stage4_fixture.py"
fixture.write_text("def value():\n    return 41\n", encoding="utf-8")
subprocess.run(["git", "-C", str(repo), "add", fixture.name], check=True)
subprocess.run(
    [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=Loom Dogfood",
        "-c",
        "user.email=loom@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    ],
    check=True,
)

result = subprocess.run(
    [sys.executable, str(root / "scripts" / "stage4_task.py"), str(fixture)],
    check=False,
    text=True,
    capture_output=True,
)
if result.returncode:
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    raise SystemExit(result.returncode)

payload = json.loads(result.stdout)
assert payload["status"] == "success"
outputs = payload["output"]
assert outputs
verification = next(
    result for result in outputs if result["worker_id"] == "verify"
)
assert verification["output"]["acceptance"] is True
assert [result["worker_id"] for result in outputs] == ["inspect", "plan", "implement", "verify"]
assert payload["evidence"]
assert "return 42" in fixture.read_text(encoding="utf-8")
assert subprocess.run(["git", "-C", str(repo), "diff", "--check"], check=False).returncode == 0
assert subprocess.run(["git", "-C", str(repo), "status", "--short"], check=True, capture_output=True, text=True).stdout
print("RESULT: LOOM STAGE 4 DOGFOOD PASSED")
PY
