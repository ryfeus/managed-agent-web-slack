#!/usr/bin/env python3
"""Reject tracked deployment configuration and credential-shaped values."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SECRET_PATTERN = re.compile(
    r"\b(?:xox[abp]-[A-Za-z0-9-]{16,}|xapp-[A-Za-z0-9-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|whsec_[A-Za-z0-9_-]{16,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
)
ALLOWED_SYNTHETIC_CREDENTIALS = frozenset({"whsec_ZTJlLXdlYmhvb2stc2VjcmV0"})
ENVIRONMENT_TEMPLATE = ".env.example"


def scan_paths(root: Path, paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for relative in paths:
        posix = relative.as_posix()
        if (
            relative.name == ".env" or relative.name.startswith(".env.")
        ) and relative.name != ENVIRONMENT_TEMPLATE:
            errors.append(f"{posix}: deployment environment files must not be tracked")
            continue
        if posix.startswith(".generated/"):
            errors.append(f"{posix}: generated deployment material must not be tracked")
            continue
        path = root / relative
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for secret in SECRET_PATTERN.findall(source):
            if secret not in ALLOWED_SYNTHETIC_CREDENTIALS:
                errors.append(f"{posix}: contains a credential-shaped value")
    return errors


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
    return [Path(value) for value in result.stdout.decode().split("\0") if value]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        errors = scan_paths(root, tracked_paths(root))
    except subprocess.CalledProcessError as error:
        print(f"unable to enumerate tracked files: {error}", file=sys.stderr)
        return 1
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"repository safety check failed with {len(errors)} violation(s)", file=sys.stderr)
        return 1
    print("Repository safety checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
