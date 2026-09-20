#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.uv-cache}"

rm -rf \
  dist/python-package \
  dist/lambdas/python-buffered \
  dist/lambdas/web-stream \
  dist/lambdas/python-buffered.zip \
  dist/lambdas/web-stream.zip
mkdir -p dist/python-package dist/lambdas/python-buffered dist/lambdas/web-stream

uv export --quiet --directory backend --locked --no-dev --no-emit-project --output-file ../dist/python-requirements.txt

docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  -v "$PWD:/workspace" \
  -w /workspace \
  public.ecr.aws/sam/build-python3.14:latest-x86_64 \
  /bin/bash -lc "
    python -m pip install --quiet --disable-pip-version-check \
      -r dist/python-requirements.txt -t dist/python-package
    cp -R dist/python-package/. dist/lambdas/python-buffered/
    cp -R backend/src/managed_agents_app dist/lambdas/python-buffered/
    cp -R dist/lambdas/python-buffered/. dist/lambdas/web-stream/
    cp backend/run.sh dist/lambdas/web-stream/run.sh
    chmod +x dist/lambdas/web-stream/run.sh
    cd dist/lambdas/python-buffered
    python -m zipfile -c /workspace/dist/lambdas/python-buffered.zip .
    cd ../web-stream
    python -m zipfile -c /workspace/dist/lambdas/web-stream.zip .
  "

if find dist/python-package -name '*.so' -print -quit | grep -q .; then
  find dist/python-package -name '*.so' -print0 | xargs -0 file | grep -Ev 'ELF 64-bit.*x86-64' && {
    echo "Python Lambda package contains a non-x86_64 native library." >&2
    exit 1
  } || true
fi

for artifact in dist/lambdas/python-buffered.zip dist/lambdas/web-stream.zip; do
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

if [[ ! -x dist/lambdas/web-stream/run.sh ]]; then
  echo "Lambda web-stream launcher is not executable." >&2
  exit 1
fi
