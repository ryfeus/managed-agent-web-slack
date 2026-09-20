#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
source .env
set +a

export NEXT_PUBLIC_API_BASE_URL="${NEXT_PUBLIC_API_BASE_URL:-http://localhost:3001}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.uv-cache}"
exec concurrently -k \
  "npm run dev -w @managed-agents/web" \
  "uv run --directory backend uvicorn managed_agents_app.local_api:app --host 127.0.0.1 --port 3001 --reload"
