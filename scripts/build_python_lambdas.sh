#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.uv-cache}"

rm -rf \
  dist/python-package \
  dist/lambdas/python-buffered \
  dist/lambdas/agui-bridge \
  dist/lambdas/cma-controller \
  dist/lambdas/a2a-event-sink \
  dist/lambdas/python-buffered.zip \
  dist/lambdas/agui-bridge.zip \
  dist/lambdas/cma-controller.zip \
  dist/lambdas/a2a-event-sink.zip
mkdir -p dist/python-package dist/lambdas/python-buffered dist/lambdas/agui-bridge dist/lambdas/cma-controller dist/lambdas/a2a-event-sink
# Create the output files on the host before Docker writes through the bind mount.
for artifact in dist/lambdas/python-buffered.zip dist/lambdas/agui-bridge.zip dist/lambdas/cma-controller.zip dist/lambdas/a2a-event-sink.zip; do
  : > "$artifact"
done

uv export --quiet --directory backend --locked --no-dev --no-emit-project --output-file ../dist/python-requirements.txt

docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  -v "$PWD:/workspace" \
  -w /workspace \
  public.ecr.aws/sam/build-python3.14:latest-x86_64 \
  /bin/bash -lc "
    set -euo pipefail
    python -m pip install --quiet --disable-pip-version-check \
      -r dist/python-requirements.txt -t dist/python-package
    cp -R dist/python-package/. dist/lambdas/python-buffered/
    cp -R backend/src/managed_agents_app dist/lambdas/python-buffered/
    cp -R dist/lambdas/python-buffered/. dist/lambdas/agui-bridge/
    cp -R dist/lambdas/python-buffered/. dist/lambdas/cma-controller/
    cp -R dist/lambdas/python-buffered/. dist/lambdas/a2a-event-sink/
    cp backend/run_agui_bridge.sh dist/lambdas/agui-bridge/run.sh
    cp backend/run_cma_controller.sh dist/lambdas/cma-controller/run.sh
    cp backend/run_a2a_event_sink.sh dist/lambdas/a2a-event-sink/run.sh
    chmod +x dist/lambdas/agui-bridge/run.sh
    chmod +x dist/lambdas/cma-controller/run.sh
    chmod +x dist/lambdas/a2a-event-sink/run.sh
    cd dist/lambdas/python-buffered
    python -m zipfile -c /workspace/dist/lambdas/python-buffered.zip .
    cd ../agui-bridge
    python -m zipfile -c /workspace/dist/lambdas/agui-bridge.zip .
    cd ../cma-controller
    python -m zipfile -c /workspace/dist/lambdas/cma-controller.zip .
    cd ../a2a-event-sink
    python -m zipfile -c /workspace/dist/lambdas/a2a-event-sink.zip .
  "

if find dist/python-package -name '*.so' -print -quit | grep -q .; then
  find dist/python-package -name '*.so' -print0 | xargs -0 file | grep -Ev 'ELF 64-bit.*x86-64' && {
    echo "Python Lambda package contains a non-x86_64 native library." >&2
    exit 1
  } || true
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
