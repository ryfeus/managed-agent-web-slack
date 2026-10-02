#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $# -ne 1 ]]; then
  echo "Usage: ./scripts/cma.sh {plan|apply|show|validate}" >&2
  exit 2
fi
case "$1" in
  show) exec python3 scripts/cma_lock.py json ;;
  validate) exec python3 scripts/cma_lock.py validate ;;
  plan|apply) ;;
  *) echo "Usage: ./scripts/cma.sh {plan|apply|show|validate}" >&2; exit 2 ;;
esac

python3 scripts/cma_lock.py sources
if [[ -f cma/claude-lock.json ]]; then
  # Explicit provider operations can resume a partially completed bootstrap;
  # deployment and show still require both resources.
  python3 scripts/cma_lock.py preflight
fi
ant_binary="$(command -v ant || true)"
if [[ -z "$ant_binary" && -x .generated/bin/ant ]]; then
  ant_binary="$PWD/.generated/bin/ant"
fi
if [[ -z "$ant_binary" ]]; then
  echo "Install ant CLI >= 1.30.0 and put it on PATH; see cma/README.md." >&2
  exit 1
fi
ant_version="$("$ant_binary" --version)"
python3 - "$ant_version" <<'PY'
import re
import sys
match = re.search(r'\b(\d+)\.(\d+)\.(\d+)\b', sys.argv[1])
if not match or tuple(map(int, match.groups())) < (1, 30, 0):
    sys.exit('ant CLI >= 1.30.0 is required.')
PY

# Load only the API key; exported credentials take precedence. CLI OAuth/WIF
# authentication remains supported when no local key is configured.
if [[ -z "${ANTHROPIC_API_KEY:-}" && -f .env ]]; then
  export ANTHROPIC_API_KEY="$(
    set -a
    source .env
    printf '%s' "${ANTHROPIC_API_KEY:-}"
  )"
fi
if [[ "$1" == plan ]]; then
  (cd cma && "$ant_binary" apply --dry-run .)
else
  trap 'if [[ $? -ne 0 ]]; then echo "CMA apply failed. Inspect the provider diff and retain any lockfile updates; authenticate with ant auth login or ANTHROPIC_API_KEY if needed." >&2; fi' EXIT
  (cd cma && "$ant_binary" apply --yes .)
  python3 scripts/cma_lock.py validate
fi
