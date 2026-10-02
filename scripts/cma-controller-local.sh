#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.uv-cache}"
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
CMA_REPOSITORY_ROOT="$PWD"
source scripts/cma_env.sh
exec uv run --directory backend python -m uvicorn \
  managed_agents_app.cma_controller.app:production_app --factory \
  --host 127.0.0.1 --port "${CMA_LOCAL_PORT:-8081}"
