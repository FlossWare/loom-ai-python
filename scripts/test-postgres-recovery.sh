#!/usr/bin/env bash
set -euo pipefail

: "${FLOSSWARE_LOOM_POSTGRES_DSN:?Set FLOSSWARE_LOOM_POSTGRES_DSN to a disposable PostgreSQL database}"
: "${FLOSSWARE_LOOM_FLEET_POSTGRES_DSN:?Set FLOSSWARE_LOOM_FLEET_POSTGRES_DSN to a disposable PostgreSQL database}"
python -m pytest -q tests/test_postgres_execution_state.py tests/test_postgres_fleet_state.py -k live
