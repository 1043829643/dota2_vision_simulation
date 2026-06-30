#!/usr/bin/env bash
set -euo pipefail

PORT="${DEPLOY_RUN_PORT:-5000}"

echo "Starting Dota vision API on port ${PORT}"
exec python -m uvicorn web.backend.app:app --host 0.0.0.0 --port "${PORT}"
