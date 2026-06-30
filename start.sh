#!/usr/bin/env bash
set -euo pipefail

PORT="${DEPLOY_RUN_PORT:-5000}"
export DOTA_CACHE_ROOT="${DOTA_CACHE_ROOT:-/tmp/dota_vision_web_cache}"

echo "Starting Dota vision API on port ${PORT}"
echo "Using cache root ${DOTA_CACHE_ROOT}"
exec python -m uvicorn web.backend.app:app --host 0.0.0.0 --port "${PORT}"
