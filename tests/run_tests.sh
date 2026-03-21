#!/usr/bin/env bash
# Run pytest inside the running API container.
# Usage:
#   ./tests/run_tests.sh                          # all tests
#   ./tests/run_tests.sh tests/test_upload_endpoints.py   # single file
#   ./tests/run_tests.sh -k "test_success"        # filter by name
set -euo pipefail

CONTAINER="api"

docker compose exec -e PYTHONPATH=/app "$CONTAINER" python -m pytest \
  --tb=short \
  -v \
  "${@:-tests/}"
