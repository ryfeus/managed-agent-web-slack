#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -x .generated/cargo/bin/cargo ]]; then
  export CARGO_HOME="$PWD/.generated/cargo"
  export RUSTUP_HOME="$PWD/.generated/rustup"
  export PATH="$CARGO_HOME/bin:$PATH"
fi
if ! command -v cargo >/dev/null; then
  echo 'OpenAPPA requires Rust 1.99 or newer (cargo and rustc).' >&2
  exit 1
fi
cargo build --locked --manifest-path services/openappa/Cargo.toml
mkdir -p .generated/openappa-bin
cp services/openappa/target/debug/openappa-cma .generated/openappa-bin/openappa-cma
if [[ "${1:-}" == --test ]]; then
  cargo fmt --manifest-path services/openappa/Cargo.toml --check
  cargo test --locked --manifest-path services/openappa/Cargo.toml
fi
