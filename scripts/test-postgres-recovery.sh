#!/usr/bin/env bash
set -euo pipefail

: "${FLOSSWARE_LOOM_POSTGRES_DSN:?Set FLOSSWARE_LOOM_POSTGRES_DSN to a disposable PostgreSQL database}"
python -m pytest -q tests/test_postgres_execution_state.py -k live
