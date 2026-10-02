#!/usr/bin/env python3
"""Read repository-owned CMA identity locally; never contact provider services."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

RESOURCE_PATHS = {
    "agents/application.md": "agent",
    "environments/application.yaml": "environment",
}
SECRET_PATTERN = re.compile(
    r"\b(?:xox[abp]-[A-Za-z0-9-]{16,}|xapp-[A-Za-z0-9-]{16,}|"
    r"sk-ant-[A-Za-z0-9_-]{16,}|whsec_[A-Za-z0-9_-]{16,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
)
SECRET_FIELD_PATTERN = re.compile(
    r'(?im)(?:^|[,{])\s*["\x27]?(?:api[_-]?key|access[_-]?token|client[_-]?secret|'
    r"password|secret|authorization|signing[_-]?key|private[_-]?key|webhook[_-]?secret|"
    r"oauth[_-]?token|refresh[_-]?token|cookie[_-]?secret|aws[_-]?secret[_-]?access[_-]?key|"
    r'aws[_-]?session[_-]?token)["\x27]?\s*:'
)


class LockError(ValueError):
    """Unsafe or incomplete repository CMA configuration."""


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def positive_version(value: Any) -> int:
    if type(value) is int and value >= 1:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) and int(value) >= 1:
        return int(value)
    raise LockError("agent version must be a positive integer")


def validate_sources(project: Path) -> None:
    for relative in RESOURCE_PATHS:
        path = project / relative
        if not path.is_file() or not path.resolve().is_relative_to(project.resolve()):
            raise LockError(f"missing or unsafe CMA source: {relative}")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise LockError(f"empty CMA source: {relative}")
        if SECRET_PATTERN.search(text) or SECRET_FIELD_PATTERN.search(text):
            raise LockError(f"secret-bearing CMA source: {relative}")


def parse_lock(data: Any, project: Path, *, allow_partial: bool = False) -> dict[str, str | int]:
    validate_sources(project)
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise LockError("unsupported CMA lockfile format (expected version 1)")
    decoded = json.dumps(data)
    if SECRET_PATTERN.search(decoded) or SECRET_FIELD_PATTERN.search(decoded):
        raise LockError("secret-bearing CMA lockfile")
    origin = data.get("origin")
    if not isinstance(origin, dict) or not all(
        _string(origin.get(key)) for key in ("base_url", "organization_id", "workspace_id")
    ):
        raise LockError("CMA lockfile origin must identify its host, organization, and workspace")
    try:
        UUID(origin["organization_id"])
        url = urlsplit(origin["base_url"])
    except ValueError as error:
        raise LockError("malformed CMA lockfile origin") from error
    if not re.fullmatch(r"wrkspc_[A-Za-z0-9]+", origin["workspace_id"]):
        raise LockError("malformed CMA lockfile workspace origin")
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise LockError("CMA lockfile origin base_url must be a credential-free HTTPS URL")
    resources = data.get("resources")
    if not isinstance(resources, dict):
        raise LockError("CMA lockfile resources must be an object")
    selected: dict[str, dict[str, Any]] = {}
    for key, resource in resources.items():
        if not isinstance(key, str):
            raise LockError("CMA resource paths must be strings")
        # Permit CLI keys with or without the leading './', but never normalize traversal.
        relative = key[2:] if key.startswith("./") else key
        if relative not in RESOURCE_PATHS:
            raise LockError(f"unexpected CMA resource path: {key}")
        kind = RESOURCE_PATHS[relative]
        if kind in selected:
            raise LockError(f"ambiguous CMA {kind} resource")
        if not isinstance(resource, dict) or resource.get("kind") != kind:
            raise LockError(f"{key}: expected resource kind {kind}")
        prefix = "agent" if kind == "agent" else "env"
        if not isinstance(resource.get("id"), str) or not re.fullmatch(
            rf"{prefix}_[A-Za-z0-9]+", resource["id"]
        ):
            raise LockError(f"{key}: invalid {kind} ID")
        if not all(_string(resource.get(field)) for field in ("hash", "remote_hash")):
            raise LockError(f"{key}: missing local or remote fingerprint")
        selected[kind] = resource
        if kind == "agent":
            positive_version(resource.get("version"))
    for kind in RESOURCE_PATHS.values():
        if kind not in selected and not allow_partial:
            raise LockError(f"missing CMA {kind} resource")
    result: dict[str, str | int] = {}
    if "agent" in selected:
        result.update(
            agent_id=selected["agent"]["id"], agent_version=positive_version(selected["agent"].get("version"))
        )
    if "environment" in selected:
        result["environment_id"] = selected["environment"]["id"]
    return result


def read_lock(project: Path, *, allow_partial: bool = False) -> dict[str, str | int]:
    path = project / "claude-lock.json"
    if not path.resolve().is_relative_to(project.resolve()):
        raise LockError("unsafe CMA lockfile path")
    try:
        source = path.read_text(encoding="utf-8")
        if SECRET_PATTERN.search(source) or SECRET_FIELD_PATTERN.search(source):
            raise LockError("secret-bearing CMA lockfile")
        data = json.loads(source, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LockError(f"cannot read CMA lockfile: {path.name} ({type(error).__name__})") from error
    return parse_lock(data, project, allow_partial=allow_partial)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LockError(f"duplicate CMA lockfile key: {key}")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "json", "sources", "preflight"))
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1] / "cma")
    args = parser.parse_args()
    try:
        if args.command == "sources":
            validate_sources(args.project)
            return 0
        resolved = read_lock(args.project, allow_partial=args.command == "preflight")
    except (LockError, OSError, UnicodeError) as error:
        print(f"CMA validation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(resolved) if args.command == "json" else "CMA lock and sources validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
