#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
if [[ -x .generated/cargo/bin/cargo ]]; then
  export CARGO_HOME="$PWD/.generated/cargo"
  export RUSTUP_HOME="$PWD/.generated/rustup"
  export PATH="$CARGO_HOME/bin:$PATH"
fi
if [[ "${1:-}" != --test ]]; then
  python3 scripts/prepare_openappa.py
fi
manifest=examples/support-triage/Cargo.toml
cargo build --locked --manifest-path "$manifest" --bin support-triage
if [[ "${1:-}" == --test ]]; then
  cargo fmt --manifest-path "$manifest" --check
  cargo test --locked --manifest-path "$manifest"
else
  exec examples/support-triage/target/debug/support-triage "$@"
fi
