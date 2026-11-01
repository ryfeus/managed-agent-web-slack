#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.uv-cache}"
./scripts/build_openappa.sh --test
python3 scripts/cma_lock.py validate
python3 scripts/check_public_readiness.py
python3 scripts/test_deploy_guard.py
npm run lint
npm run format:check:python
npm run typecheck
npm test
