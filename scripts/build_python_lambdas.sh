#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.uv-cache}"
export PYTHONDONTWRITEBYTECODE=1
# Avoid expanding an empty array under macOS Bash 3.2's nounset behavior.
if [[ "${1:-}" == --no-cache && $# == 1 ]]; then
  exec python3 scripts/build_lambdas.py --no-cache
elif [[ $# != 0 ]]; then
  echo 'Usage: build:python [--no-cache]' >&2
  exit 2
fi
exec python3 scripts/build_lambdas.py
