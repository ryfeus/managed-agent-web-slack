#!/usr/bin/env python3
"""Export the tracked, distributable repository tree for a fresh public copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "PUBLIC_EXPORT_MANIFEST.json"
EXCLUDED_PREFIXES = (
    ".generated/",
    ".git/",
    ".next/",
    ".terraform/",
    ".uv-cache/",
    ".venv/",
    "coverage/",
    "dist/",
    "node_modules/",
    "out/",
    "plans/",
    "playwright-report/",
    "playwright-report-production/",
    "test-results/",
)
EXCLUDED_PARTS = {"__pycache__"}


def is_exported_path(relative: Path) -> bool:
    posix = relative.as_posix()
    if (posix == ".env" or posix.startswith(".env.")) and posix != ".env.example":
        return False
    if posix.startswith(EXCLUDED_PREFIXES) or EXCLUDED_PARTS.intersection(relative.parts):
        return False
    return not (
        relative.suffix in {".pyc", ".pyo"}
        or ".tfstate" in relative.name
        or relative.name in {"crash.log"}
        or relative.name.startswith("crash.")
        and relative.name.endswith(".log")
    )


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
    return [Path(value) for value in result.stdout.decode().split("\0") if value]


def export_tree(root: Path, output: Path) -> list[Path]:
    source = root.resolve()
    destination = output.resolve()
    is_generated_output = (
        source in destination.parents and destination.relative_to(source).parts[0] == ".generated"
    )
    if destination == source or source in destination.parents and not is_generated_output:
        raise ValueError("output must be outside the repository or under .generated/")
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    exported: list[Path] = []
    for relative in sorted(path for path in tracked_paths(source) if is_exported_path(path)):
        source_file = source / relative
        if not source_file.is_file():
            continue
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
        exported.append(relative)
    manifest = {
        "files": [
            {"path": path.as_posix(), "sha256": hashlib.sha256((output / path).read_bytes()).hexdigest()}
            for path in exported
        ]
    }
    (output / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return exported


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args()
    try:
        exported = export_tree(args.root, args.output)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        parser.error(str(error))
    print(f"Exported {len(exported)} files to {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
