#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
cd "$(dirname "$0")/.."
if find dist/python-package -name '*.so' -print -quit | grep -q .; then
  invalid_native="$(find dist/python-package -name '*.so' -print0 | xargs -0 file | grep -Ev 'ELF 64-bit.*x86-64' || true)"
  if [[ -n "$invalid_native" ]]; then
    echo "Python Lambda package contains a non-x86_64 native library." >&2
    exit 1
  fi
fi

for artifact in dist/lambdas/python-buffered.zip dist/lambdas/agui-bridge.zip dist/lambdas/cma-controller.zip dist/lambdas/a2a-event-sink.zip; do
  if [[ ! -s "$artifact" ]]; then
    echo "Missing Lambda artifact: $artifact" >&2
    exit 1
  fi
  if ! unzip -l "$artifact" standardwebhooks/__init__.py >/dev/null; then
    echo "Lambda artifact is missing the Anthropic webhook verifier: $artifact" >&2
    exit 1
  fi
  if ! unzip -t "$artifact" >/dev/null; then
    echo "Lambda artifact is not a valid ZIP archive: $artifact" >&2
    exit 1
  fi
  python3 - "$artifact" <<'PY'
import sys
from pathlib import Path
from zipfile import ZipFile
artifact = Path(sys.argv[1])
if artifact.stat().st_size > 50_000_000:
    raise SystemExit(f"Lambda artifact exceeds the direct-upload size limit: {artifact}")
with ZipFile(artifact) as archive:
    if any(name.endswith(('.pyc', '.pyo')) for name in archive.namelist()):
        raise SystemExit(f"Lambda artifact contains unnecessary Python bytecode: {artifact}")
PY
done

if [[ ! -x dist/lambdas/agui-bridge/run.sh ]]; then
  echo "Lambda AG-UI bridge launcher is not executable." >&2
  exit 1
fi
if [[ ! -x dist/lambdas/cma-controller/run.sh ]]; then
  echo "Lambda CMA controller launcher is not executable." >&2
  exit 1
fi
if [[ ! -x dist/lambdas/a2a-event-sink/run.sh ]]; then
  echo "Lambda A2A event-sink launcher is not executable." >&2
  exit 1
fi

if [[ ! -x dist/lambdas/python-buffered/managed_agents_app/policy/openappa-cma ]] ||
   ! file dist/lambdas/python-buffered/managed_agents_app/policy/openappa-cma | grep -q 'ELF 64-bit.*x86-64'; then
  echo "Scheduler OpenAPPA helper is absent or has an invalid architecture." >&2
  exit 1
fi
