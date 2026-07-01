#!/usr/bin/env bash
set -euo pipefail

PORT="${DEPLOY_RUN_PORT:-5000}"
# 缓存目录必须持久：/tmp 会在系统重启/清理时被清空，导致单场缓存与预热历史全部丢失。
export DOTA_CACHE_ROOT="${DOTA_CACHE_ROOT:-$(cd "$(dirname "$0")" && pwd)/var/web_cache}"

echo "Starting Dota vision API on port ${PORT}"
echo "Using cache root ${DOTA_CACHE_ROOT}"
exec python -m uvicorn web.backend.app:app --host 0.0.0.0 --port "${PORT}"
