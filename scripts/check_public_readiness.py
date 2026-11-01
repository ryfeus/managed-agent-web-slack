#!/usr/bin/env python3
"""Reject private deployment identifiers and credential-shaped values."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PRIVATE_PATH_PREFIXES = ("plans/",)
FORBIDDEN_LITERALS = tuple(
    "".join(parts)
    for parts in (
        ("339543", "757547"),
        ("339543", "757547-terraform-state-us-west-2"),
        ("agent_01Lgz", "ds16BNHNm6C7Ydw4LgZ"),
        ("env_01Hxeb", "orPnKfnamf7a74iuBq"),
        ("d1rpkpj7", "lelw5f.cloudfront.net"),
        ("E2SX5", "EGEU73DG5"),
        ("xbubrem64", "jebwhs4ct7vypgsb4.dsql.us-west-2.on.aws"),
        ("T04AH", "NM7H"),
        ("U04AH", "NM7P"),
    )
)
SECRET_PATTERN = re.compile(
    r"\b(?:xox[abp]-[A-Za-z0-9-]{16,}|xapp-[A-Za-z0-9-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|whsec_[A-Za-z0-9_-]{16,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
)
FIXTURE_SECRETS = {"whsec_ZTJlLXdlYmhvb2stc2VjcmV0"}
ENVIRONMENT_TEMPLATE = ".env.example"


def _lock_identity_text(source: str) -> str:
    """Exclude only valid, nonsecret CMA identity fields from private-ID checks."""
    try:
        data = json.loads(source, object_pairs_hook=_unique_json_object)
    except (ValueError, TypeError):
        return source
    if not isinstance(data, dict) or not isinstance(data.get("resources"), dict):
        return source
    for key, kind, prefix in (
        ("./agents/application.md", "agent", "agent"),
        ("./environments/application.yaml", "environment", "env"),
    ):
        resource = data["resources"].get(key)
        if (
            isinstance(resource, dict)
            and resource.get("kind") == kind
            and isinstance(resource.get("id"), str)
            and re.fullmatch(rf"{prefix}_[A-Za-z0-9]+", resource["id"])
        ):
            resource["id"] = "repository-managed-identity"
    return json.dumps(data)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate JSON key")
        data[key] = value
    return data


def scan_paths(root: Path, paths: list[Path], *, allow_private_paths: bool = True) -> list[str]:
    errors: list[str] = []
    for relative in paths:
        posix = relative.as_posix()
        if allow_private_paths and posix.startswith(PRIVATE_PATH_PREFIXES):
            continue
        if relative.name.startswith(".env") and relative.name != ENVIRONMENT_TEMPLATE:
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
        identity_text = _lock_identity_text(source) if posix == "cma/claude-lock.json" else source
        for value in FORBIDDEN_LITERALS:
            if value in identity_text:
                errors.append(f"{posix}: contains a private deployment identifier")
        for secret in SECRET_PATTERN.findall(source + "\n" + identity_text):
            if secret not in FIXTURE_SECRETS:
                errors.append(f"{posix}: contains a credential-shaped value")
    return errors


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
    return [Path(value) for value in result.stdout.decode().split("\0") if value]


def public_tree_paths(root: Path) -> list[Path]:
    return sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--public-tree",
        action="store_true",
        help="scan every file in an exported tree without private-path exemptions",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        paths = public_tree_paths(root) if args.public_tree else tracked_paths(root)
        errors = scan_paths(root, paths, allow_private_paths=not args.public_tree)
    except subprocess.CalledProcessError as error:
        print(f"unable to enumerate tracked files: {error}", file=sys.stderr)
        return 1
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"public-readiness check failed with {len(errors)} violation(s)", file=sys.stderr)
        return 1
    print("Public-readiness checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
