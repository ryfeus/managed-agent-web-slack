#!/usr/bin/env python3
"""Materialize the pinned native source and our read-only offer inspection patch."""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISION = "c7c1e1ef36aa07604b421c48be266d7397557656"
DESTINATION = ROOT / "services/openappa/upstream"


def prepare() -> None:
    candidates = list((ROOT / ".generated/cargo/git/checkouts").glob("openappa-*/c7c1e1e"))
    source = candidates[0] if candidates else ROOT / ".generated/openappa-source"
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--no-checkout", "https://github.com/archestra-ai/OpenAPPA", str(source)],
            check=True,
        )
        subprocess.run(["git", "-C", str(source), "checkout", "--detach", REVISION], check=True)
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != REVISION:
        raise RuntimeError("OpenAPPA checkout does not match the pinned revision")
    subprocess.run(["git", "-C", str(source), "diff", "--exit-code", "HEAD"], check=True)
    untracked = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "--others", "--exclude-standard"],
        text=True,
    ).splitlines()
    if set(untracked) - {".cargo-ok"}:
        raise RuntimeError("Unexpected files in the OpenAPPA source checkout")
    if DESTINATION.exists():
        shutil.rmtree(DESTINATION)
    # Upstream build.rs reads committed batteries through git.
    shutil.copytree(source, DESTINATION, ignore=shutil.ignore_patterns("target"))
    patch = ROOT / "services/openappa/patches/offer-kind.patch"
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=DESTINATION, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=DESTINATION, check=True)


if __name__ == "__main__":
    prepare()
