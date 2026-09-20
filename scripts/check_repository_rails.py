#!/usr/bin/env python3
"""Validate machine-readable limitations and the feature-contract template."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

LIMITATIONS_PATH = Path("knowledge/known-limitations.json")
TEMPLATE_PATH = Path("docs/feature-contract-template.md")
ALLOWED_STATUSES = {"accepted", "open", "resolved"}
REQUIRED_LIMITATION_FIELDS = {"id", "title", "status", "owner", "test", "desired_behavior"}
REQUIRED_TEMPLATE_HEADINGS = (
    "# Feature Contract — <name>",
    "## Goal",
    "## Non-goals",
    "## User-visible behavior",
    "## Inputs",
    "## Outputs / side effects",
    "## Invariants",
    "## Failure semantics",
    "## Concurrency / idempotency semantics",
    "## Security / authorization constraints",
    "## Persistence constraints",
    "## Compatibility constraints",
    "## Verification mapping",
    "## Live-contract requirements",
    "## Known limitations",
    "## Completion checklist",
)


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_limitations(root: Path) -> list[str]:
    registry = root / LIMITATIONS_PATH
    if not registry.is_file():
        return [f"{LIMITATIONS_PATH}: file is missing"]
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return [f"{LIMITATIONS_PATH}: invalid JSON: {error}"]
    if not isinstance(data, list):
        return [f"{LIMITATIONS_PATH}: top-level value must be an array"]

    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        location = f"{LIMITATIONS_PATH}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{location}: entry must be an object")
            continue
        missing = sorted(REQUIRED_LIMITATION_FIELDS - item.keys())
        if missing:
            errors.append(f"{location}: missing fields: {', '.join(missing)}")
        for field in sorted(REQUIRED_LIMITATION_FIELDS & item.keys()):
            if not _nonempty_string(item[field]):
                errors.append(f"{location}.{field}: must be a non-empty string")
        limitation_id = item.get("id")
        if _nonempty_string(limitation_id):
            if not re.fullmatch(r"KL-\d{3}", limitation_id):
                errors.append(f"{location}.id: must match KL-###")
            if limitation_id in seen:
                errors.append(f"{location}.id: duplicate ID {limitation_id}")
            seen.add(limitation_id)
        status = item.get("status")
        if _nonempty_string(status) and status not in ALLOWED_STATUSES:
            errors.append(
                f"{location}.status: expected one of {', '.join(sorted(ALLOWED_STATUSES))}, got {status}"
            )
        test_value = item.get("test")
        if not _nonempty_string(test_value):
            continue
        test_path = Path(test_value)
        if test_path.is_absolute() or ".." in test_path.parts:
            errors.append(f"{location}.test: must be a repository-relative path")
            continue
        resolved = root / test_path
        if not resolved.is_file():
            errors.append(f"{location}.test: referenced path does not exist: {test_value}")
        elif _nonempty_string(limitation_id) and limitation_id not in resolved.read_text(encoding="utf-8"):
            errors.append(f"{location}.test: {test_value} does not reference {limitation_id}")
    return errors


def validate_template(root: Path) -> list[str]:
    template = root / TEMPLATE_PATH
    if not template.is_file():
        return [f"{TEMPLATE_PATH}: file is missing"]
    text = template.read_text(encoding="utf-8")
    return [
        f"{TEMPLATE_PATH}: missing heading: {heading}"
        for heading in REQUIRED_TEMPLATE_HEADINGS
        if heading not in text
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    errors = [*validate_limitations(root), *validate_template(root)]
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"repository rail check failed with {len(errors)} violation(s)", file=sys.stderr)
        return 1
    print("Repository rail checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
